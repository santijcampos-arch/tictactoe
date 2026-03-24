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
import requests
import pdfplumber
from dateutil.relativedelta import relativedelta

FOLDER     = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(FOLDER, 'cases.json')
NOTIF_FILE = os.path.join(FOLDER, 'notifications.json')
TELEGRAM_FILE = os.path.join(FOLDER, 'telegram_ciudadania.json')
CONOCIMIENTO_FILE = os.path.join(FOLDER, 'conocimiento_juzgados.json')
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

# ── PDF ────────────────────────────────────────────────────────────────────────

def descargar_pdf(url, cookies, headers, dest):
    try:
        resp = requests.get(url, cookies=cookies, headers=headers, timeout=30)
        if resp.status_code != 200:
            return False
        ct = resp.headers.get('Content-Type', '')
        if 'pdf' not in ct.lower() and resp.content[:4] != b'%PDF':
            return False
        with open(dest, 'wb') as f:
            f.write(resp.content)
        return True
    except Exception:
        return False


def extraer_texto_pdf(path):
    try:
        with pdfplumber.open(path) as pdf:
            paginas = [p.extract_text() or '' for p in pdf.pages]
        texto = '\n\n'.join(p for p in paginas if p.strip())
        return re.sub(r'\n{3,}', '\n\n', texto).strip()
    except Exception as e:
        return f'[ERROR pdfplumber: {e}]'

# ── Detección de jura ──────────────────────────────────────────────────────────

# Keywords para detectar turno asignado (Paso 1)
_JURA_KEYWORDS = [
    'TURNO JURA', 'FECHA JURA', 'FECHA DE JURA', 'NOTA JURA',
    'JURA CIUDADANIA', 'CERTIFICADO DE JURAMENTO',
    'ASIGNA TURNO PARA JURA', 'SOLICITA FECHA DE JURA',
]

# Keywords para detectar jura completada (Paso 3)
_JURA_COMPLETADA_KEYWORDS = [
    'NOTA PRESTA JURAMENTO', 'NOTA DE JURA', 'ACTA DE JURA',
    'PRESTA JURAMENTO Y RETIRA CARTA', 'NOTA DE JURAMENTO',
    'CONSTANCIA JURA', 'NOTA JURA CIUDADANIA', 'CERTIFICADO DE JURAMENTO',
]

# Meses en español para parsear fechas de texto
_MESES = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4,
    'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
    'septiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}


def _parsear_fecha_texto(m, act_fecha_fallback):
    """
    Convierte un match de Patrón 1 o 2 (grupos: día, mes-palabra, año) a 'YYYY-MM-DD'.
    Si el mes no está en _MESES (incluye 'próximo'), usa día 15 del mes siguiente
    a act_fecha_fallback (formato 'DD/MM/YYYY').
    """
    dia_str = m.group(1)
    mes_str = m.group(2).lower()
    anio_str = m.group(3)
    mes_num = _MESES.get(mes_str)
    if mes_num:
        try:
            return datetime(int(anio_str), mes_num, int(dia_str)).strftime('%Y-%m-%d')
        except ValueError:
            pass
    # Fallback: día 15 del mes siguiente a act_fecha_fallback
    try:
        base = datetime.strptime(act_fecha_fallback, '%d/%m/%Y')
        siguiente = (base + relativedelta(months=1)).replace(day=15)
        return siguiente.strftime('%Y-%m-%d')
    except Exception:
        return None


def _extraer_fecha_jura(act, cookies, headers):
    """
    Intenta extraer la fecha de jura de una actuación (Paso 2).
    Orden: A) regex en descripción, B) regex en PDF, C) Groq fallback.
    Retorna 'YYYY-MM-DD' o None.
    """
    global _GROQ_LAST
    desc = act.get('descripcion', '') or ''
    act_fecha = act.get('fecha', '') or ''

    # A. Regex en descripción
    m = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', desc)
    if m:
        partes = m.group(1).split('/')
        return f'{partes[2]}-{partes[1]}-{partes[0]}'

    # B. Regex en texto del PDF
    texto_pdf = ''
    pdf_href = act.get('pdf_href')
    if pdf_href:
        try:
            import tempfile
            dest = os.path.join(tempfile.gettempdir(), 'jura_temp.pdf')
            ok = descargar_pdf(pdf_href, cookies, headers, dest)
            if ok:
                texto_pdf = extraer_texto_pdf(dest)
                if texto_pdf.startswith('[ERROR'):
                    texto_pdf = ''
        except Exception as e:
            print(f'    [JURA PDF] Error descargando PDF: {e}')
            texto_pdf = ''

        if texto_pdf:
            # Patrón 1: "el día 16 de abril de 2025"
            m1 = re.search(r'el\s+d[ií]a\s+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})', texto_pdf, re.I)
            if m1:
                fecha = _parsear_fecha_texto(m1, act_fecha)
                if fecha:
                    return fecha
            # Patrón 2: "el viernes 27 de febrero de 2026"
            m2 = re.search(r'\b(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})\b', texto_pdf, re.I)
            if m2:
                fecha = _parsear_fecha_texto(m2, act_fecha)
                if fecha:
                    return fecha
            # Patrón 3: fecha numérica DD/MM/YYYY
            m3 = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', texto_pdf)
            if m3:
                partes = m3.group(1).split('/')
                return f'{partes[2]}-{partes[1]}-{partes[0]}'

    # C. Groq fallback
    if not _GROQ_AVAILABLE:
        return None
    texto_groq = texto_pdf if texto_pdf else desc
    if not texto_groq or not texto_groq.strip():
        return None
    try:
        with _GROQ_SEM:
            with _GROQ_LOCK:
                ahora = time.time()
                espera = 1.5 - (ahora - _GROQ_LAST)
                if espera > 0:
                    time.sleep(espera)
                _GROQ_LAST = time.time()
            prompt = (
                "Del siguiente texto de un despacho judicial argentino, extraé la fecha "
                "en que el interesado debe concurrir a prestar juramento de ciudadanía.\n"
                "Respondé únicamente con la fecha en formato DD/MM/YYYY.\n"
                f"Si no hay fecha, respondé 'no encontrada'.\n\n{texto_groq}"
            )
            resp = _GROQ_CLIENT.chat.completions.create(
                model='llama-3.1-8b-instant',
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=20,
                temperature=0,
            )
            respuesta = resp.choices[0].message.content.strip()
        m = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', respuesta)
        if m:
            partes = m.group(1).split('/')
            return f'{partes[2]}-{partes[1]}-{partes[0]}'
    except Exception as e:
        print(f'    [JURA] Error Groq: {e}')
    return None


def detectar_jura(actuaciones_text, cookies, headers):
    """
    Analiza las actuaciones de un caso de ciudadanía para detectar:
    - fechaJura: fecha asignada para el juramento ('YYYY-MM-DD' o None)
    - juraCompletada: True si el interesado ya juró

    Pasos:
      1. Detectar FIRMA DESPACHO con keyword de jura → actuación elegida
      2. Extraer fecha del turno (regex descripción → regex PDF → Groq)
      3. Detectar actuaciones de confirmación de jura completada
    """
    fecha_jura = None
    jura_completada = False
    step1_act = None

    # ── Paso 1: detectar turno asignado ───────────────────────────────────────
    filtradas = []
    for act in actuaciones_text:
        if act.get('tipo', '').upper().strip() != 'FIRMA DESPACHO':
            continue
        desc_up = act.get('descripcion', '').upper()
        # Caso especial J04-S07
        if 'RECTIFICACION' in desc_up and 'JURA' in desc_up:
            filtradas.append(act)
            continue
        if any(kw in desc_up for kw in _JURA_KEYWORDS):
            filtradas.append(act)

    if filtradas:
        def _fecha_sort_key(a):
            try:
                return datetime.strptime(a.get('fecha', ''), '%d/%m/%Y')
            except (ValueError, TypeError):
                return datetime.min
        step1_act = max(filtradas, key=_fecha_sort_key)

        # ── Paso 2: extraer fecha ──────────────────────────────────────────────
        fecha_jura = _extraer_fecha_jura(step1_act, cookies, headers)

    # ── Paso 3: detectar jura completada ──────────────────────────────────────
    hoy = datetime.today().date()
    for act in actuaciones_text:
        desc_up = act.get('descripcion', '').upper()

        # Caso especial J02-S03
        if (act.get('tipo', '').upper() == 'ESCRITO AGREGADO'
                and act.get('descripcion', '').strip().lower() == 'jura'):
            jura_completada = True
            break

        if not any(kw in desc_up for kw in _JURA_COMPLETADA_KEYWORDS):
            continue

        # Regla de desambiguación
        if (step1_act
                and act.get('fecha') == step1_act.get('fecha')
                and act.get('descripcion') == step1_act.get('descripcion')):
            # Es la misma actuación elegida en Step 1
            if fecha_jura:
                try:
                    fj_date = datetime.strptime(fecha_jura, '%Y-%m-%d').date()
                    if fj_date <= hoy:
                        jura_completada = True
                except ValueError:
                    pass
        else:
            # Actuación distinta → confirmación directa
            jura_completada = True

        if jura_completada:
            break

    return {'fechaJura': fecha_jura, 'juraCompletada': jura_completada}

# ── Extracción de actuaciones ─────────────────────────────────────────────────

def get_actuaciones_cit(driver):
    """
    Extrae TODAS las filas de la tabla de actuaciones del expediente actual.
    Retorna lista de dicts [{tipo, descripcion, fecha, oficina, pdf_href}] de más antigua a más reciente.
    No abre documentos — solo lee el texto de la tabla.
    pdf_href es str solo si el tipo es FIRMA DESPACHO y hay botón de descarga, None en los demás casos.
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
            # Extraer href del botón de descarga si existe
            pdf_href = None
            for td in fila.find_all('td'):
                a = td.find('a', href=True)
                if a and a['href']:
                    href = a['href']
                    if 'descargar' in href.lower() or href.lower().endswith('.pdf') or 'documento' in href.lower():
                        pdf_href = href
                        break
                # También buscar forms con action
                form = td.find('form')
                if form and form.get('action'):
                    action = form['action']
                    if 'descargar' in action.lower() or 'documento' in action.lower():
                        pdf_href = action
                        break
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
                # pdf_href: str solo si FIRMA DESPACHO con botón, None en todos los demás casos
                act_pdf_href = pdf_href if tipo.upper().strip() == 'FIRMA DESPACHO' and pdf_href else None
                actuaciones.append({
                    'fecha': fecha,
                    'tipo': tipo,
                    'descripcion': descripcion,
                    'oficina': oficina,
                    'pdf_href': act_pdf_href,
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

# ── Telegram context ──────────────────────────────────────────────────────────

def load_telegram_ctx(client_name, telegram_data):
    """
    Busca mensajes del cliente en telegram_ciudadania.json.
    Retorna string con los últimos 3 mensajes relevantes, o '' si no hay.
    telegram_data: dict cargado de telegram_ciudadania.json (puede ser None).
    """
    if not telegram_data or not client_name:
        return ''

    apellido = client_name.strip().upper().split()[0] if client_name.strip() else ''
    if not apellido or len(apellido) < 3:
        return ''

    msgs = []
    for grupo, entradas in telegram_data.items():
        for entry in entradas:
            cliente_entry = (entry.get('cliente') or '').upper()
            if apellido in cliente_entry:
                msgs.append(f"[{entry['fecha']}] {entry['de']}: {entry['texto'][:200]}")

    if not msgs:
        return ''

    ultimos = msgs[-3:]  # los 3 más recientes (la lista ya viene ordenada por fecha)
    return '\n'.join(ultimos)

# ── Análisis Groq ─────────────────────────────────────────────────────────────

def analizar_ciudadania(case, actuaciones, ya_detectadas, telegram_ctx):
    """
    Llama a Groq con todas las actuaciones del caso.
    Retorna dict con keys: stages, requires_action, action_note, last_relevant_stage.
    Retorna None si Groq no está disponible o falla.
    """
    if not _GROQ_AVAILABLE or not actuaciones:
        return None

    # Serializar actuaciones
    actos_texto = '\n'.join(
        f"[{a['fecha']}] {a['descripcion']}"
        for a in actuaciones
        if a.get('descripcion')
    )

    # Etapas ya detectadas por keyword (no re-evaluar)
    ya_str = ', '.join(f"{k}={v}" for k, v in ya_detectadas.items()) if ya_detectadas else 'ninguna'

    prompt = f"""Sos un asistente para un estudio de abogados argentino especializado en ciudadanía.
Analizás las actuaciones de un expediente de ciudadanía del PJN y determinás:
1. El estado actual de cada etapa procesal
2. Si hay algo que el estudio debe atender (pedido de documentación, dictamen desfavorable, sentencia de rechazo, pedido de aclaración, etc.)

Etapas a evaluar: pfa_interpol, renaper, cne, reincidencia, dnm, pfa_dactilo, edicto, pfa_convenio, medios_de_vida, fiscal, sentencia, carta_ciudadania

Ya fueron detectadas automáticamente (no re-evaluar): {ya_str}

Actuaciones del expediente (de más antigua a más reciente):
{actos_texto}

{f"Contexto adicional de comunicaciones con el cliente:{chr(10)}{telegram_ctx}" if telegram_ctx else ""}

Respondé ÚNICAMENTE con JSON válido, sin texto adicional:
{{
  "stages": {{
    "pfa_interpol": "OK" o "NO" o "",
    "renaper": "OK" o "NO" o "",
    "cne": "OK" o "NO" o "",
    "reincidencia": "OK" o "NO" o "",
    "dnm": "OK" o "NO" o "",
    "pfa_dactilo": "OK" o "NO" o "",
    "edicto": "OK" o "NO" o "",
    "pfa_convenio": "OK" o "NO" o "",
    "medios_de_vida": "OK" o "NO" o "",
    "fiscal": "OK" o "NO" o "",
    "sentencia": "OK" o "NO" o "",
    "carta_ciudadania": "OK" o "NO" o ""
  }},
  "requires_action": true o false,
  "action_note": "descripción específica de qué debe hacer el estudio (vacío si no requiere acción)",
  "last_relevant_stage": "nombre de la etapa más avanzada detectada"
}}

Solo incluí en "stages" las etapas que podás determinar con certeza. Dejá "" para las que no tengas información.
Si "requires_action" es true, "action_note" debe ser específico: qué se pide, en qué actuación, con qué fecha."""

    global _GROQ_LAST
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
                max_tokens=600,
            )
            raw = resp.choices[0].message.content.strip()
            # Extraer JSON si viene envuelto en markdown
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if not m:
                print(f"    [Groq] Respuesta sin JSON: {raw[:100]}")
                return None
            result = json.loads(m.group(0))
            print(f"    [Groq] Análisis OK (requires_action={result.get('requires_action')})")
            return result
        except json.JSONDecodeError as e:
            print(f"    [Groq] JSON inválido: {e}")
            return None
        except Exception as e:
            case_num = case.get('caseNumber', '?')
            print(f"    [Groq] Error en {case_num}: {e}")
            return None


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
        return act.get('fecha', '') > last_date  # lexicográfico: aceptable para alertas

    nuevas = [a for a in actuaciones if es_nueva(a)]
    for alerta in perfil.get('alertas_reactivas', []):
        keywords = alerta.get('keywords', [])
        for act in nuevas:
            desc_up = act.get('descripcion', '').upper()
            if any(kw.upper() in desc_up for kw in keywords):
                alertas.append(alerta['mensaje'])
                break  # una alerta por keyword-group, no duplicar

    return alertas


# ── Merge de stages ───────────────────────────────────────────────────────────

def merge_stages(existing, detected):
    """
    Combina stages existentes con los recién detectados (keyword + Groq).
    Reglas:
    - 'OK' es permanente (nunca se baja)
    - 'NO' puede ser sobreescrito por 'OK' (informe que fue favorable)
    - '' se actualiza libremente
    """
    result = dict(existing) if existing else {}
    for key, new_val in detected.items():
        current = result.get(key, '')
        if current == 'OK':
            continue  # OK es permanente
        if new_val in ('OK', 'NO'):
            result[key] = new_val
    return result


def _update_last_check(case_id):
    """
    Actualiza solo lastPjnCheck para el caso dado.
    Thread-safe vía _CASES_LOCK + file lock.
    Usar cuando el smart skip detecta que no hay cambios.
    """
    with _CASES_LOCK:
        if not _acquire_file_lock(_CASES_FILE_LOCK_PATH):
            print(f"    [WARN] No se pudo adquirir lock para update_last_check")
            return
        try:
            with open(CASES_FILE, encoding='utf-8') as f:
                all_cases = json.load(f)
            for c in all_cases:
                if c.get('id') == case_id:
                    c['lastPjnCheck'] = datetime.now().isoformat()
                    break
            _atomic_write_json(CASES_FILE, all_cases)
        except Exception as e:
            print(f"    [WARN] Error en update_last_check: {e}")
        finally:
            _release_file_lock(_CASES_FILE_LOCK_PATH)


# ── Procesamiento por caso ────────────────────────────────────────────────────

def process_case(driver, case, ocr, cases, telegram_data):
    """
    Procesa un caso de ciudadanía:
    1. Navega al PJN
    2. Smart skip si no hay cambios
    3. Keyword detection
    4. Groq si hay etapas sin resolver
    5. Merge stages + write cases.json
    6. Notificaciones
    Modifica 'cases' in-place (lista compartida, protegida por _CASES_LOCK).
    """
    case_number = case.get('caseNumber', '?')
    client_name = case.get('clientName', '?')

    actuaciones, pjn_last_date, driver = query_citizenship_case(driver, case, ocr)
    if actuaciones is None:
        print(f"    Sin resultados para {case_number}")
        return driver

    # Smart skip: si la fecha de la última actuación no cambió, solo actualizar lastPjnCheck
    if (pjn_last_date
            and pjn_last_date == case.get('lastActionDate')
            and case.get('lastPjnCheck')):
        print(f"    Sin cambios desde {pjn_last_date} — saltando análisis")
        _update_last_check(case['id'])
        return driver

    # Detectar fecha de jura
    try:
        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        headers = {'User-Agent': driver.execute_script("return navigator.userAgent")}
        jura = detectar_jura(actuaciones, cookies, headers)
    except Exception as e:
        print(f"    [JURA] Error al detectar jura: {e}")
        jura = {'fechaJura': None, 'juraCompletada': False}
    fecha_jura_anterior = None   # se actualiza dentro del lock con el valor real del archivo
    if jura['fechaJura']:
        print(f"    [JURA] Fecha detectada: {jura['fechaJura']}")
    if jura['juraCompletada']:
        print(f"    [JURA] Jura completada")

    # Keyword detection
    keyword_stages = detect_stages_by_keyword(actuaciones)
    if keyword_stages:
        print(f"    [KW] Detectadas: {', '.join(keyword_stages.keys())}")

    # Determinar si quedan etapas sin resolver
    existing_stages = case.get('stages', {})
    merged_so_far   = merge_stages(existing_stages, keyword_stages)
    all_stage_keys  = [k for k, _ in CITIZENSHIP_STAGES]
    unresolved      = [k for k in all_stage_keys if not merged_so_far.get(k)]

    # Groq: solo si hay etapas sin resolver
    groq_result = None
    if unresolved:
        telegram_ctx = load_telegram_ctx(client_name, telegram_data)
        groq_result  = analizar_ciudadania(case, actuaciones, keyword_stages, telegram_ctx)

    groq_stages = (groq_result or {}).get('stages', {})
    final_stages = merge_stages(merged_so_far, groq_stages)

    # Knowledge base alerts (proactivas y reactivas)
    conocimiento = _cargar_conocimiento()
    alertas_kb = detectar_alertas_conocimiento(case, actuaciones, conocimiento, final_stages)
    for msg in alertas_kb:
        print(f'    [KB] Alerta: {msg[:80]}')

    # Detectar etapas completadas por primera vez en esta corrida
    prev_sentencia      = existing_stages.get('sentencia', '')
    prev_carta          = existing_stages.get('carta_ciudadania', '')
    new_sentencia       = final_stages.get('sentencia', '')
    new_carta           = final_stages.get('carta_ciudadania', '')
    sentencia_nueva     = (prev_sentencia != new_sentencia and new_sentencia in ('OK', 'NO'))
    carta_nueva         = (prev_carta != new_carta and new_carta == 'OK')

    # Escribir cases.json (thread-safe)
    with _CASES_LOCK:
        if not _acquire_file_lock(_CASES_FILE_LOCK_PATH):
            print(f"    [WARN] No se pudo adquirir lock de cases.json")
            return driver
        try:
            with open(CASES_FILE, encoding='utf-8') as f:
                all_cases = json.load(f)
            idx = next((i for i, c in enumerate(all_cases) if c.get('id') == case['id']), None)
            if idx is not None:
                fecha_jura_anterior = all_cases[idx].get('fechaJura')
                all_cases[idx]['stages']        = final_stages
                all_cases[idx]['lastActionDate'] = pjn_last_date
                all_cases[idx]['lastPjnCheck']   = datetime.now().isoformat()
                if jura['fechaJura']:
                    all_cases[idx]['fechaJura'] = jura['fechaJura']
                if jura['juraCompletada']:
                    all_cases[idx]['juraCompletada'] = True
                if groq_result and groq_result.get('requires_action'):
                    all_cases[idx]['nextAction'] = groq_result.get('action_note', '')
                elif not groq_result:
                    pass  # no tocar nextAction si Groq falló
                else:
                    all_cases[idx]['nextAction'] = ''
            _atomic_write_json(CASES_FILE, all_cases)
        except Exception as e:
            print(f"    [ERROR] No se pudo escribir cases.json: {e}")
            return driver
        finally:
            _release_file_lock(_CASES_FILE_LOCK_PATH)

    if idx is None:
        return driver

    print(f"    OK {case_number} — stages: {sum(1 for v in final_stages.values() if v == 'OK')}/12 completas")

    # Notificaciones
    if groq_result and groq_result.get('requires_action'):
        msg = groq_result.get('action_note', 'Acción requerida — revisar expediente')
        add_notification(case, 'citizenship_update', msg)
        print(f"    [NOTIF] Acción requerida: {msg[:80]}")

    if sentencia_nueva:
        estado = 'favorable' if new_sentencia == 'OK' else 'desfavorable/pendiente'
        add_notification(case, 'citizenship_update', f"Sentencia dictada ({estado}) — {case_number}")
        print(f"    [NOTIF] Sentencia detectada")

    if carta_nueva:
        add_notification(case, 'citizenship_update', f"Carta de ciudadanía lista — {client_name}")
        print(f"    [NOTIF] Carta de ciudadanía")

    if jura['fechaJura'] and jura['fechaJura'] != fecha_jura_anterior:
        fj = jura['fechaJura']
        fecha_fmt = f"{fj[8:10]}/{fj[5:7]}/{fj[:4]}"
        add_notification(case, 'jura_asignada', f"Jura asignada — {client_name}: {fecha_fmt}")
        print(f"    [NOTIF] Jura asignada: {fecha_fmt}")

    for msg in alertas_kb:
        add_notification(case, 'conocimiento_alert', msg)

    return driver


# ── Worker ────────────────────────────────────────────────────────────────────

def run_worker(worker_id, cases_subset, cases, telegram_data):
    driver = setup_driver()
    ocr = ddddocr.DdddOcr(show_ad=False)
    updated = 0
    try:
        for case in cases_subset:
            name = case.get('clientName') or case.get('caseNumber', '?')
            print(f"  [W{worker_id}] [{case.get('caseNumber','?')}] {name}")
            driver = process_case(driver, case, ocr, cases, telegram_data)
            time.sleep(8)  # evitar ráfagas al PJN
            updated += 1
    finally:
        quit_driver(driver)
    print(f"  [W{worker_id}] Terminado — {updated} casos procesados.")
    return updated


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print('=' * 60)
    print('  NegroLex — PJN Citizenship Checker')
    print(f'  Workers: {N_WORKERS}')
    print('=' * 60)
    print()

    if AUTO and not es_dia_habil():
        print(f"  Hoy ({datetime.now().strftime('%A %d/%m/%Y')}) no es día hábil — saliendo.")
        return

    if not os.path.exists(CASES_FILE):
        print('No se encontró cases.json.')
        return

    with open(CASES_FILE, encoding='utf-8') as f:
        cases = json.load(f)

    # Cargar contexto de Telegram (opcional)
    telegram_data = None
    if os.path.exists(TELEGRAM_FILE):
        try:
            with open(TELEGRAM_FILE, encoding='utf-8') as f:
                telegram_data = json.load(f)
            print(f"  [Telegram] {len(telegram_data)} grupos cargados.")
        except Exception as e:
            print(f"  [Telegram] No disponible: {e}")

    # Filtrar casos de ciudadanía con número de expediente
    cit_cases = [
        c for c in cases
        if c.get('category') == 'citizenship'
        and c.get('caseNumber')
        and parse_citizenship_case_number(c['caseNumber'])[0]  # número parseable
    ]
    if LIMIT:
        cit_cases = cit_cases[:LIMIT]

    if not cit_cases:
        print('No hay casos de ciudadanía con número de expediente.')
        return

    print(f"  {len(cit_cases)} casos de ciudadanía encontrados.")
    print(f"  Distribuyendo entre {N_WORKERS} workers...\n")

    chunks = [[] for _ in range(N_WORKERS)]
    for i, case in enumerate(cit_cases):
        chunks[i % N_WORKERS].append(case)

    total_updated = 0
    with ThreadPoolExecutor(max_workers=N_WORKERS) as executor:
        futures = {
            executor.submit(run_worker, i + 1, chunk, cases, telegram_data): i
            for i, chunk in enumerate(chunks) if chunk
        }
        for future in as_completed(futures):
            try:
                total_updated += future.result()
            except Exception as e:
                print(f"  [ERROR] Worker falló: {e}")

    print()
    print('=' * 60)
    print(f"  Listo. {total_updated} casos procesados.")
    print('=' * 60)

    if not AUTO:
        try:
            input('\nPresioná Enter para salir.')
        except EOFError:
            pass


if __name__ == '__main__':
    main()
