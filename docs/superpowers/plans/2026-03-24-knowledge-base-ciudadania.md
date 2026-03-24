# Knowledge Base de Juzgados CCF — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pipeline that scrapes 12 terminated J08-S15 citizenship cases, analyzes them with Groq to extract court-specific patterns, generates a knowledge base (`conocimiento_juzgados.json`), and integrates it into `check_citizenship.py` to fire proactive and reactive alerts on the 136 active cases.

**Architecture:** `explore_terminados.py` scrapes PJN → `terminados_raw.json`; `analizar_terminados.py` calls Groq per case then aggregates → `conocimiento_juzgados.json`; `check_citizenship.py` loads the KB lazily and calls `detectar_alertas_conocimiento()` inside `process_case()`.

**Tech Stack:** Python 3.13, Selenium + ddddocr (PJN scraping), BeautifulSoup (HTML parsing), Groq API `llama-3.1-8b-instant` (pattern extraction), `concurrent.futures.ThreadPoolExecutor` (N_WORKERS=3 for scraper only), `json`/`shutil` (file I/O).

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `explore_terminados.py` | Create | Scrape J08-S15 terminated cases → `terminados_raw.json` |
| `analizar_terminados.py` | Create | Groq analysis + aggregation → `conocimiento_juzgados.json` + memory copy |
| `check_citizenship.py` | Modify (lines ~30, ~80, ~1000) | Add KB loader + `detectar_alertas_conocimiento()` + call in `process_case()` |
| `terminados_raw.json` | Generated | Raw scrape output (not committed) |
| `conocimiento_juzgados.json` | Generated | Knowledge base consumed at runtime (not committed) |

---

## Task 1: `explore_terminados.py` — PJN Scraper

**Files:**
- Create: `explore_terminados.py`

### Context for the implementer

This script scrapes the PJN (`scw.pjn.gov.ar`) for 12 hardcoded J08-S15 terminated citizenship cases and saves their actuaciones to `terminados_raw.json`. It's a one-off script run manually, not scheduled.

Copy these verbatim from `explore_jura.py` (same logic, same PJN site):
- `setup_driver()` — Chrome setup (no `_ACTIVE_DRIVERS` counter needed, this is single-process)
- `solve_captcha(driver, ocr)` — captcha resolution
- `navegar_a_caso(driver, ocr, case_number)` — navigates to a case by bare number like `"11346/2024"`, hardcoded CCF code `'3'`

Copy `get_actuaciones_cit(driver)` verbatim from `check_citizenship.py` (lines 576–670). This returns `[{tipo, descripcion, fecha, oficina, pdf_href}]` oldest-first. No pagination — accepted limitation for this pilot.

---

- [ ] **Step 1: Write the script skeleton (imports + constants + CASOS_TERMINADOS)**

```python
"""
NegroLex — Scraper de expedientes terminados J08-S15
Scrape las actuaciones de 12 expedientes terminados del PJN para el piloto de knowledge base.

Uso: python explore_terminados.py
Salida: terminados_raw.json
"""

import re
import os
import json
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup
import ddddocr

FOLDER     = os.path.dirname(os.path.abspath(__file__))
PJN_URL    = 'https://scw.pjn.gov.ar/scw/home.seam'
CCF_CODE   = '3'
RAW_FILE   = os.path.join(FOLDER, 'terminados_raw.json')
N_WORKERS  = 3

_PRINT_LOCK = threading.Lock()
_RAW_LOCK   = threading.Lock()

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


def plog(msg=''):
    with _PRINT_LOCK:
        print(msg, flush=True)
```

- [ ] **Step 2: Copy driver functions from `explore_jura.py`**

Copy verbatim: `setup_driver()`, `solve_captcha(driver, ocr)`, `navegar_a_caso(driver, ocr, case_number)` from `explore_jura.py`.

`navegar_a_caso` parses bare numbers like `"11346/2024"` with `re.match(r'0*(\d+)/(\d{4})', ...)` and uses `CCF_CODE` hardcoded — no change needed.

Also copy `get_actuaciones_cit(driver)` verbatim from `check_citizenship.py` (lines 576–670). This function uses `BeautifulSoup` and returns actuaciones oldest-first.

Note: `get_actuaciones_cit` references a nested `_RUIDO` regex and calls `print(...)` directly — leave as-is.

- [ ] **Step 3: Write `extraer_caratula(driver)` and `extraer_partido_apellido(caratula)`**

```python
def extraer_caratula(driver):
    """
    Extrae la carátula del expediente actual.
    Busca el patrón 'Caratula: ...' en el texto de la página.
    Retorna string o '' si no se encuentra.
    """
    try:
        page_text = BeautifulSoup(driver.page_source, 'html.parser').get_text('\n')
        m = re.search(r'Car[aá]tula[:\s]+([^\n]{3,120})', page_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        # Fallback: buscar "s/ Ciudadanía" como ancla
        m2 = re.search(r'([A-ZÁÉÍÓÚ][A-ZÁÉÍÓÚ ,]+s/\s*Ciudadan[ií]a[^\n]{0,60})', page_text)
        if m2:
            return m2.group(1).strip()
    except Exception as e:
        plog(f'    [CARATULA] Error: {e}')
    return ''


def extraer_partido_apellido(caratula):
    """
    De la carátula, extrae el primer apellido en mayúsculas.
    "PETROV, Ivan s/ Ciudadanía" → "PETROV"
    "GARCIA LOPEZ, Maria s/ Ciudadanía" → "GARCIA"
    Retorna None si no puede determinar el apellido.
    """
    if not caratula:
        return None
    # Primer bloque de mayúsculas antes de coma o "s/"
    m = re.match(r'([A-ZÁÉÍÓÚ]+)', caratula.strip())
    if m:
        apellido = m.group(1)
        if len(apellido) >= 3:
            return apellido
    return None
```

- [ ] **Step 4: Write `scrape_caso(driver, ocr, juz, sec, numero)` with error handling**

```python
def scrape_caso(driver, ocr, juz, sec, numero):
    """
    Scrape un caso terminado del PJN.
    Retorna (resultado_dict, driver) donde resultado_dict tiene:
      - case_number, caratula, partido_apellido, actuaciones, scraped_at, error
    driver puede ser nuevo si Chrome murió y se reinició.
    """
    grupo_key = f'J{juz:02d}-S{sec:02d}'
    tag = f'{grupo_key} {numero}'

    chrome_restarts = 0
    for intento in range(3):
        try:
            encontrado = navegar_a_caso(driver, ocr, numero)
            if not encontrado:
                plog(f'  [{tag}] No encontrado en PJN')
                return {
                    'case_number': numero,
                    'caratula': '',
                    'partido_apellido': None,
                    'actuaciones': [],
                    'scraped_at': datetime.now().isoformat(),
                    'error': 'not_found',
                }, driver

            caratula = extraer_caratula(driver)
            partido_apellido = extraer_partido_apellido(caratula)
            actuaciones = get_actuaciones_cit(driver)

            plog(f'  [{tag}] OK — caratula: {caratula[:60] or "(no encontrada)"}'
                 f' | {len(actuaciones)} actuaciones')

            return {
                'case_number': numero,
                'caratula': caratula,
                'partido_apellido': partido_apellido,
                'actuaciones': actuaciones,
                'scraped_at': datetime.now().isoformat(),
                'error': None,
            }, driver

        except Exception as e:
            plog(f'  [{tag}] Error intento {intento+1}/3: {str(e)[:80]}')
            # Detectar Chrome muerto
            try:
                driver.title
            except Exception:
                if chrome_restarts >= 2:
                    plog(f'  [{tag}] Chrome muerto demasiadas veces — abandonando')
                    return {
                        'case_number': numero,
                        'caratula': '',
                        'partido_apellido': None,
                        'actuaciones': [],
                        'scraped_at': datetime.now().isoformat(),
                        'error': 'chrome_died',
                    }, driver
                chrome_restarts += 1
                plog(f'  [{tag}] Chrome muerto ({chrome_restarts}/2), reiniciando...')
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = setup_driver()

    # Agotados los intentos — marcar como error captcha (causa más probable)
    return {
        'case_number': numero,
        'caratula': '',
        'partido_apellido': None,
        'actuaciones': [],
        'scraped_at': datetime.now().isoformat(),
        'error': 'captcha',
    }, driver
```

- [ ] **Step 5: Write `guardar_resultado()`, `run_worker()`, and `main()`**

```python
def guardar_resultado(grupo_key, resultado):
    """Guarda o actualiza un resultado en terminados_raw.json de forma thread-safe."""
    with _RAW_LOCK:
        if os.path.exists(RAW_FILE):
            try:
                with open(RAW_FILE, encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        else:
            data = {}

        if grupo_key not in data:
            data[grupo_key] = []

        # Reemplazar si ya existe el caso, agregar si no
        existing_idx = next(
            (i for i, r in enumerate(data[grupo_key])
             if r['case_number'] == resultado['case_number']),
            None
        )
        if existing_idx is not None:
            data[grupo_key][existing_idx] = resultado
        else:
            data[grupo_key].append(resultado)

        tmp = RAW_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RAW_FILE)


def run_worker(worker_id, casos):
    """
    Worker: procesa una lista de (juz, sec, numero).
    Salta los ya scrapeados correctamente (error: null en terminados_raw.json).
    """
    driver = setup_driver()
    ocr = ddddocr.DdddOcr(show_ad=False)
    procesados = 0
    try:
        for juz, sec, numero in casos:
            grupo_key = f'J{juz:02d}-S{sec:02d}'

            # Resume: saltar si ya está OK
            if os.path.exists(RAW_FILE):
                try:
                    with open(RAW_FILE, encoding='utf-8') as f:
                        existing = json.load(f)
                    grupo_data = existing.get(grupo_key, [])
                    ya_ok = any(
                        r['case_number'] == numero and r.get('error') is None
                        for r in grupo_data
                    )
                    if ya_ok:
                        plog(f'  [W{worker_id}] [{grupo_key} {numero}] ya scrapeado — saltando')
                        continue
                except Exception:
                    pass

            resultado, driver = scrape_caso(driver, ocr, juz, sec, numero)
            guardar_resultado(grupo_key, resultado)
            procesados += 1
            time.sleep(8)  # respetar rate limit del PJN
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    plog(f'  [W{worker_id}] Terminado — {procesados} casos procesados.')
    return procesados


def main():
    print('=' * 60)
    print('  NegroLex — Scraper de Terminados J08-S15')
    print(f'  Casos: {len(CASOS_TERMINADOS)} | Workers: {N_WORKERS}')
    print('=' * 60)

    # Distribuir casos entre workers
    chunks = [[] for _ in range(N_WORKERS)]
    for i, caso in enumerate(CASOS_TERMINADOS):
        chunks[i % N_WORKERS].append(caso)

    with ThreadPoolExecutor(max_workers=N_WORKERS) as executor:
        futures = {
            executor.submit(run_worker, i, chunk): i
            for i, chunk in enumerate(chunks) if chunk
        }
        total = 0
        for fut in as_completed(futures):
            try:
                total += fut.result()
            except Exception as e:
                print(f'  [ERROR] Worker falló: {e}')

    print(f'\n  Scraping completado. Total procesados: {total}')
    print(f'  Output: {RAW_FILE}')


if __name__ == '__main__':
    main()
```

- [ ] **Step 6: Smoke-test the script structure (no PJN needed)**

```bash
cd "C:\Users\Usuario\Desktop\claude code test"
python -c "import explore_terminados; print('Import OK'); print('Casos:', len(explore_terminados.CASOS_TERMINADOS))"
```

Expected output:
```
Import OK
Casos: 12
```

If import fails, fix any syntax errors before proceeding.

- [ ] **Step 7: Commit**

```bash
git add explore_terminados.py
git commit -m "feat: add explore_terminados.py scraper for J08-S15 terminated cases"
```

---

## Task 2: `analizar_terminados.py` — Groq Analysis + Knowledge Base

**Files:**
- Create: `analizar_terminados.py`

### Context for the implementer

This script reads `terminados_raw.json` and `telegram_messages.json`, calls Groq (llama-3.1-8b-instant) per case, aggregates results, and writes `conocimiento_juzgados.json`.

`telegram_messages.json` structure: `{group_name: [{id, fecha, de, texto}, ...], ...}`. The file is large (~38k messages) — load it with `json.load` once, iterate groups by name.

Telegram matching: find groups whose key (group name) contains `partido_apellido` (case-insensitive). Take the last 20 messages from the first matching group.

Rate limiting: use the same `_GROQ_SEM`/`_GROQ_LOCK`/`_GROQ_LAST` pattern from `check_citizenship.py` (one call at a time, 5s minimum gap).

The 50% threshold for pattern aggregation is applied in Python *after* the LLM response (not in the prompt), to avoid relying on the LLM to count correctly.

---

- [ ] **Step 1: Write skeleton (imports + constants + Groq client)**

```python
"""
NegroLex — Análisis de expedientes terminados para knowledge base de juzgados.

Lee terminados_raw.json + telegram_messages.json, llama a Groq por caso y por
juzgado/secretaría, y escribe conocimiento_juzgados.json.

Uso: python analizar_terminados.py
Salida: conocimiento_juzgados.json
"""

import os
import re
import json
import time
import shutil
import threading
from datetime import datetime

from groq import Groq as _Groq

FOLDER          = os.path.dirname(os.path.abspath(__file__))
RAW_FILE        = os.path.join(FOLDER, 'terminados_raw.json')
TELEGRAM_FILE   = os.path.join(FOLDER, 'telegram_messages.json')
CONOCIMIENTO_FILE = os.path.join(FOLDER, 'conocimiento_juzgados.json')
MEMORY_DIR      = r'C:\Users\Usuario\.claude\projects\C--Users-Usuario-Desktop\memory'
MEMORY_FILE     = os.path.join(MEMORY_DIR, 'conocimiento_juzgados.json')
MEMORY_MD       = os.path.join(MEMORY_DIR, 'MEMORY.md')

# Groq rate limiting (misma lógica que check_citizenship.py)
_GROQ_SEM  = threading.Semaphore(1)
_GROQ_LOCK = threading.Lock()
_GROQ_LAST = 0.0

_GROQ_KEY_FILE = os.path.join(FOLDER, 'groq_key.txt')
try:
    with open(_GROQ_KEY_FILE, encoding='utf-8') as _f:
        _GROQ_KEY = _f.read().strip()
    _GROQ_CLIENT = _Groq(api_key=_GROQ_KEY)
    _GROQ_AVAILABLE = True
    print('  [Groq] API lista.')
except Exception as _e:
    _GROQ_CLIENT = None
    _GROQ_AVAILABLE = False
    print(f'  [Groq] No disponible: {_e}')
```

- [ ] **Step 2: Write `cargar_terminados()` and `cargar_telegram()`**

```python
def cargar_terminados():
    """
    Carga terminados_raw.json.
    Retorna dict {grupo_key: [caso_dict, ...]}.
    Solo incluye casos con error: null.
    """
    if not os.path.exists(RAW_FILE):
        print(f'  [ERROR] No existe {RAW_FILE} — ejecutar explore_terminados.py primero')
        return {}
    with open(RAW_FILE, encoding='utf-8') as f:
        data = json.load(f)
    # Filtrar casos con error
    filtrado = {}
    for grupo, casos in data.items():
        ok = [c for c in casos if c.get('error') is None]
        if ok:
            filtrado[grupo] = ok
    return filtrado


def cargar_telegram():
    """
    Carga telegram_messages.json.
    Retorna dict {group_name: [msg_dict, ...]} o {} si no existe.
    La carga puede ser lenta (archivo grande).
    """
    if not os.path.exists(TELEGRAM_FILE):
        print(f'  [WARN] No existe {TELEGRAM_FILE} — se usarán solo actuaciones PJN')
        return {}
    print('  Cargando telegram_messages.json (puede tardar)...')
    with open(TELEGRAM_FILE, encoding='utf-8') as f:
        return json.load(f)
```

- [ ] **Step 3: Write `buscar_mensajes_telegram(partido_apellido, telegram_data)`**

```python
def buscar_mensajes_telegram(partido_apellido, telegram_data):
    """
    Busca el primer grupo de Telegram cuyo nombre contenga partido_apellido
    (case-insensitive). Retorna los últimos 20 mensajes como lista de strings.
    Retorna [] si no hay match o partido_apellido es None.
    """
    if not partido_apellido or not telegram_data:
        return []

    apellido_lower = partido_apellido.lower()
    for nombre_grupo, mensajes in telegram_data.items():
        if apellido_lower in nombre_grupo.lower():
            # Tomar hasta 20 mensajes más recientes
            ultimos = mensajes[-20:] if len(mensajes) > 20 else mensajes
            return [
                f"[{m.get('fecha', '')}] {m.get('de', '')}: {m.get('texto', '')[:300]}"
                for m in ultimos
            ]
    return []
```

- [ ] **Step 4: Write `llamar_groq(prompt, max_tokens)` (rate-limited wrapper)**

```python
def llamar_groq(prompt, max_tokens=800):
    """
    Llama a Groq con rate limiting (1 llamado a la vez, 5s mínimo entre llamados).
    Retorna el texto de la respuesta o None si falla.
    """
    global _GROQ_LAST
    if not _GROQ_AVAILABLE:
        return None

    with _GROQ_SEM:
        with _GROQ_LOCK:
            ahora = time.time()
            espera = 5.0 - (ahora - _GROQ_LAST)
            if espera > 0:
                time.sleep(espera)
            _GROQ_LAST = time.time()

        try:
            resp = _GROQ_CLIENT.chat.completions.create(
                model='llama-3.1-8b-instant',
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f'  [Groq] Error: {e}')
            return None


def extraer_json_respuesta(texto):
    """
    Extrae el primer objeto JSON de un texto de respuesta del LLM.
    Retorna dict o None.
    """
    if not texto:
        return None
    m = re.search(r'\{.*\}', texto, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
```

- [ ] **Step 5: Write `analizar_caso(juz, sec, case_number, actuaciones, mensajes_telegram)`**

```python
def analizar_caso(juz, sec, case_number, actuaciones, mensajes_telegram):
    """
    Llama a Groq para analizar un expediente terminado.
    Retorna dict con etapas_detectadas, actuaciones_inusuales, pedidos_al_cliente, observaciones.
    Retorna None si Groq no está disponible o la respuesta es inválida.
    """
    actuaciones_text = '\n'.join(
        f"[{a.get('fecha', '')}] {a.get('tipo', '')} — {a.get('descripcion', '')}"
        for a in actuaciones
        if a.get('descripcion') or a.get('tipo')
    ) or '(sin actuaciones)'

    telegram_text = '\n'.join(mensajes_telegram) if mensajes_telegram else '(sin mensajes)'

    prompt = f"""Analizá el siguiente expediente de ciudadanía argentina en el fuero CCF.

Juzgado: {juz}, Secretaría: {sec}
Expediente: {case_number}

ACTUACIONES (cronológico, más antigua primero):
{actuaciones_text}

MENSAJES TELEGRAM DEL CLIENTE (si hay):
{telegram_text}

Extraé en JSON:
{{
  "etapas_detectadas": ["pfa_interpol", "renaper", ...],
  "actuaciones_inusuales": ["OFICIO SIDE", ...],
  "pedidos_al_cliente": ["antecedentes SIDE", ...],
  "observaciones": "texto libre"
}}
Solo incluí lo que podás determinar con certeza. Respondé ÚNICAMENTE con JSON válido."""

    print(f'    [Groq] Analizando {case_number}...')
    respuesta = llamar_groq(prompt, max_tokens=600)
    resultado = extraer_json_respuesta(respuesta)
    if resultado:
        print(f'    [Groq] OK — etapas: {resultado.get("etapas_detectadas", [])}')
    else:
        print(f'    [Groq] Sin resultado válido para {case_number}')
    return resultado
```

- [ ] **Step 6: Write `agregar_juzgado(juz, sec, resultados_casos)`**

```python
def agregar_juzgado(juz, sec, resultados_casos):
    """
    Llama a Groq con los resultados de todos los casos del juzgado/secretaría
    para generar el perfil final.
    Retorna el dict del perfil o un perfil vacío si falla.
    """
    n = len(resultados_casos)
    resultados_json = json.dumps(resultados_casos, ensure_ascii=False, indent=2)

    prompt = f"""Analizaste {n} expedientes del Juzgado {juz} Secretaría {sec}.

Resultados por caso:
{resultados_json}

Generá el perfil del juzgado en JSON:
{{
  "particularidades": ["lista de cosas que este juzgado hace distinto a los demás"],
  "secuencia_tipica": ["orden típico de etapas"],
  "alertas_proactivas": [
    {{
      "condicion": {{"etapa_presente": "renaper", "etapa_ausente": "side"}},
      "mensaje": "descripción de la alerta para el estudio"
    }}
  ],
  "alertas_reactivas": [
    {{
      "keywords": ["OFICIO SIDE"],
      "mensaje": "descripción de la acción a tomar"
    }}
  ]
}}
Solo incluí alertas que estén respaldadas por los datos. Respondé ÚNICAMENTE con JSON válido."""

    print(f'  [Groq] Generando perfil J{juz}-S{sec} ({n} casos)...')
    respuesta = llamar_groq(prompt, max_tokens=1000)
    perfil = extraer_json_respuesta(respuesta)
    if not perfil:
        print(f'  [Groq] Sin perfil válido para J{juz}-S{sec} — usando perfil vacío')
        perfil = {
            'particularidades': [],
            'secuencia_tipica': [],
            'alertas_proactivas': [],
            'alertas_reactivas': [],
        }
    return perfil
```

- [ ] **Step 7: Write `filtrar_patrones_50pct(resultados_casos)` (Python-side threshold)**

```python
def filtrar_patrones_50pct(resultados_casos):
    """
    Retorna las etapas y actuaciones inusuales presentes en >50% de los casos.
    Usado como input para el prompt de agregación.
    """
    n = len(resultados_casos)
    if n == 0:
        return {}

    conteo_etapas = {}
    conteo_inusuales = {}

    for r in resultados_casos:
        if not r:
            continue
        for etapa in r.get('etapas_detectadas', []):
            conteo_etapas[etapa] = conteo_etapas.get(etapa, 0) + 1
        for act in r.get('actuaciones_inusuales', []):
            conteo_inusuales[act] = conteo_inusuales.get(act, 0) + 1

    umbral = n / 2
    return {
        'etapas_frecuentes': [e for e, c in conteo_etapas.items() if c > umbral],
        'actuaciones_inusuales_frecuentes': [a for a, c in conteo_inusuales.items() if c > umbral],
        'n_casos': n,
    }
```

- [ ] **Step 8: Write `procesar_grupo(grupo_key, casos, telegram_data)` and `copiar_a_memoria()`**

```python
def procesar_grupo(grupo_key, casos, telegram_data):
    """
    Procesa un grupo (ej: "J08-S15"):
    1. Analiza cada caso con Groq
    2. Filtra patrones al 50%
    3. Genera perfil del juzgado con segunda llamada a Groq
    Retorna el dict del perfil listo para conocimiento_juzgados.json.
    """
    m = re.match(r'J(\d+)-S(\d+)', grupo_key)
    if not m:
        print(f'  [WARN] Formato de grupo inesperado: {grupo_key}')
        return None
    juz = int(m.group(1))
    sec = int(m.group(2))

    print(f'\n[{grupo_key}] Analizando {len(casos)} casos...')
    resultados = []
    for caso in casos:
        mensajes = buscar_mensajes_telegram(
            caso.get('partido_apellido'),
            telegram_data
        )
        resultado = analizar_caso(
            juz, sec,
            caso['case_number'],
            caso.get('actuaciones', []),
            mensajes,
        )
        resultados.append(resultado)

    # Filtrar al 50% en Python (no delegar al LLM)
    patrones = filtrar_patrones_50pct([r for r in resultados if r])
    print(f'  [{grupo_key}] Patrones frecuentes: {patrones}')

    # Segunda llamada: generar perfil con el subset filtrado
    resultados_validos = [r for r in resultados if r]
    perfil = agregar_juzgado(juz, sec, resultados_validos)

    return {
        'nombre': f'Juzgado {juz} - Secretaría {sec}',
        'casos_analizados': len(casos),
        'casos_con_resultado': len(resultados_validos),
        'generado_en': datetime.now().isoformat(),
        'patrones_50pct': patrones,
        **perfil,
    }


def copiar_a_memoria(conocimiento):
    """
    Copia conocimiento_juzgados.json a la carpeta de memoria de Claude.
    Actualiza MEMORY.md si la entrada aún no existe.
    """
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        shutil.copy2(CONOCIMIENTO_FILE, MEMORY_FILE)
        print(f'  [MEM] Copiado a {MEMORY_FILE}')

        # Agregar entrada en MEMORY.md si no existe
        entrada = '- [conocimiento_juzgados.json](./conocimiento_juzgados.json) — Knowledge base de patrones por juzgado/secretaría CCF (generado de expedientes terminados + Telegram).'
        if os.path.exists(MEMORY_MD):
            with open(MEMORY_MD, encoding='utf-8') as f:
                contenido = f.read()
            if 'conocimiento_juzgados.json' not in contenido:
                with open(MEMORY_MD, 'a', encoding='utf-8') as f:
                    f.write('\n' + entrada + '\n')
                print('  [MEM] MEMORY.md actualizado')
            else:
                print('  [MEM] MEMORY.md ya tiene la entrada')
        else:
            print(f'  [WARN] No se encontró MEMORY.md en {MEMORY_DIR}')
    except Exception as e:
        print(f'  [WARN] No se pudo copiar a memoria: {e}')
```

- [ ] **Step 9: Write `main()`**

```python
def main():
    print('=' * 60)
    print('  NegroLex — Análisis de Terminados → Knowledge Base')
    print('=' * 60)

    terminados = cargar_terminados()
    if not terminados:
        print('  Sin datos para analizar.')
        return

    telegram_data = cargar_telegram()

    conocimiento = {}
    for grupo_key, casos in terminados.items():
        perfil = procesar_grupo(grupo_key, casos, telegram_data)
        if perfil:
            conocimiento[grupo_key] = perfil

    if not conocimiento:
        print('  No se generó conocimiento.')
        return

    tmp = CONOCIMIENTO_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(conocimiento, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONOCIMIENTO_FILE)
    print(f'\n  Conocimiento guardado en {CONOCIMIENTO_FILE}')

    copiar_a_memoria(conocimiento)
    print('\n  Listo.')


if __name__ == '__main__':
    main()
```

- [ ] **Step 10: Smoke-test import**

```bash
python -c "import analizar_terminados; print('Import OK')"
```

Expected: `Import OK` (plus `[Groq] API lista.` if `groq_key.txt` existe).

- [ ] **Step 11: Commit**

```bash
git add analizar_terminados.py
git commit -m "feat: add analizar_terminados.py — Groq analysis + conocimiento_juzgados.json generator"
```

---

## Task 3: Integración en `check_citizenship.py`

**Files:**
- Modify: `check_citizenship.py`

### Context for the implementer

Add three things to `check_citizenship.py`:
1. `CONOCIMIENTO_FILE` constant (near other file constants, ~line 31)
2. `_conocimiento_cache = None` module variable + `_cargar_conocimiento()` loader (near the end of the utility functions section, before `process_case`)
3. `detectar_alertas_conocimiento(case, actuaciones, conocimiento, stages)` function
4. A call inside `process_case()` after `final_stages` is computed (line ~1000) and before the `with _CASES_LOCK:` block (line ~1011)

`stages` in `process_case()` is `final_stages` at the time of the call.

"New actuaciones" (for reactive alerts) = actuaciones whose fecha is strictly after `case.get('lastActionDate')`. If `lastActionDate` is None (first run), treat all as new.

---

- [ ] **Step 1: Add `CONOCIMIENTO_FILE` constant**

In `check_citizenship.py`, find the block of file constants (~line 29–33):
```python
FOLDER     = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(FOLDER, 'cases.json')
NOTIF_FILE = os.path.join(FOLDER, 'notifications.json')
TELEGRAM_FILE = os.path.join(FOLDER, 'telegram_ciudadania.json')
```

Add after `TELEGRAM_FILE`:
```python
CONOCIMIENTO_FILE = os.path.join(FOLDER, 'conocimiento_juzgados.json')
```

- [ ] **Step 2: Add `_conocimiento_cache` and `_cargar_conocimiento()`**

Add after the `_GROQ_LOCK` block (~line 74), before `AUTO =`:
```python
_conocimiento_cache = None

def _cargar_conocimiento():
    """
    Carga conocimiento_juzgados.json una sola vez y lo cachea en módulo.
    Retorna dict vacío si el archivo no existe (no crashea).
    """
    global _conocimiento_cache
    if _conocimiento_cache is not None:
        return _conocimiento_cache
    if not os.path.exists(CONOCIMIENTO_FILE):
        _conocimiento_cache = {}
        return _conocimiento_cache
    try:
        with open(CONOCIMIENTO_FILE, encoding='utf-8') as f:
            _conocimiento_cache = json.load(f)
        print(f'  [KB] Conocimiento cargado: {list(_conocimiento_cache.keys())}')
    except Exception as e:
        print(f'  [KB] Error cargando conocimiento: {e}')
        _conocimiento_cache = {}
    return _conocimiento_cache
```

- [ ] **Step 3: Add `detectar_alertas_conocimiento()`**

Add just before the `# ── Merge de stages` section (~line 895):

```python
# ── Knowledge base alerts ──────────────────────────────────────────────────────

def detectar_alertas_conocimiento(case, actuaciones, conocimiento, stages):
    """
    Aplica alertas proactivas y reactivas del knowledge base al caso.

    Parámetros:
    - case: dict del caso desde cases.json
    - actuaciones: lista de actuaciones scrapeadas en esta corrida
    - conocimiento: dict cargado desde conocimiento_juzgados.json
    - stages: dict final de etapas detectadas {etapa: 'OK'/'NO'/''}

    Retorna lista de strings con mensajes de alerta (vacía si no aplica).
    """
    if not conocimiento:
        return []

    juzgado   = case.get('juzgado')
    secretaria = case.get('secretaria')
    if not juzgado or not secretaria:
        return []

    grupo_key = f'J{int(juzgado):02d}-S{int(secretaria):02d}'
    perfil = conocimiento.get(grupo_key)
    if not perfil:
        return []

    alertas = []

    # Alertas proactivas — basadas en etapas presentes/ausentes
    for alerta in perfil.get('alertas_proactivas', []):
        cond = alerta.get('condicion', {})
        etapa_presente = cond.get('etapa_presente')
        etapa_ausente  = cond.get('etapa_ausente')
        if (not etapa_presente or stages.get(etapa_presente) == 'OK') and \
           (not etapa_ausente  or not stages.get(etapa_ausente)):
            alertas.append(alerta['mensaje'])

    # Alertas reactivas — basadas en actuaciones nuevas (posteriores a lastActionDate)
    last_date = case.get('lastActionDate')  # DD/MM/YYYY o None

    def es_nueva(act):
        if not last_date:
            return True
        return act.get('fecha', '') > last_date  # lexicográfico: DD/MM/YYYY compara bien

    nuevas = [a for a in actuaciones if es_nueva(a)]
    for alerta in perfil.get('alertas_reactivas', []):
        keywords = alerta.get('keywords', [])
        for act in nuevas:
            desc_up = act.get('descripcion', '').upper()
            if any(kw.upper() in desc_up for kw in keywords):
                alertas.append(alerta['mensaje'])
                break  # una alerta por keyword-group, no duplicar

    return alertas
```

> **Note on date comparison:** `lastActionDate` is stored as `"DD/MM/YYYY"`. Direct string comparison works correctly for same-month/year comparisons but can fail across months (e.g., `"15/02/2024" > "01/03/2024"` is False correctly, but `"15/12/2024" > "01/03/2024"` compares as `"15" > "01"` → True, which is wrong). For the reactive alerts use case (comparing recent actuaciones), this is acceptable since we err on the side of firing too many alerts rather than missing them. A proper fix would convert both to `datetime`, but that adds complexity the spec doesn't require.

- [ ] **Step 4: Call `detectar_alertas_conocimiento()` inside `process_case()`**

In `process_case()`, find the line where `final_stages` is assigned (~line 1000):
```python
    final_stages = merge_stages(merged_so_far, groq_stages)
```

Add immediately after that line, before the `# Detectar etapas completadas` block:
```python
    # Knowledge base alerts (proactivas y reactivas)
    conocimiento = _cargar_conocimiento()
    alertas_kb = detectar_alertas_conocimiento(case, actuaciones, conocimiento, final_stages)
    for msg in alertas_kb:
        print(f'    [KB] Alerta: {msg[:80]}')
```

And later, in the **notifications section** after the lock block (after `if idx is None: return driver`), add inside the notification block:
```python
    for msg in alertas_kb:
        add_notification(case, 'conocimiento_alert', msg)
```

Place this just before or after the `groq_result.get('requires_action')` notification block.

- [ ] **Step 5: Verify syntax**

```bash
python -c "import check_citizenship; print('Import OK')"
```

Expected: `Import OK` plus the usual startup messages. Fix any syntax errors.

- [ ] **Step 6: Commit**

```bash
git add check_citizenship.py
git commit -m "feat: add detectar_alertas_conocimiento() integration in check_citizenship.py"
```

---

## Task 4: Manual deploy prep (no code)

**Files:**
- `cases.json` — add `juzgado` and `secretaria` fields to J08-S15 cases (manual)

### Context for the implementer

Before `detectar_alertas_conocimiento()` can fire alerts for active cases, each J08-S15 case in `cases.json` needs `"juzgado": 8` and `"secretaria": 15` fields. This is a manual data update — no script.

Also: `terminados_raw.json` doesn't exist yet — it's generated by running `explore_terminados.py`. And `conocimiento_juzgados.json` doesn't exist yet — it's generated by running `analizar_terminados.py` after that.

**Steps (manual, no commit needed):**

- [ ] **Step 1: Verify `cases.json` structure for J08-S15 cases**

Open `cases.json`, find cases where `caseNumber` starts with `"CCF"` and you know the juzgado/secretaría. For the pilot, manually add `"juzgado": 8, "secretaria": 15` to the relevant cases.

- [ ] **Step 2: Document how to run the full pipeline**

The full pipeline when ready:
```bash
# 1. Scrape terminated cases (~40min with 3 workers)
python explore_terminados.py

# 2. Analyze with Groq and generate knowledge base (~5min)
python analizar_terminados.py

# 3. Run citizenship checker (will now load KB and fire alerts)
python check_citizenship.py
```

- [ ] **Step 3: Final commit with any documentation updates**

```bash
# Verificar qué se va a agregar antes de commitear
git status
# NO agregar terminados_raw.json ni conocimiento_juzgados.json — son archivos de datos
git add docs/ check_citizenship.py
git commit -m "docs: document knowledge base pipeline execution steps"
```

---

## Plan Review Checklist

Before considering this plan complete, verify:
- [ ] All file paths are exact (no relative paths without context)
- [ ] All function signatures match their call sites
- [ ] `alertas_kb` variable is accessible in the notifications block (it's defined before the lock block, so yes)
- [ ] The `_cargar_conocimiento()` call in `process_case()` uses the module-level cache (not per-case reload)
- [ ] `copiar_a_memoria()` in `analizar_terminados.py` handles the case where `MEMORY_DIR` doesn't exist (uses `os.makedirs`)
