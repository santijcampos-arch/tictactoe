# check_citizenship.py — Diseño Final

**Fecha:** 2026-03-22
**Estado:** Aprobado

---

## Resumen

Script `check_citizenship.py` que consulta el PJN (scw.pjn.gov.ar) para los casos de ciudadanía, detecta el avance de cada etapa procesal, y genera notificaciones cuando hay algo que el estudio debe atender. Corre en background vía Task Scheduler, lunes a viernes.

---

## Contexto

**Estado actual:**
- Los casos de ciudadanía en `cases.json` tienen `caseNumber` (ej: `CCF 020934/2022` o `24590/2024`) pero `lastActionDate: null` y etapas sin actualizar desde el ingreso manual.
- `check_pjn.py` ya hace scraping del PJN para casos constitucionales — `check_citizenship.py` es su análogo para ciudadanía.
- ~130 casos de ciudadanía con número de expediente. Primera corrida: ~27 min. Corridas posteriores: 3-6 min (smart skip).

**Infraestructura reutilizada de `check_pjn.py`:**
- `setup_driver()` / `quit_driver()` — Chrome headless con anti-detección
- `solve_captcha()` — resolución de captcha con ddddocr
- `_atomic_write_json()` — escritura atómica de JSON
- `parse_case_number()` — parseo de número de expediente
- `JURISDICTION_CODES` — mapa de prefijos a códigos PJN

---

## Etapas del proceso de ciudadanía

Las 12 etapas del flujo, en orden, con sus valores posibles (`"OK"`, `"NO"`, `""`):

| Key | Label | Descripción |
|---|---|---|
| `pfa_interpol` | PFA INTERPOL | Respuesta PFA sobre antecedentes Interpol |
| `renaper` | RENAPER | Informe RENAPER |
| `cne` | CNE | Informe Cámara Nacional Electoral |
| `reincidencia` | REINCIDENCIA | Informe de reincidencia |
| `dnm` | DNM | Informe Dirección Nacional de Migraciones |
| `pfa_dactilo` | PFA DACTILO | Informe dactiloscópico PFA |
| `edicto` | EDICTO | Publicación de edicto |
| `pfa_convenio` | PFA CONVENIO | Informe PFA convenio |
| `medios_de_vida` | MEDIOS DE VIDA | Acreditación de medios de vida |
| `fiscal` | FISCAL | Dictamen fiscal |
| `sentencia` | SENTENCIA | Sentencia del juzgado |
| `carta_ciudadania` | CARTA CIUDADANÍA | Carta de ciudadanía entregada |

---

## Arquitectura

### Flujo principal

```
Para cada caso citizenship con caseNumber:
  1. Smart skip → si lastActionDate no cambió → next case
  2. Abrir PJN → resolver captcha → buscar expediente
  3. Extraer lista de actuaciones (texto de cada fila)
  4. Keyword detection → marcar etapas obvias como OK/NO
  5. Groq (1 call) → analizar actuaciones restantes → stage updates + action flags
  6. Actualizar stages + lastActionDate + lastPjnCheck en cases.json
  7. Si requires_action → escribir notificación en notifications.json
```

### Paralelismo

- `N_WORKERS = 2` — dos Chrome simultáneos
- Delay mínimo de 3s entre búsquedas consecutivas al PJN (independiente del worker)
- `MAX_CHROME_INSTANCES = 3` (mismo límite que check_pjn.py)

---

## Componente 1 — Smart Skip

```python
def should_skip(case):
    """True si el caso no tiene actuaciones nuevas desde el último check."""
    if not case.get('lastPjnCheck'):
        return False  # nunca chequeado → no saltear
    if not case.get('lastActionDate'):
        return False  # sin fecha guardada → no saltear
    # El PJN va a confirmar si lastActionDate cambió al cargar la página
    # El skip real se decide después de ver la fecha del PJN, antes de llamar a Groq
    return False
```

El skip real ocurre en `process_case()`:
```python
# Después de cargar el expediente en el PJN:
pjn_last_date = get_last_action_date(driver)  # fecha de la actuación más reciente
if pjn_last_date == case.get('lastActionDate') and case.get('lastPjnCheck'):
    print(f"    Sin cambios — saltando")
    update_last_check(case)  # actualiza lastPjnCheck sin tocar el resto
    return
```

---

## Componente 2 — Parseo de número de expediente

Los casos de ciudadanía tienen dos formatos:
- `CCF 020934/2022` → jurisdicción CCF explícita (código `'3'`)
- `24590/2024` → sin prefijo → default CCF (`'3'`)

```python
def parse_citizenship_case_number(case_number):
    """Extiende parse_case_number() con fallback a CCF para números sin prefijo."""
    code, number, year = parse_case_number(case_number)
    if code:
        return code, number, year
    # Intentar parsear como número puro: XXXXX/YYYY
    m = re.match(r'0*(\d+)/(\d{4})', case_number.strip())
    if m:
        return JURISDICTION_CODES['CCF'], m.group(1), m.group(2)
    return None, None, None
```

---

## Componente 3 — Extracción de actuaciones

Tras cargar el expediente en el PJN, extraer todas las filas de la tabla de actuaciones:

```python
def get_actuaciones(driver):
    """
    Retorna lista de dicts con los campos de cada actuación:
    [{tipo, descripcion, fecha, oficina}, ...]
    Navega por todas las páginas si hay paginación.
    """
```

Campos a extraer por fila:
- **tipo**: FIRMA DESPACHO, ESCRITO INCORPORADO, MOVIMIENTO, DEO, EVENTO DEO, etc.
- **descripcion**: texto libre (columna "DESCRIPCIÓN DE TALLE")
- **fecha**: fecha de la actuación
- **oficina**: VJ6, etc.

---

## Componente 4 — Keyword Detection

Antes de llamar a Groq, detectar etapas con patrones inequívocos:

| Patrón (en `descripcion`, case-insensitive) | Stage | Valor |
|---|---|---|
| `CONTESTACION INTERPOL` | `pfa_interpol` | `OK` |
| `CONTESTACION RENAPER` | `renaper` | `OK` |
| `INFORME REINCIDENCIA` | `reincidencia` | `OK` |
| `LIBRE EDICTO` o `DE LIBRE EDICTO` | `edicto` | `OK` |
| `INFORME.*CNE` o `CAMARA NACIONAL ELECTORAL` | `cne` | `OK` |
| `CARTA DE CIUDADANIA` o `CARTA CIUDADANIA` | `carta_ciudadania` | `OK` |

Estos son los únicos patrones suficientemente unívocos para marcar sin LLM. Todo lo demás va a Groq.

---

## Componente 5 — Análisis Groq

### Cuándo se llama

Una sola llamada por caso, con **todas** las actuaciones que no fueron marcadas por keywords. Se llama solo si hay actuaciones sin resolver o si `lastActionDate` cambió.

### Prompt

```
Sos un asistente para un estudio de abogados argentino especializado en ciudadanía.
Analizás las actuaciones de un expediente de ciudadanía del PJN y determinás:
1. El estado actual de cada etapa procesal
2. Si hay algo que el estudio debe atender (pedido de documentación, dictamen desfavorable, sentencia de rechazo, etc.)

Etapas a evaluar: pfa_interpol, renaper, cne, reincidencia, dnm, pfa_dactilo, edicto, pfa_convenio, medios_de_vida, fiscal, sentencia, carta_ciudadania

Ya fueron detectadas automáticamente (no re-evaluar): {ya_detectadas}

Actuaciones del expediente (de más antigua a más reciente):
{actuaciones_texto}

Datos del cliente (contexto adicional de Telegram si disponible):
{telegram_context}

Respondé en JSON:
{
  "stages": {
    "pfa_interpol": "OK"|"NO"|"",
    ...
  },
  "requires_action": true|false,
  "action_note": "Descripción de qué debe hacer el estudio, si corresponde",
  "last_relevant_stage": "nombre de la etapa más avanzada detectada"
}

Solo incluí en "stages" las etapas que podás determinar con certeza. Dejá "" las que no tengas información.
Si "requires_action" es true, "action_note" debe ser específico: qué se pide, en qué actuación, con qué fecha.
```

### Contexto de Telegram

Si existe `telegram_ciudadania.json`, buscar mensajes del cliente por nombre. Incluir los últimos 3 mensajes relevantes como contexto adicional al prompt de Groq (puede ayudar a entender documentación ya enviada, fechas de audiencia, etc.).

---

## Componente 6 — Actualización de datos

### cases.json

Por cada caso procesado:
```python
case['stages'] = merged_stages          # keyword + Groq, sin pisar OKs ya existentes
case['lastActionDate'] = pjn_last_date  # fecha más reciente del PJN
case['lastPjnCheck'] = datetime.now().isoformat()
if groq_result.get('requires_action'):
    case['nextAction'] = groq_result['action_note']
```

**Regla de merge**: nunca bajar una etapa de `OK` a `""`. Solo se puede avanzar (de `""` a `OK`/`NO`) o mantener.

### notifications.json

Cuando `requires_action` es True o cuando una etapa nueva se completó:

```python
{
    "id": "<timestamp>-<caseId>",
    "type": "citizenship_update",
    "caseId": "<id>",
    "clientName": "<nombre>",
    "caseNumber": "<número>",
    "message": "<action_note o descripción de etapa completada>",
    "date": "<fecha>",
    "read": false
}
```

---

## Scheduling

- **Frecuencia**: lunes a viernes, 10hs y 16hs (2 corridas por día)
- **Comando**: `pythonw3.13 check_citizenship.py` (sin consola visible)
- **Configurar en**: Task Scheduler, igual que `check_pjn.py`

---

## Archivos

| Archivo | Rol |
|---|---|
| `check_citizenship.py` | Script principal (nuevo) |
| `cases.json` | Lee y actualiza casos de ciudadanía |
| `notifications.json` | Escribe notificaciones |
| `telegram_ciudadania.json` | Lee contexto adicional por cliente (opcional) |
| `groq_key.txt` | API key Groq (ya existe) |

---

## Rate limiting

- `N_WORKERS = 2`
- Delay global de 3s entre búsquedas al PJN (Lock compartido entre workers)
- Si el PJN devuelve un error o timeout 3 veces seguidas → loguear y continuar con el siguiente caso
- No reintentar indefinidamente — igual que `check_pjn.py`

---

## Criterios de éxito

- [ ] La primera corrida procesa los ~130 casos en ≤30 minutos.
- [ ] Corridas posteriores procesan solo casos con cambios en ≤10 minutos.
- [ ] Las etapas detectadas por keyword se marcan correctamente sin llamar a Groq.
- [ ] Groq identifica correctamente HACESE SABER con pedidos de documentación y genera notificación.
- [ ] Una sentencia de rechazo genera notificación con `requires_action: true`.
- [ ] No se pisan etapas ya marcadas OK.
- [ ] El script termina limpiamente aunque un caso falle (Chrome no queda colgado).
