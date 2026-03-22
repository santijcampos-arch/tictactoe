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
- ~130 casos de ciudadanía con número de expediente. Primera corrida: ~55 min. Corridas posteriores con smart skip: 3-6 min (solo casos con actuaciones nuevas).

**Infraestructura reutilizada de `check_pjn.py`:**
- `setup_driver()` / `quit_driver()` — Chrome headless con anti-detección
- `solve_captcha()` — resolución de captcha con ddddocr
- `_atomic_write_json()` — escritura atómica de JSON
- `_acquire_file_lock()` / `_release_file_lock()` — locks cross-process
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
  1. Abrir PJN → resolver captcha → buscar expediente
  2. Leer fecha de última actuación del PJN
  3. Smart skip: si fecha == lastActionDate guardada Y lastPjnCheck existe → actualizar lastPjnCheck y continuar
  4. Extraer lista completa de actuaciones
  5. Keyword detection → marcar etapas obvias como OK/NO
  6. Si quedan etapas sin resolver: Groq (1 call) → stage updates + action flags
  7. Merge de stages (respetando reglas de merge)
  8. Actualizar cases.json (thread-safe)
  9. Si requires_action → escribir notificación en notifications.json
```

### Paralelismo y rate limiting

- `N_WORKERS = 1` — un Chrome a la vez, igual que `check_pjn.py`
- `time.sleep(8)` entre casos (mismo intervalo que `check_pjn.py`)
- `MAX_CHROME_INSTANCES = 3` (límite de seguridad, igual que `check_pjn.py`)

> Nota: N_WORKERS = 1 es deliberado. El PJN es el mismo servidor que usa check_pjn.py. Dos workers simultáneos podrían generar rate limiting. Si después de varias semanas de uso normal no hay problemas, se puede evaluar subir a 2.

---

## Componente 1 — Smart Skip

El skip se evalúa dentro de `process_case()`, después de cargar la página del expediente y leer la fecha de la última actuación:

```python
def process_case(driver, case, telegram_ctx):
    # Cargar expediente, resolver captcha...
    pjn_last_date = get_last_action_date(driver)  # fecha de la actuación más reciente en el PJN

    # Smart skip: si nada cambió desde el último check, actualizar solo lastPjnCheck
    if pjn_last_date and pjn_last_date == case.get('lastActionDate') and case.get('lastPjnCheck'):
        print(f"    Sin cambios — saltando {case['caseNumber']}")
        _update_last_check(case['id'])  # escribe solo lastPjnCheck, thread-safe
        return

    # ... continuar con extracción y análisis
```

```python
def _update_last_check(case_id):
    """Actualiza solo lastPjnCheck para el caso dado. Thread-safe vía file lock."""
    if not _acquire_file_lock(_CASES_FILE_LOCK_PATH):
        return
    try:
        with open(CASES_FILE, encoding='utf-8') as f:
            all_cases = json.load(f)
        for c in all_cases:
            if c['id'] == case_id:
                c['lastPjnCheck'] = datetime.now().isoformat()
                break
        _atomic_write_json(CASES_FILE, all_cases)
    finally:
        _release_file_lock(_CASES_FILE_LOCK_PATH)
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
    # Intentar parsear como número puro: XXXXX/YYYY (sin prefijo de jurisdicción)
    m = re.match(r'0*(\d+)/(\d{4})', case_number.strip())
    if m:
        return JURISDICTION_CODES['CCF'], m.group(1), m.group(2)
    return None, None, None
```

> Los ceros a la izquierda siempre se eliminan (el PJN espera el número sin ellos).

---

## Componente 3 — Extracción de actuaciones

Tras cargar el expediente, extraer todas las filas de la tabla de actuaciones. Si hay paginación, navegar todas las páginas:

```python
def get_actuaciones(driver):
    """
    Retorna lista de dicts, una por actuación, de más antigua a más reciente:
    [{"tipo": "FIRMA DESPACHO", "descripcion": "ETAPA 1 - OFICIO/DEO", "fecha": "03/24/2024", "oficina": "VJ6"}, ...]
    """
```

Campos a extraer por fila:
- **tipo**: FIRMA DESPACHO, ESCRITO INCORPORADO, MOVIMIENTO, DEO, EVENTO DEO, etc.
- **descripcion**: texto libre (columna "DESCRIPCIÓN DE TALLE")
- **fecha**: fecha de la actuación
- **oficina**: VJ6, etc.

---

## Componente 4 — Keyword Detection

Antes de llamar a Groq, detectar etapas con patrones inequívocos sobre el campo `descripcion` (case-insensitive):

| Patrón | Stage | Valor |
|---|---|---|
| `CONTESTACION INTERPOL` | `pfa_interpol` | `OK` |
| `CONTESTACION RENAPER` | `renaper` | `OK` |
| `INFORME REINCIDENCIA` | `reincidencia` | `OK` |
| `LIBRE EDICTO` o `DE LIBRE EDICTO` | `edicto` | `OK` |
| `INFORME.*CNE` o `CAMARA NACIONAL ELECTORAL` | `cne` | `OK` |
| `CARTA DE CIUDADANIA` o `CARTA CIUDADANIA` | `carta_ciudadania` | `OK` |

Solo estos patrones son suficientemente inequívocos para marcar sin LLM. Todo lo demás va a Groq.

---

## Componente 5 — Análisis Groq

### Cuándo se llama

Una sola llamada por caso, con todas las actuaciones del expediente. Se llama solo si:
1. `lastActionDate` cambió (o es la primera vez que se chequea), **Y**
2. Al menos una etapa sigue sin determinar (valor `""`) después del keyword detection.

Si todas las etapas ya tienen valor `OK` o `NO` tras el keyword detection, no se llama a Groq.

### Manejo de errores Groq

Si la llamada a Groq falla (error de red, rate limit, JSON inválido):
- Loguear el error: `[Groq] Error en {caseNumber}: {e}`
- Dejar las etapas en su estado actual (no modificar stages)
- No generar notificación
- Continuar con el siguiente caso

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

Si existe `telegram_ciudadania.json`, buscar mensajes del cliente por nombre. Incluir los últimos 3 mensajes relevantes como contexto adicional (puede ayudar a entender documentación ya enviada, fechas de audiencia, etc.).

---

## Componente 6 — Merge de stages

**Regla completa de merge** al combinar stages existentes con los detectados (keyword + Groq):

| Estado actual | Nuevo valor detectado | Resultado |
|---|---|---|
| `OK` | cualquiera | `OK` (nunca se baja) |
| `NO` | `OK` | `OK` (puede corregirse si el informe fue favorable) |
| `NO` | `""` | `NO` (mantiene) |
| `""` | `OK` o `NO` | nuevo valor |
| `""` | `""` | `""` (sin info) |

```python
def merge_stages(existing, detected):
    """Merge de stages con las reglas de precedencia definidas."""
    result = dict(existing)
    for key, new_val in detected.items():
        current = result.get(key, '')
        if current == 'OK':
            continue  # OK es permanente
        if new_val in ('OK', 'NO'):
            result[key] = new_val
    return result
```

---

## Componente 7 — Actualización de datos

### cases.json

Por cada caso procesado, la escritura es thread-safe: primero se adquiere `_CASES_LOCK` (threading.Lock en memoria), luego `_acquire_file_lock()` (lock cross-process), igual que en `check_pjn.py`:

```python
with _CASES_LOCK:
    if not _acquire_file_lock(_CASES_FILE_LOCK_PATH):
        print(f"    No se pudo obtener lock — saltando escritura")
        return
    try:
        all_cases = json.load(open(CASES_FILE, encoding='utf-8'))
        for c in all_cases:
            if c['id'] == case['id']:
                c['stages'] = merge_stages(c.get('stages', {}), detected_stages)
                c['lastActionDate'] = pjn_last_date
                c['lastPjnCheck'] = datetime.now().isoformat()
                if groq_result and groq_result.get('requires_action'):
                    c['nextAction'] = groq_result['action_note']
                break
        _atomic_write_json(CASES_FILE, all_cases)
    finally:
        _release_file_lock(_CASES_FILE_LOCK_PATH)
```

### notifications.json

Se escribe una notificación cuando `requires_action` es `True`. Las etapas completadas de forma rutinaria (ej: llegó RENAPER) **no** generan notificación — solo los casos que requieren acción del estudio.

Excepción: `sentencia` y `carta_ciudadania` generan notificación cuando se completan **por primera vez** (es decir, cuando el valor pasa de `""` a `OK`/`NO` en esta corrida). Si ya estaban en `OK`/`NO` en el run anterior, no se vuelve a notificar.

```python
{
    "id": "<timestamp>-<caseId>",
    "type": "citizenship_update",
    "caseId": "<id>",
    "clientName": "<nombre>",
    "caseNumber": "<número>",
    "message": "<action_note o 'Sentencia dictada' / 'Carta de ciudadanía lista'>",
    "date": "<fecha>",
    "read": false
}
```

---

## UI — Indicadores de acción requerida

### Lista de ciudadanía (`view-citizenship`)

Cuando un caso tiene `nextAction` seteado (no vacío), mostrar un ícono de advertencia al lado del nombre del cliente en la lista. Usar el mismo color de acento del tema (gold `#b8975a`) para consistencia visual.

```html
<!-- Ejemplo de fila con alerta -->
<span class="cit-action-badge" title="Acción requerida">⚠</span>
```

El badge debe ser sutil — no un botón, solo un indicador visual que el usuario ve de un vistazo sin que distraiga del resto de la lista.

### Detalle del caso (`view-case-detail`)

Cuando el caso tiene `nextAction`, mostrar un bloque destacado en el detalle, visible inmediatamente al abrir el caso (sin tener que scrollear):

```html
<div class="case-action-alert">
  <span class="case-action-alert__icon">⚠</span>
  <div class="case-action-alert__body">
    <div class="case-action-alert__label">Acción requerida</div>
    <div class="case-action-alert__text">{nextAction}</div>
  </div>
</div>
```

**Estilos**: fondo sutil con borde izquierdo en gold (`#b8975a`), texto claro sobre fondo oscuro navy. Mismo sistema visual que el resto de la app (Dark Refined Flat).

**Posición**: arriba de todo en el detalle del caso, antes de los campos editables.

**Clearing**: si el abogado resuelve la acción, puede borrar el texto de `nextAction` en el detalle (campo editable inline, igual que los comentarios). Al guardar el caso vacío ese campo, el badge desaparece de la lista.

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

## Criterios de éxito

- [ ] La primera corrida procesa los ~130 casos en ≤60 minutos.
- [ ] Corridas posteriores procesan solo casos con cambios en ≤10 minutos.
- [ ] Las etapas detectadas por keyword se marcan correctamente sin llamar a Groq.
- [ ] Si todas las etapas están resueltas tras keyword detection, no se llama a Groq.
- [ ] Groq identifica correctamente HACESE SABER con pedidos de documentación y genera notificación.
- [ ] Una sentencia de rechazo genera notificación con `requires_action: true`.
- [ ] Una sentencia favorable genera notificación aunque `requires_action` sea false.
- [ ] No se pisan etapas ya marcadas OK.
- [ ] Un error de Groq no interrumpe el script — se loguea y continúa.
- [ ] El script termina limpiamente aunque un caso falle (Chrome no queda colgado).
