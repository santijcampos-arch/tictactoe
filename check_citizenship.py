"""
NegroLex — PJN Citizenship Checker
Consulta expedientes de ciudadanía en scw.pjn.gov.ar con Selenium + ddddocr.
Requiere: pip install selenium ddddocr beautifulsoup4 groq
"""

import os
import re
import sys
import json
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from groq import Groq as _Groq
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup
import ddddocr

FOLDER     = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(FOLDER, 'cases.json')
NOTIF_FILE = os.path.join(FOLDER, 'notifications.json')
TELEGRAM_FILE = os.path.join(FOLDER, 'telegram_ciudadania.json')
PJN_URL    = 'https://scw.pjn.gov.ar/scw/home.seam'

N_WORKERS           = 1   # UN Chrome a la vez
MAX_CHROME_INSTANCES = 3

JURISDICTION_CODES = {
    'CSJ': '0',  'CIV': '1',  'CAF': '2',  'CCF': '3',
    'CNE': '4',  'CSS': '5',  'CPE': '6',  'CNT': '7',
    'CFP': '8',  'CCC': '9',  'COM': '10', 'CPF': '11',
    'CPN': '12', 'FBB': '13', 'FCR': '14', 'FCB': '15',
    'FCT': '16', 'FGR': '17', 'FLP': '18', 'FMP': '19',
    'FMZ': '20', 'FPO': '21', 'FPA': '22', 'FRE': '23',
    'FSA': '24', 'FRO': '25', 'FSM': '26', 'FTU': '27',
}

CITIZENSHIP_STAGES = [
    ('pfa_interpol',   'PFA INTERPOL'),
    ('renaper',        'RENAPER'),
    ('cne',            'CNE'),
    ('reincidencia',   'REINCIDENCIA'),
    ('dnm',            'DNM'),
    ('pfa_dactilo',    'PFA DACTILO'),
    ('edicto',         'EDICTO'),
    ('pfa_convenio',   'PFA CONVENIO'),
    ('medios_de_vida', 'MEDIOS DE VIDA'),
    ('fiscal',         'FISCAL'),
    ('sentencia',      'SENTENCIA'),
    ('carta_ciudadania', 'CARTA CIUDADANÍA'),
]

_CASES_FILE_LOCK_PATH = CASES_FILE + '.lock'
_NOTIF_FILE_LOCK_PATH = NOTIF_FILE + '.lock'

_ACTIVE_DRIVERS      = 0
_ACTIVE_DRIVERS_LOCK = threading.Lock()
_CASES_LOCK          = threading.Lock()
_NOTIF_LOCK          = threading.Lock()

# Groq rate limiting (same pattern as check_pjn.py)
_GROQ_SEM  = threading.Semaphore(1)
_GROQ_LAST = 0.0
_GROQ_LOCK = threading.Lock()

AUTO  = '--auto'  in sys.argv
LIMIT = None
for _i, _arg in enumerate(sys.argv):
    if _arg == '--limit' and _i + 1 < len(sys.argv):
        LIMIT = int(sys.argv[_i + 1])

# ── Groq client ───────────────────────────────────────────────────────────────

_GROQ_KEY_FILE = os.path.join(FOLDER, 'groq_key.txt')
try:
    with open(_GROQ_KEY_FILE, encoding='utf-8') as _f:
        _GROQ_KEY = _f.read().strip()
    _GROQ_CLIENT = _Groq(api_key=_GROQ_KEY)
    _GROQ_AVAILABLE = True
    print("  [Groq] API lista.")
except Exception as _e:
    _GROQ_CLIENT = None
    _GROQ_AVAILABLE = False
    print(f"  [Groq] No disponible: {_e}")

# ── Shared infrastructure ─────────────────────────────────────────────────────

def _acquire_file_lock(lock_path, timeout=10):
    """Lock cross-process via os.O_EXCL (atómico en Windows)."""
    start = time.time()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - start > timeout:
                return False
            time.sleep(0.05)

def _release_file_lock(lock_path):
    try:
        os.unlink(lock_path)
    except OSError:
        pass

def _atomic_write_json(path, data):
    """Escribe data en path de forma atómica via archivo temporal."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def setup_driver():
    global _ACTIVE_DRIVERS
    with _ACTIVE_DRIVERS_LOCK:
        if _ACTIVE_DRIVERS >= MAX_CHROME_INSTANCES:
            print(f'\n[FATAL] Se detectaron {_ACTIVE_DRIVERS} instancias de Chrome abiertas. '
                  f'Límite de {MAX_CHROME_INSTANCES} superado. Terminando programa.')
            os._exit(1)
        _ACTIVE_DRIVERS += 1
    opts = webdriver.ChromeOptions()
    opts.add_argument('--window-position=-32000,-32000')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',
        {'source': "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"})
    return driver

def quit_driver(driver):
    global _ACTIVE_DRIVERS
    try:
        driver.quit()
    except Exception:
        pass
    with _ACTIVE_DRIVERS_LOCK:
        _ACTIVE_DRIVERS = max(0, _ACTIVE_DRIVERS - 1)

def wait_for(driver, timeout, condition):
    try:
        return WebDriverWait(driver, timeout).until(condition)
    except TimeoutException:
        return None

def read_captcha(ocr, png_bytes):
    """Lee los 4 dígitos del captcha. Retorna string de 4 dígitos o None."""
    result = ocr.classification(png_bytes)
    digits = re.sub(r'\D', '', result)
    if len(digits) == 4:
        print(f"    [OCR] '{digits}'")
        return digits
    print(f"    [OCR] Leyó '{digits}' — reintentando...")
    return None

def solve_captcha(driver, ocr):
    """
    Flujo: esperar iframe → VER DESAFÍO → OCR → ingresar + Enter → esperar aprobación.
    Retorna True si el desafío fue aprobado.
    """
    try:
        WebDriverWait(driver, 10).until(lambda d: any(
            'captcha.pjn.gov.ar' in (f.get_attribute('src') or '')
            for f in d.find_elements(By.TAG_NAME, 'iframe')
        ))
    except TimeoutException:
        print("    No apareció el iframe del captcha.")
        return False

    captcha_frame = next(
        (f for f in driver.find_elements(By.TAG_NAME, 'iframe')
         if 'captcha.pjn.gov.ar' in (f.get_attribute('src') or '')),
        None
    )
    if not captcha_frame:
        return False

    driver.switch_to.frame(captcha_frame)
    w = WebDriverWait(driver, 10)

    try:
        btn = w.until(EC.element_to_be_clickable(
            (By.XPATH, "//*[contains(text(),'VER DESAF')]")
        ))
        driver.execute_script("arguments[0].click();", btn)
    except TimeoutException:
        print("    No apareció el botón VER DESAFÍO.")
        driver.switch_to.default_content()
        return False

    try:
        img_el = w.until(EC.visibility_of_element_located((By.TAG_NAME, 'img')))
    except TimeoutException:
        print("    No apareció la imagen del captcha.")
        driver.switch_to.default_content()
        return False

    captcha_text = read_captcha(ocr, img_el.screenshot_as_png)

    if not captcha_text:
        driver.switch_to.default_content()
        return False

    try:
        inp = driver.find_element(
            By.XPATH, "//input[@placeholder='Ingrese el texto aquí' or @type='text']"
        )
        inp.clear()
        inp.send_keys(captcha_text)
        inp.send_keys(Keys.RETURN)
    except NoSuchElementException:
        print("    No se encontró el campo del captcha.")
        driver.switch_to.default_content()
        return False

    try:
        WebDriverWait(driver, 20).until_not(
            EC.visibility_of_element_located((By.XPATH, "//*[contains(text(),'ENVIANDO')]"))
        )
    except TimeoutException:
        print("    Timeout esperando aprobación del captcha.")
        driver.switch_to.default_content()
        return False

    try:
        page_text = driver.find_element(By.TAG_NAME, 'body').text
        if re.search(r'incorrecto|error|inv[aá]lido', page_text, re.IGNORECASE):
            print("    Captcha rechazado por el servidor.")
            driver.switch_to.default_content()
            return False
    except Exception:
        pass

    print("    OK Desafio aprobado")
    driver.switch_to.default_content()
    return True

def add_notification(case, notif_type, message):
    """Agrega una notificación a notifications.json (thread-safe)."""
    with _NOTIF_LOCK:
        try:
            if os.path.exists(NOTIF_FILE):
                with open(NOTIF_FILE, encoding='utf-8') as f:
                    notifs = json.load(f)
            else:
                notifs = []
        except Exception:
            notifs = []

        notifs.insert(0, {
            'id':         datetime.now().strftime('%Y%m%d%H%M%S%f'),
            'timestamp':  datetime.now().isoformat(),
            'caseNumber': case.get('caseNumber', ''),
            'clientName': case.get('clientName', ''),
            'category':   case.get('category', ''),
            'tipoAccion': case.get('tipoAccion', ''),
            'type':       notif_type,
            'message':    message,
        })

        if _acquire_file_lock(_NOTIF_FILE_LOCK_PATH):
            try:
                _atomic_write_json(NOTIF_FILE, notifs)
            except Exception as e:
                print(f"    [NOTIF] Error guardando: {e}")
            finally:
                _release_file_lock(_NOTIF_FILE_LOCK_PATH)
        else:
            print(f"    [NOTIF] No se pudo adquirir lock — notificación perdida")

def es_dia_habil():
    """Retorna False si hoy es fin de semana o feriado nacional argentino."""
    hoy = datetime.now().date()

    if hoy.weekday() >= 5:
        return False

    FERIADOS = {
        # 2026
        (2026,  1,  1), (2026,  2, 16), (2026,  2, 17), (2026,  3, 23),
        (2026,  3, 24), (2026,  4,  2), (2026,  4,  3), (2026,  5,  1),
        (2026,  5, 25), (2026,  6, 15), (2026,  6, 17), (2026,  6, 19),
        (2026,  6, 20), (2026,  7,  9), (2026,  8, 17), (2026, 10, 12),
        (2026, 11, 20), (2026, 11, 23), (2026, 12,  8), (2026, 12, 25),
        # 2027
        (2027,  1,  1), (2027,  2,  8), (2027,  2,  9), (2027,  3, 24),
        (2027,  3, 26), (2027,  4,  2), (2027,  5,  1), (2027,  5, 25),
        (2027,  6, 17), (2027,  6, 20), (2027,  7,  9), (2027,  8, 16),
        (2027, 10, 11), (2027, 11, 22), (2027, 12,  8), (2027, 12, 25),
    }

    if (hoy.year, hoy.month, hoy.day) in FERIADOS:
        return False

    if hoy.year > 2027:
        print(f"  [ADVERTENCIA] Año {hoy.year} sin feriados cargados — actualizar FERIADOS en check_citizenship.py", file=sys.stderr)

    return True

# ── Parseo de expediente ──────────────────────────────────────────────────────

def parse_case_number(case_number):
    """'CAF 031904/2025' → ('2', '31904', '2025'). None si no reconoce."""
    m = re.match(r'([A-Z]+)\s+0*(\d+)/(\d{4})', case_number.strip())
    if not m:
        return None, None, None
    code = JURISDICTION_CODES.get(m.group(1))
    return code, m.group(2), m.group(3)


def parse_citizenship_case_number(case_number):
    """
    Extiende parse_case_number con fallback a CCF para números sin prefijo.
    '24590/2024' → ('3', '24590', '2024')
    'CCF 020934/2022' → ('3', '20934', '2022')
    """
    code, number, year = parse_case_number(case_number)
    if code:
        return code, number, year
    # Número puro sin prefijo: XXXXX/YYYY → default CCF
    m = re.match(r'0*(\d+)/(\d{4})', case_number.strip())
    if m:
        return JURISDICTION_CODES['CCF'], m.group(1), m.group(2)
    return None, None, None

# ── Extracción de actuaciones ─────────────────────────────────────────────────

def get_actuaciones_cit(driver):
    """
    Extrae TODAS las filas de la tabla de actuaciones del expediente actual.
    Retorna lista de dicts [{tipo, descripcion, fecha, oficina}] de más antigua a más reciente.
    No abre documentos — solo lee el texto de la tabla.
    """
    actuaciones = []
    try:
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        tables = soup.find_all('table')

        tabla = None
        for t in tables:
            txt = t.get_text(' ')
            if not re.search(r'\d{2}/\d{2}/\d{4}', txt):
                continue
            rows = t.find_all('tr')
            if len(rows) < 2:
                continue
            header = rows[0].get_text(' ').lower()
            if any(w in header for w in ['tipo', 'descripci', 'fecha', 'actu', 'descargar']):
                tabla = t
                break

        if tabla is None:
            print("    [ACT] No se encontró tabla de actuaciones")
            return actuaciones

        filas = tabla.find_all('tr')[1:]  # saltar encabezado
        for fila in filas:
            celdas = [td.get_text(' ', strip=True) for td in fila.find_all('td')]
            celdas = [c for c in celdas if c and c not in ('Descargar', 'Ver', 'Descargar Actuación')]
            if not celdas:
                continue

            fecha_m = next(
                (re.search(r'\d{2}/\d{2}/\d{4}', c) for c in celdas if re.search(r'\d{2}/\d{2}/\d{4}', c)),
                None
            )
            fecha = fecha_m.group(0) if fecha_m else ''

            def _val(c):
                return c.split(':\n', 1)[1].strip() if ':\n' in c else c.strip()

            tipo_cell    = next((c for c in celdas if re.match(r'tipo\s+actua', c, re.I)), '')
            detalle_cell = next((c for c in celdas if re.match(r'detalle', c, re.I)), '')
            oficina_cell = next((c for c in celdas if re.match(r'oficina', c, re.I)), '')

            tipo    = _val(tipo_cell)    if tipo_cell    else ''
            detalle = _val(detalle_cell) if detalle_cell else ''
            oficina = _val(oficina_cell) if oficina_cell else ''

            if tipo or detalle:
                descripcion = ' — '.join(filter(None, [tipo, detalle]))
            else:
                _RUIDO = re.compile(r'descargar|^\s*ver\s*$|^fecha:|^[A-Z]{2,4}\s*$', re.I)
                otros = [c for c in celdas if c and c != fecha
                         and not re.search(r'\d{2}/\d{2}/\d{4}', c)
                         and not _RUIDO.search(c)]
                descripcion = ' — '.join(filter(None, otros))

            if descripcion or fecha:
                actuaciones.append({
                    'fecha': fecha,
                    'tipo': tipo,
                    'descripcion': descripcion,
                    'oficina': oficina,
                })

        print(f"    [ACT] {len(actuaciones)} actuaciones extraídas")
    except Exception as e:
        print(f"    [ACT] Error: {e}")

    # Devolver de más antigua a más reciente (el PJN muestra más reciente primero)
    return list(reversed(actuaciones))

def query_citizenship_case(driver, case, ocr):
    """
    Navega al PJN, busca el expediente, extrae actuaciones.
    Retorna (actuaciones, pjn_last_date, driver) — driver puede ser reemplazado si Chrome muere.
    actuaciones = None si no se pudo consultar.
    """
    case_number = case.get('caseNumber', '')
    code, number, year = parse_citizenship_case_number(case_number)
    if not code:
        print(f"    Expediente no reconocido: {case_number}")
        return None, None, driver

    chrome_restarts = 0
    attempt = 0
    while attempt < 5:
        attempt += 1
        if attempt > 1:
            print(f"    Reintento {attempt}/5...")
        try:
            driver.get(PJN_URL)

            if not wait_for(driver, 15, EC.presence_of_element_located((By.ID, 'formPublica:camaraNumAni'))):
                print("    Página no cargó")
                continue

            Select(driver.find_element(By.ID, 'formPublica:camaraNumAni')).select_by_value(code)
            driver.find_element(By.ID, 'formPublica:numero').send_keys(number)
            driver.find_element(By.ID, 'formPublica:anio').send_keys(year)

            if not solve_captcha(driver, ocr):
                continue

            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(By.ID, 'formPublica:buscarPorNumeroButton')
            )

            if not wait_for(driver, 30, EC.presence_of_element_located(
                (By.XPATH, "//*[contains(text(),'Expediente:') or contains(text(),'Datos Generales')]")
            )):
                print("    Resultado no llegó")
                continue

            # Verificar que no diga "no encontrado"
            body_text = driver.find_element(By.TAG_NAME, 'body').text
            if re.search(r'no\s+se\s+encontr[oó]|sin\s+resultado', body_text, re.IGNORECASE):
                print(f"    Expediente no encontrado en PJN: {case_number}")
                return None, None, driver

            actuaciones = get_actuaciones_cit(driver)

            # Fecha de última actuación (primera en la lista original del PJN = más reciente)
            # actuaciones está ordenada de antigua a reciente, entonces la última es la más reciente
            pjn_last_date = actuaciones[-1]['fecha'] if actuaciones else None

            return actuaciones, pjn_last_date, driver

        except Exception as e:
            msg = str(e).split('\n')[0][:120]
            print(f"    Error: {msg}")
            try:
                driver.title
            except Exception:
                if chrome_restarts >= 2:
                    print("    Chrome muerto demasiadas veces, abandonando.")
                    return None, None, driver
                chrome_restarts += 1
                print(f"    Chrome muerto ({chrome_restarts}/2), reiniciando...")
                quit_driver(driver)
                driver = setup_driver()
            continue

    return None, None, driver

# ── Detección por keywords ────────────────────────────────────────────────────

# (pattern, stage_key, value)
# Se aplica sobre el campo 'descripcion' de cada actuación (case-insensitive).
# Solo patrones inequívocos — todo lo ambiguo va a Groq.
KEYWORD_PATTERNS = [
    (r'CONTESTACION\s+INTERPOL',              'pfa_interpol',   'OK'),
    (r'CONTESTACION\s+RENAPER',               'renaper',        'OK'),
    (r'INFORME\s+REINCIDENCIA',               'reincidencia',   'OK'),
    (r'(?:DE\s+)?LIBRE\s+EDICTO',             'edicto',         'OK'),
    (r'INFORME.*CNE|CAMARA\s+NACIONAL\s+ELECTORAL', 'cne',     'OK'),
    (r'CARTA\s+(?:DE\s+)?CIUDADAN[IÍ]A',     'carta_ciudadania', 'OK'),
]


def detect_stages_by_keyword(actuaciones):
    """
    Recorre todas las actuaciones y aplica KEYWORD_PATTERNS sobre 'descripcion'.
    Retorna dict {stage_key: 'OK'} para las etapas detectadas.
    Si un patrón matchea varias veces, el valor final siempre es 'OK' (idempotente).
    """
    detected = {}
    for act in actuaciones:
        desc = act.get('descripcion', '')
        for pattern, stage, value in KEYWORD_PATTERNS:
            if re.search(pattern, desc, re.IGNORECASE):
                detected[stage] = value
    return detected
