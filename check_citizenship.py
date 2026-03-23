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
        import sys
        print(f"  [ADVERTENCIA] Año {hoy.year} sin feriados cargados — actualizar FERIADOS en check_citizenship.py", file=sys.stderr)

    return True
