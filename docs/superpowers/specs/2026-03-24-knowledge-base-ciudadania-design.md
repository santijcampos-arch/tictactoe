# Knowledge Base de Juzgados CCF — Design Spec

**Fecha:** 2026-03-24
**Proyecto:** NegroLex — explore_terminados.py + analizar_terminados.py + conocimiento_juzgados.json + check_citizenship.py

---

## Contexto

Los expedientes de ciudadanía CCF se distribuyen en ~18 secretarías (J01-J11). Cada juzgado tiene particularidades propias que no están documentadas en ningún lado: J08 solicita SIDE, J10 pide confirmación de asistencia por mail, etc.

Se dispone de ~150 expedientes terminados (con historial completo de actuaciones en el PJN) y ~38.000 mensajes de Telegram de clientes. El objetivo es extraer patrones por juzgado/secretaría y usar ese conocimiento para generar alertas proactivas y reactivas en `check_citizenship.py` cuando procesa los 136 casos activos.

**Piloto:** J08-S15 (12 expedientes terminados).

---

## Arquitectura

```
explore_terminados.py  →  terminados_raw.json
        ↓
analizar_terminados.py  →  conocimiento_juzgados.json
        ↓
check_citizenship.py (modificado)
        ↓
notifications.json + lawcase.html
```

El `conocimiento_juzgados.json` también se copia a `~/.claude/projects/.../memory/` para que Claude lo consulte en futuras sesiones como contexto de trabajo.

---

## Componente 1: `explore_terminados.py`

### Input

Lista hardcodeada de casos terminados como tuplas `(juzgado, secretaria, numero)`. Para el piloto, solo J08-S15:

```python
CASOS_TERMINADOS = [
    # (juzgado, secretaria, numero)
    (8, 15, '11346/2024'),
    (8, 15, '13863/2024'),
    (8, 15, '15131/2024'),
    (8, 15, '16502/2024'),
    (8, 15, '17453/2024'),
    (8, 15, '19404/2024'),
    (8, 15, '27054/2024'),
    (8, 15, '27117/2024'),
    (8, 15, '4333/2023'),
    (8, 15, '6275/2024'),
    (8, 15, '9027/2023'),
    (8, 15, '23471/2024'),
]
```

### Infraestructura

- Copiar de `explore_jura.py`: `setup_driver`, `quit_driver`, `solve_captcha`, `navegar_a_caso`
- `N_WORKERS = 3` para el scraping masivo (solo este script — `check_citizenship.py` mantiene N_WORKERS=1)
- `sleep(8)` entre casos por worker
- Cada worker tiene su propio Chrome y su propio ddddocr

### Por cada caso

1. Construir el número completo con prefijo CCF antes de llamar a `parse_citizenship_case_number()`. Los casos están hardcodeados como `(juzgado, secretaria, numero)` donde `numero` es bare (`"11346/2024"`); construir `"CCF " + numero` (ej: `"CCF 011346/2024"` con zero-padding a 6 dígitos si aplica) antes de pasarlo a la función, igual que hace `check_citizenship.py` al leer `caseNumber` desde `cases.json`.
2. Extraer **carátula** del expediente — texto que aparece como título (ej: `"PETROV, Ivan s/ Ciudadanía Argentina"`). Buscar en el DOM el elemento que contenga "s/ Ciudadanía" o similar.
3. Extraer **todas las actuaciones** — mismo método que `get_actuaciones_cit()` en `check_citizenship.py`: `{tipo, descripcion, fecha, oficina}`
4. Guardar resultado

### Output: `terminados_raw.json`

```json
{
  "J08-S15": [
    {
      "case_number": "11346/2024",
      "caratula": "PETROV, Ivan s/ Ciudadanía Argentina",
      "partido_apellido": "PETROV",
      "actuaciones": [
        {"fecha": "15/03/2024", "tipo": "FIRMA DESPACHO", "descripcion": "OFICIO SIDE", "oficina": "..."}
      ],
      "scraped_at": "2026-03-24T10:00:00",
      "error": null
    }
  ]
}
```

### Resume capability

Al iniciar, cargar `terminados_raw.json` si existe. Saltear casos que ya tengan entrada con `error: null` (scrapeados correctamente). Los casos con error se reintentan.

### Manejo de errores

- Captcha falla 3 veces → `"error": "captcha"`, continuar
- Expediente no encontrado → `"error": "not_found"`, continuar
- Chrome muere → reiniciar driver, reintentar el caso actual (max 2 reinicios por worker)

### Extracción de `partido_apellido`

De la carátula, extraer el primer apellido en mayúsculas antes de la coma o del `s/`. Ejemplos:
- `"PETROV, Ivan s/ Ciudadanía"` → `"PETROV"`
- `"GARCIA LOPEZ, Maria s/ Ciudadanía"` → `"GARCIA"`

Usado para el matching con Telegram en el paso siguiente.

---

## Componente 2: `analizar_terminados.py`

### Input

- `terminados_raw.json`
- `telegram_messages.json` — dict de grupo → lista de mensajes `{texto, fecha, de}`

### Proceso por juzgado/secretaría

Para cada grupo `"J08-S15"`:

1. **Cargar casos** del grupo desde `terminados_raw.json` (solo los sin error)
2. **Matching Telegram** — para cada caso, buscar grupos de Telegram cuyo nombre contenga `partido_apellido` (case-insensitive). Tomar hasta 20 mensajes más recientes del grupo encontrado.
3. **Llamada a Groq por caso** — prompt que recibe actuaciones + mensajes Telegram y extrae:
   - Etapas presentes y su orden
   - Actuaciones inusuales (no presentes en la mayoría de los otros juzgados)
   - Pedidos específicos del juzgado al cliente
4. **Agregación** — consolidar resultados de todos los casos del grupo para extraer patrones comunes (presentes en >50% de los casos)
5. **Segunda llamada a Groq** — con los resultados agregados, generar el perfil final del juzgado/secretaría

### Prompt por caso

```
Analizá el siguiente expediente de ciudadanía argentina en el fuero CCF.

Juzgado: {juzgado}, Secretaría: {secretaria}
Expediente: {case_number}

ACTUACIONES (cronológico, más antigua primero):
{actuaciones_text}

MENSAJES TELEGRAM DEL CLIENTE (si hay):
{telegram_text}

Extraé en JSON:
{
  "etapas_detectadas": ["pfa_interpol", "renaper", ...],
  "actuaciones_inusuales": ["OFICIO SIDE", ...],
  "pedidos_al_cliente": ["antecedentes SIDE", ...],
  "observaciones": "texto libre"
}
Solo incluí lo que podás determinar con certeza.
```

### Prompt de agregación

```
Analizaste {n} expedientes del Juzgado {juzgado} Secretaría {secretaria}.

Resultados por caso:
{resultados_json}

Generá el perfil del juzgado en JSON:
{
  "particularidades": ["lista de cosas que este juzgado hace distinto a los demás"],
  "secuencia_tipica": ["orden típico de etapas"],
  "alertas_proactivas": [
    {
      "condicion": {"etapa_presente": "renaper", "etapa_ausente": "side"},
      "mensaje": "descripción de la alerta para el estudio"
    }
  ],
  "alertas_reactivas": [
    {
      "keywords": ["OFICIO SIDE"],
      "mensaje": "descripción de la acción a tomar"
    }
  ]
}
```

### Rate limiting Groq

Usar `_GROQ_SEM` y `_GROQ_LOCK` copiados de `check_citizenship.py`. Modelo: `llama-3.1-8b-instant`. Un llamado a la vez.

### Output: `conocimiento_juzgados.json`

```json
{
  "J08-S15": {
    "nombre": "Juzgado 8 - Secretaría 15",
    "casos_analizados": 12,
    "generado_en": "2026-03-24T...",
    "particularidades": [
      "Solicita oficio SIDE además de PFA Interpol",
      "Pide medios de vida actualizados al momento de sentencia"
    ],
    "secuencia_tipica": [
      "pfa_interpol", "renaper", "side", "reincidencia",
      "dnm", "pfa_dactilo", "edicto", "medios_de_vida",
      "fiscal", "sentencia", "carta_ciudadania"
    ],
    "alertas_proactivas": [
      {
        "condicion": {"etapa_presente": "renaper", "etapa_ausente": "side"},
        "mensaje": "J08 suele solicitar SIDE — preparar al cliente con antecedentes penales SIDE"
      }
    ],
    "alertas_reactivas": [
      {
        "keywords": ["OFICIO SIDE", "INFORME SIDE"],
        "mensaje": "J08 solicitó SIDE — coordinar envío de documentación con el cliente"
      }
    ]
  }
}
```

---

## Componente 3: Integración en `check_citizenship.py`

### Nueva función: `detectar_alertas_conocimiento(case, actuaciones, conocimiento)`

**Parámetros:**
- `case`: dict del caso desde `cases.json`
- `actuaciones`: lista de actuaciones nuevas del PJN (ya scrapeadas en esta corrida)
- `conocimiento`: dict cargado desde `conocimiento_juzgados.json`

**Retorna:** lista de strings con mensajes de alerta (vacía si no hay alertas).

### Determinar J/S del caso

El juzgado se extrae del `caseNumber` del caso (ej: el número CCF determina el juzgado asignado vía el sistema PJN). La secretaría **no está en `cases.json`** y no se intenta inferir automáticamente — se agrega el campo `"secretaria": 15` manualmente a cada caso en `cases.json` como parte del deploy antes de la primera corrida con knowledge base activo. Esta es la única estrategia implementada: sin mapeo por rangos, sin campo auxiliar en `conocimiento_juzgados.json`.

`detectar_alertas_conocimiento()` construye la clave `"J{juzgado}-S{secretaria}"` desde `case['juzgado']` y `case['secretaria']`. Si alguno de los dos campos falta en el caso, retorna lista vacía (ver Casos borde).

### Evaluación de alertas proactivas

`stages` es el dict de etapas detectadas del caso, construido por la lógica existente en `process_case()` (ej: `{'pfa_interpol': 'OK', 'renaper': 'OK'}`). Se pasa como parámetro adicional a `detectar_alertas_conocimiento()`:

**Firma final:** `detectar_alertas_conocimiento(case, actuaciones, conocimiento, stages)`

```python
for alerta in perfil['alertas_proactivas']:
    cond = alerta['condicion']
    etapa_presente = cond.get('etapa_presente')
    etapa_ausente = cond.get('etapa_ausente')
    if (not etapa_presente or stages.get(etapa_presente) == 'OK') and \
       (not etapa_ausente or not stages.get(etapa_ausente)):
        alertas.append(alerta['mensaje'])
```

Sin `eval()` — solo acceso a dict.

### Evaluación de alertas reactivas

Para cada actuación nueva (las que tienen fecha posterior a `lastActionDate` previo):
```python
for alerta in perfil['alertas_reactivas']:
    desc_up = act['descripcion'].upper()
    if any(kw in desc_up for kw in alerta['keywords']):
        alertas.append(alerta['mensaje'])
```

### Integración en `process_case()`

Llamar después de la detección de etapas y antes de escribir `cases.json`:

```python
conocimiento = _cargar_conocimiento()  # carga lazy con cache en módulo
alertas_kb = detectar_alertas_conocimiento(case, actuaciones, conocimiento, stages)
for msg in alertas_kb:
    add_notification(case, 'conocimiento_alert', msg)
```

`_cargar_conocimiento()` carga `conocimiento_juzgados.json` una sola vez al inicio del proceso y lo cachea en una variable de módulo.

---

## Memory persistence

Al finalizar `analizar_terminados.py`, copiar `conocimiento_juzgados.json` a:
`C:\Users\Usuario\.claude\projects\C--Users-Usuario-Desktop\memory\conocimiento_juzgados.json`

Y agregar entrada en `MEMORY.md`:
```
- [conocimiento_juzgados.json](./conocimiento_juzgados.json) — Knowledge base de patrones por juzgado/secretaría CCF (generado de expedientes terminados + Telegram).
```

---

## Tareas de implementación (piloto J08-S15)

1. **`explore_terminados.py`** — crear script con los 12 casos de J08-S15, scraping con N_WORKERS=3, output a `terminados_raw.json`
2. **`analizar_terminados.py`** — crear script de análisis con Groq, output a `conocimiento_juzgados.json`
3. **`check_citizenship.py`** — agregar `detectar_alertas_conocimiento()` e integrar en `process_case()`
4. **Memory** — copiar knowledge base a memoria y actualizar `MEMORY.md`

---

## Casos borde

- **Carátula no encontrada**: usar `case_number` como fallback para identificar el caso, `partido_apellido = None`, no intentar matching Telegram
- **Sin match en Telegram**: analizar solo con actuaciones PJN, sin contexto de chat
- **Juzgado no en knowledge base**: `detectar_alertas_conocimiento()` retorna lista vacía silenciosamente
- **Campo `secretaria` o `juzgado` ausente en el caso**: `detectar_alertas_conocimiento()` retorna lista vacía silenciosamente
- **`conocimiento_juzgados.json` no existe**: `_cargar_conocimiento()` retorna dict vacío, no crashea
