"""
NegroLex — PJN Auto-Checker (paralelo)
Consulta expedientes constitucionales en scw.pjn.gov.ar con Selenium + ddddocr.
Requiere: pip install selenium ddddocr beautifulsoup4
"""

import os
import json
import re
import time
import io
import threading
import requests
import pdfplumber
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from groq import Groq as _Groq

# Cargar API key de Groq
_GROQ_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'groq_key.txt')
try:
    with open(_GROQ_KEY_FILE, encoding='utf-8') as _f:
        _GROQ_KEY = _f.read().strip()
    _GEMINI_CLIENT = _Groq(api_key=_GROQ_KEY)
    _GEMINI = True
    print("  [Groq] API lista.")
except Exception as _e:
    _GEMINI_CLIENT = None
    _GEMINI = None
    print(f"  [Groq] No disponible: {_e}")

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
PJN_URL    = 'https://scw.pjn.gov.ar/scw/home.seam'

# ── Configuración de paralelismo ──────────────────────────────────────────────
N_WORKERS = 1  # UN Chrome a la vez — evita ban por consultas simultáneas al PJN
MAX_CHROME_INSTANCES = 3  # si se superan, el programa se termina

# Contador de instancias de Chrome activas
_ACTIVE_DRIVERS = 0
_ACTIVE_DRIVERS_LOCK = threading.Lock()

# Lock para escrituras a cases.json
_CASES_LOCK = threading.Lock()
# Semáforo para limitar llamadas concurrentes a Gemini (1 a la vez → respeta rate limit)
_GEMINI_SEM = threading.Semaphore(1)
# Tiempo mínimo entre llamadas a Gemini (segundos)
_GEMINI_LAST = 0.0
_GEMINI_LOCK = threading.Lock()

JURISDICTION_CODES = {
    'CSJ': '0',  'CIV': '1',  'CAF': '2',  'CCF': '3',
    'CNE': '4',  'CSS': '5',  'CPE': '6',  'CNT': '7',
    'CFP': '8',  'CCC': '9',  'COM': '10', 'CPF': '11',
    'CPN': '12', 'FBB': '13', 'FCR': '14', 'FCB': '15',
    'FCT': '16', 'FGR': '17', 'FLP': '18', 'FMP': '19',
    'FMZ': '20', 'FPO': '21', 'FPA': '22', 'FRE': '23',
    'FSA': '24', 'FRO': '25', 'FSM': '26', 'FTU': '27',
}

_CASES_FILE_LOCK_PATH = CASES_FILE + '.lock'
_NOTIF_FILE_LOCK_PATH = NOTIF_FILE + '.lock'

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def detectTipoAccion(case):
    """
    Detecta el tipo de proceso contencioso a partir de la carátula.
    Jerarquía estricta (igual que lawcase.html):
      1. amparo_mora      — MORA / ART. 28 / 19.549
      2. recurso_directo  — RECURSO DIRECTO / APELACIÓN RESOLUCIÓN / ART. 32 / 25.871
      3. inconstitucionalidad — todo lo demás (fallback)
    """
    t = (case.get('caseTitle') or case.get('caratula') or '').upper()
    if re.search(r'MORA|ART[\.\s]*28|19[\.\s]?549', t):
        return 'amparo_mora'
    if re.search(
        r'RECURSO DIRECTO|APELACI[OÓ]N.*RESOLUCI[OÓ]N|APELACION.*RESOLUCION'
        r'|ART[\.\s]*32|25[\.\s]?871', t
    ):
        return 'recurso_directo'
    return 'inconstitucionalidad'


def parse_case_number(case_number):
    """'CAF 031904/2025' → ('2', '31904', '2025')"""
    m = re.match(r'([A-Z]+)\s+0*(\d+)/(\d{4})', case_number.strip())
    if not m:
        return None, None, None
    code = JURISDICTION_CODES.get(m.group(1))
    return code, m.group(2), m.group(3)


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


# ── OCR ───────────────────────────────────────────────────────────────────────

def read_captcha(ocr, png_bytes):
    """Lee los 4 dígitos del captcha. Retorna string de 4 dígitos o None."""
    result = ocr.classification(png_bytes)
    digits = re.sub(r'\D', '', result)
    if len(digits) == 4:
        print(f"    [OCR] '{digits}'")
        return digits
    print(f"    [OCR] Leyó '{digits}' — reintentando...")
    return None


# ── Captcha ────────────────────────────────────────────────────────────────────

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


# ── Query ──────────────────────────────────────────────────────────────────────

def query_case(driver, case, ocr):
    """Retorna (result, driver) — driver puede ser uno nuevo si Chrome se cerró."""
    case_number = case.get('caseNumber', '')
    code, number, year = parse_case_number(case_number)
    if not code:
        print(f"    Expediente no reconocido: {case_number}")
        return None, driver

    chrome_restarts = 0
    attempt = 0
    while attempt < 5:
        attempt += 1
        if attempt > 1:
            print(f"    Reintento {attempt}/5...")
        try:
            driver.get(PJN_URL)

            if not wait_for(driver, 15, EC.presence_of_element_located((By.ID, 'formPublica:camaraNumAni'))):
                print("    Pagina no cargo, reintentando...")
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
                print("    No llego el resultado, reintentando...")
                continue

            existing_last_date = case.get('lastActionDate', '')
            return parse_result(driver, case_number, existing_last_date=existing_last_date), driver

        except Exception as e:
            msg = str(e).split('\n')[0][:120]
            print(f"    Error: {msg} — reintentando...")
            try:
                driver.title
            except Exception:
                if chrome_restarts >= 2:
                    print("    Chrome muerto demasiadas veces, abandonando caso.")
                    return None, driver
                chrome_restarts += 1
                print(f"    Chrome muerto ({chrome_restarts}/2), reiniciando...")
                quit_driver(driver)
                driver = setup_driver()
            continue

    return None, driver


# ── Leer documento (botón ojo) ─────────────────────────────────────────────────

def leer_documento(driver):
    """
    Lee el texto del documento abierto. Si es un PDF, lo descarga con requests
    usando las cookies de Selenium y extrae el texto con pdfplumber.
    """
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
        time.sleep(1)

        url = driver.current_url

        texto = driver.find_element(By.TAG_NAME, 'body').text.strip()
        if texto and len(texto) > 50:
            texto = re.sub(r'\n{3,}', '\n\n', texto)
            return texto[:4000]

        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        headers = {'User-Agent': driver.execute_script("return navigator.userAgent")}

        resp = requests.get(url, cookies=cookies, headers=headers, timeout=15)
        if resp.status_code != 200:
            return ''

        content_type = resp.headers.get('Content-Type', '')
        if 'pdf' in content_type or url.lower().endswith('.pdf') or resp.content[:4] == b'%PDF':
            with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                paginas = []
                for page in pdf.pages[:5]:
                    t = page.extract_text()
                    if t:
                        paginas.append(t)
                texto = '\n\n'.join(paginas).strip()
            texto = re.sub(r'\n{3,}', '\n\n', texto)
            return texto[:4000]

        texto = resp.text[:4000]
        return texto

    except Exception as e:
        print(f"       [DOC] Error leyendo: {e}")
        return ''


def get_actuaciones_con_documentos(driver, known_last_date=None):
    """
    Encuentra la tabla de actuaciones en la página actual.
    Para las últimas 5 filas, extrae el texto y lee documentos si los hay.
    Si known_last_date se provee y la actuación más reciente tiene esa misma fecha,
    se omite la lectura de documentos (nada cambió desde la última consulta).
    """
    actuaciones = []

    try:
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        bs_tables = soup.find_all('table')
        tabla_idx = None
        for i, table in enumerate(bs_tables):
            if not re.search(r'\d{2}/\d{2}/\d{4}', table.get_text(' ')):
                continue
            all_rows = table.find_all('tr')
            if len(all_rows) < 2:
                continue
            first_row_text = all_rows[0].get_text(' ').lower()
            if any(w in first_row_text for w in ['tipo', 'descripci', 'fecha', 'actu', 'descargar']):
                tabla_idx = i
                print(f"    [ACT] Tabla encontrada (índice {i}, {len(all_rows)} filas)")
                break

        if tabla_idx is None:
            print("    [ACT] No se encontró la tabla de actuaciones")
            return actuaciones

        selenium_tables = driver.find_elements(By.TAG_NAME, 'table')
        if tabla_idx >= len(selenium_tables):
            return actuaciones
        tabla = selenium_tables[tabla_idx]

        filas = tabla.find_elements(By.TAG_NAME, 'tr')[1:]
        ultimas_5 = filas[:5] if len(filas) >= 5 else filas

        # Si la primera fila tiene la misma fecha que la última vez, no hay novedades —
        # saltear la lectura de documentos para no hacer pedidos innecesarios al PJN.
        skip_docs = False
        if known_last_date and ultimas_5:
            primer_fila_texto = ' '.join(c.text.strip() for c in ultimas_5[0].find_elements(By.TAG_NAME, 'td'))
            primer_fecha_m = re.search(r'\d{2}/\d{2}/\d{4}', primer_fila_texto)
            if primer_fecha_m and primer_fecha_m.group(0) == known_last_date:
                skip_docs = True
                print(f"    [ACT] Sin novedades desde {known_last_date} — omitiendo lectura de documentos")

        ventana_original = driver.current_window_handle

        for fila in ultimas_5:
            celdas = fila.find_elements(By.TAG_NAME, 'td')
            textos = [c.text.strip() for c in celdas]
            textos = [t for t in textos if t and t not in ('Descargar', 'Ver', 'Descargar Actuación')]
            if not textos:
                continue

            fecha_match = next(
                (re.search(r'\d{2}/\d{2}/\d{4}', t) for t in textos if re.search(r'\d{2}/\d{2}/\d{4}', t)),
                None
            )
            fecha = fecha_match.group(0) if fecha_match else ''

            def _cell_val(t):
                return t.split(':\n', 1)[1].strip() if ':\n' in t else t.strip()

            tipo_cell    = next((t for t in textos if re.match(r'tipo\s+actua', t, re.I)), '')
            detalle_cell = next((t for t in textos if re.match(r'detalle', t, re.I)), '')
            tipo    = _cell_val(tipo_cell)    if tipo_cell    else ''
            detalle = _cell_val(detalle_cell) if detalle_cell else ''

            if tipo or detalle:
                linea = ' — '.join(filter(None, [fecha, tipo, detalle]))
            else:
                _RUIDO = re.compile(r'descargar|^\s*ver\s*$|oficina:|^fecha:|^[A-Z]{2,4}\s*$', re.I)
                otros = [t for t in textos if t and t != fecha
                         and not re.search(r'\d{2}/\d{2}/\d{4}', t)
                         and not _RUIDO.search(t)]
                linea = ' — '.join(filter(None, [fecha] + otros))

            doc_texto = ''
            if skip_docs:
                if linea:
                    actuaciones.append({'fecha': fecha, 'descripcion': linea, 'documento': ''})
                continue
            try:
                botones = fila.find_elements(By.XPATH, './/button | .//a[@onclick or @href]')
                boton_ojo = None

                for btn in botones:
                    html_btn = btn.get_attribute('outerHTML') or ''
                    titulo   = (btn.get_attribute('title') or '').lower()
                    if re.search(r'eye|visualiz', html_btn, re.IGNORECASE) or 'ver' in titulo:
                        boton_ojo = btn
                        break

                if not boton_ojo and len(botones) >= 2:
                    boton_ojo = botones[1]

                if boton_ojo:
                    ventanas_antes = set(driver.window_handles)
                    url_antes = driver.current_url

                    driver.execute_script("arguments[0].click();", boton_ojo)
                    time.sleep(2)

                    ventanas_despues = set(driver.window_handles)
                    ventanas_nuevas  = ventanas_despues - ventanas_antes

                    if ventanas_nuevas:
                        nueva_ventana = ventanas_nuevas.pop()
                        driver.switch_to.window(nueva_ventana)
                        doc_texto = leer_documento(driver)
                        driver.close()
                        driver.switch_to.window(ventana_original)
                        print(f"       DOC Documento leido: {linea[:60]} ({len(doc_texto)} chars)")
                    elif driver.current_url != url_antes:
                        doc_texto = leer_documento(driver)
                        driver.back()
                        time.sleep(2)
                        print(f"       DOC Documento leido: {linea[:60]} ({len(doc_texto)} chars)")

            except Exception as e:
                print(f"       [DOC] Error: {e}")

            if linea:
                actuaciones.append({'fecha': fecha, 'descripcion': linea, 'documento': doc_texto})

    except Exception as e:
        print(f"    [ACT] Error general: {e}")

    return actuaciones


# ── Análisis con Gemini ────────────────────────────────────────────────────────

def analizar_caso(case_number, client_name, caratula, tribunal, sit_actual, actuaciones):
    """
    Llama a Gemini para que analice las actuaciones.
    Serializado con semáforo para no saturar el rate limit.
    """
    if not _GEMINI_CLIENT:
        return ''
    if not actuaciones:
        return ''

    partes = [
        f"Expediente: {case_number}",
        f"Cliente: {client_name}",
    ]
    if caratula:
        partes.append(f"Carátula: {caratula}")
    if tribunal:
        partes.append(f"Tribunal: {tribunal}")
    if sit_actual:
        partes.append(f"Situación actual: {sit_actual}")

    partes.append("\nÚltimas actuaciones del expediente (de más antigua a más reciente):")
    for act in actuaciones:
        partes.append(f"\n[{act['fecha']}] {act['descripcion']}")
        if act.get('documento'):
            partes.append(f"Contenido del documento:\n{act['documento'][:2000]}")

    contexto = '\n'.join(partes)

    # Determinar tipo de proceso para darle contexto al modelo
    tipo = detectTipoAccion({'caseTitle': caratula})
    tipo_label = {
        'amparo_mora':        'Amparo por Mora (Ley 19.549, Art. 28 — proceso sumarísimo, 5 días hábiles para informe del Estado)',
        'recurso_directo':    'Recurso Directo DNM (Ley 25.871, Art. 32 — impugna resolución administrativa, 5 días hábiles para informe)',
        'inconstitucionalidad': 'Proceso de Conocimiento / Inconstitucionalidad (CPCCN — 15 días hábiles para contestar traslado)',
    }.get(tipo, 'Contencioso administrativo — fuero CAF')

    prompt = f"""Sos un asistente jurídico especializado en derecho administrativo argentino, fuero Contencioso Administrativo Federal (CAF).

Tipo de proceso: {tipo_label}

Analizá las siguientes actuaciones y respondé en español, de forma clara y concisa (máximo 5 oraciones):
1. Cuál es el estado actual del expediente
2. Si hay algún plazo procesal corriendo o vencido según el tipo de proceso
3. Si hay algo urgente o una acción concreta requerida

{contexto}

Resumen del caso:"""

    global _GEMINI_LAST
    with _GEMINI_SEM:
        # Asegurar al menos 5 segundos entre llamadas
        with _GEMINI_LOCK:
            ahora = time.time()
            espera = 5.0 - (ahora - _GEMINI_LAST)
            if espera > 0:
                time.sleep(espera)
            _GEMINI_LAST = time.time()

        try:
            respuesta = _GEMINI_CLIENT.chat.completions.create(
                model='llama-3.1-8b-instant',
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=400,
            )
            resumen = respuesta.choices[0].message.content.strip()
            print(f"    [Groq] Analisis generado ({len(resumen)} chars)")
            return resumen
        except Exception as e:
            print(f"    [Groq] Error: {e}")
            return ''


# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_result(driver, case_number, existing_last_date=None):
    result = {
        'caseNumber':  case_number,
        'queried':     True,
        'lastChecked': datetime.now().strftime('%Y-%m-%d'),
    }

    body_text = driver.find_element(By.TAG_NAME, 'body').text

    if re.search(r'no\s+se\s+encontr[oó]|sin\s+resultado', body_text, re.IGNORECASE):
        print("    Expediente no encontrado.")
        result['queried'] = False
        return result

    def extract(pattern):
        m = re.search(pattern, body_text, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else ''

    dependencia  = extract(r'Dependencia[:\s]+(.+)')
    jurisdiccion = extract(r'Jurisdicci[oó]n[:\s]+(.+)')
    sit_actual   = extract(r'Sit\.?\s*Actual[:\s]+(.+)')
    caratula     = extract(r'Car[aá]tula[:\s]+(.+)')

    result['tribunal']        = dependencia or jurisdiccion
    result['proceduralStage'] = sit_actual
    result['caratula']        = caratula

    actuaciones = get_actuaciones_con_documentos(driver, known_last_date=existing_last_date)

    if not actuaciones:
        fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', body_text)
        if fechas:
            ultima = fechas[-1]
            actuaciones.append({'fecha': ultima, 'descripcion': ultima, 'documento': ''})
            print(f"    [ACT] Fallback fecha: {ultima}")
        else:
            print("    [ACT] Sin actuaciones encontradas")

    # Movimientos de "cortesía" / pase interno que no son hitos procesales reales.
    # Se guardan igual en recentActuaciones pero NO se usan como lastAction.
    RUIDO = re.compile(
        r'PAS[OÓ] A DESPACHO|VUELVA? (DE|AL) DESPACHO|PASE INTERNO|'
        r'A DESPACHO|DE DESPACHO|VISTA (DE|AL) EXPEDIENTE|'
        r'DEVUÉLVASE A (SECRETAR|DESPACHO)|AGRÉGUESE',
        re.IGNORECASE
    )

    if actuaciones:
        # Buscar el último hito procesal real (ignorar ruido)
        hito = next((a for a in actuaciones if not RUIDO.search(a['descripcion'])), actuaciones[0])
        result['lastAction']     = hito['descripcion'][:300]
        result['lastActionDate'] = hito['fecha']

    result['recentActuaciones'] = list(reversed(actuaciones))

    resumen_ia = analizar_caso(
        case_number   = case_number,
        client_name   = result.get('caratula', case_number),
        caratula      = caratula,
        tribunal      = result.get('tribunal', ''),
        sit_actual    = sit_actual,
        actuaciones   = actuaciones,
    )
    if resumen_ia:
        result['caseAnalysis'] = resumen_ia

    hoy = datetime.now().strftime('%d/%m/%Y')
    resumen_partes = [f"Actualizado: {hoy}"]
    if caratula:
        resumen_partes.append(f"Carátula: {caratula}")
    if sit_actual:
        resumen_partes.append(f"Situación: {sit_actual}")
    if dependencia:
        resumen_partes.append(f"Dependencia: {dependencia}")
    if actuaciones:
        resumen_partes.append("")
        resumen_partes.append("Últimas 5 actuaciones:")
        for act in actuaciones:
            resumen_partes.append(f"  • {act['descripcion']}")
            if act.get('documento'):
                primeras_lineas = act['documento'].split('\n')[:3]
                for linea in primeras_lineas:
                    if linea.strip():
                        resumen_partes.append(f"      {linea.strip()[:120]}")
    result['notes'] = '\n'.join(resumen_partes)

    print(f"    OK Tribunal:  {result.get('tribunal','?')[:70]}")
    print(f"    OK Estado:    {result.get('proceduralStage','?')[:60]}")
    print(f"    OK Caratula:  {result.get('caratula','?')[:70]}")
    if actuaciones:
        print(f"    OK {len(actuaciones)} actuaciones leidas "
              f"({sum(1 for a in actuaciones if a.get('documento'))} con documento)")
    else:
        print("    [ACT] Sin actuaciones extraídas")

    return result


# ── Evaluación de urgencia y sugerencia ───────────────────────────────────────

def dias_desde(fecha_str):
    if not fecha_str:
        return None
    try:
        from datetime import datetime as dt
        d = dt.strptime(fecha_str.strip(), '%d/%m/%Y')
        return (dt.now() - d).days
    except Exception:
        return None

def textos_actuaciones(case):
    acts = case.get('recentActuaciones') or []
    return ' | '.join(a.get('descripcion', '') for a in acts).upper()

def evaluar_urgencia(case):
    estado       = (case.get('proceduralStage') or '').upper()
    tribunal     = (case.get('tribunal') or '').upper()
    notas        = (case.get('notes') or '').upper()
    acts_texto   = textos_actuaciones(case)
    last_date    = case.get('lastActionDate')
    dias_sin_mov = dias_desde(last_date)
    tipo         = detectTipoAccion(case)  # 'amparo_mora' | 'recurso_directo' | 'inconstitucionalidad'
    en_camara    = 'CAMARA' in tribunal

    def dias_desde_acto(patron):
        acts = case.get('recentActuaciones') or []
        for a in reversed(acts):
            desc = (a.get('descripcion') or '').upper()
            if re.search(patron, desc, re.IGNORECASE):
                d = dias_desde(a.get('fecha'))
                if d is not None:
                    return d
        return None

    # ── URGENTES ──────────────────────────────────────────────────────────────

    # Sentencia de Cámara — universal, aplica a todos los tipos
    if re.search(r'SENTENCIA DE C[AÁ]MARA|SENT.*CAM', acts_texto + notas):
        return ('urgent',
            'Hay SENTENCIA DE CÁMARA registrada. Verificar urgente el contenido. '
            'Si es desfavorable, el plazo para recurso extraordinario ante la CSJN es de 10 días '
            'hábiles desde la notificación (Art. 257 CPCCN). Controlar si el plazo no venció.')

    # Traslado vencido — umbral según tipo de proceso
    # Amparo por mora: 5 días hábiles (Art. 28 Ley 19.549) ≈ 8 días corridos
    # Recurso directo: 5 días hábiles (Art. 77 Ley 25.871) ≈ 8 días corridos
    # Inconstitucionalidad: 15 días hábiles (CPCCN) ≈ 22 días corridos
    dias_traslado = dias_desde_acto(
        r'TRASLADO DE DEMANDA|TRASLADO.*DEMAND|CORRE TRASLADO|CÓRRASE TRASLADO'
    )
    if dias_traslado is not None and not re.search(r'CONTEST|EVACU[AÓ]|INFORMA', acts_texto):
        if tipo == 'amparo_mora' and dias_traslado > 12:
            return ('urgent',
                f'Amparo por mora: el traslado para el informe del art. 28 tiene {dias_traslado} días. '
                'El Estado debía responder en 5 días hábiles (Art. 28 Ley 19.549). '
                'Presentar escrito solicitando se tenga por no evacuado y se dicte sentencia de inmediato.')
        elif tipo == 'recurso_directo' and dias_traslado > 12:
            return ('urgent',
                f'Recurso directo: el traslado para informe tiene {dias_traslado} días. '
                'La DNM debía responder en 5 días hábiles (Art. 77 Ley 25.871). '
                'Solicitar se tenga por vencido el plazo y se llame a sentencia.')
        elif tipo == 'inconstitucionalidad' and dias_traslado > 25:
            return ('urgent',
                f'Proceso de conocimiento: el traslado de la demanda tiene {dias_traslado} días. '
                'El Estado debía contestar en 15 días hábiles. '
                'Verificar si contestó; si no lo hizo, pedir que se tenga por vencido el plazo.')

    # Llamado a sentencia sin resolución > 90 días
    dias_llamado = dias_desde_acto(
        r'LLAMADO A SENTENCIA|LL[AÁ]MESE.*SENTENCIA|AUTOS.*SENTENCIA|AUTOS PARA SENTENCIA'
    )
    if dias_llamado and dias_llamado > 90:
        return ('urgent',
            f'El juzgado llamó a sentencia hace {dias_llamado} días sin resolver. '
            'Presentar escrito instando urgente al juzgado que dicte sentencia. '
            'Verificar si existe alguna diligencia pendiente que trabe el dictado.')

    # Amparo mora: informe evacuado hace > 60 días sin sentencia
    if tipo == 'amparo_mora':
        dias_informe = dias_desde_acto(
            r'EVAC[UÚ]A INFORME|EVACU[OÓ].*INFORME|SE PRESENTA.*INFORME|INFORMA.*ART.*28'
        )
        if dias_informe and dias_informe > 60:
            return ('urgent',
                f'Amparo por mora: el Estado evacuó el informe del art. 28 hace {dias_informe} días. '
                'El juzgado debería haber dictado sentencia (proceso sumarísimo, Art. 498 CPCCN). '
                'Presentar escrito instando urgente que dicte sentencia.')

    # Sin movimiento prolongado → riesgo de caducidad
    if dias_sin_mov and dias_sin_mov > 150:
        return ('urgent',
            f'El expediente lleva {dias_sin_mov} días sin movimiento. '
            'Riesgo inmediato de caducidad de instancia (6 meses, Art. 310 CPCCN). '
            'Presentar escrito instando el proceso de inmediato.')

    # ── ATENCIÓN ──────────────────────────────────────────────────────────────

    if en_camara:
        dias_en_camara = dias_desde_acto(
            r'RECEPCION PASE|RECEPCI[OÓ]N.*PASE|ELEVACI[OÓ]N|ELEV[OÓ].*ALZADA|CONCEDASE EN RELACI[OÓ]N'
        )
        if dias_en_camara is not None and dias_en_camara < 20:
            return ('watch',
                f'El expediente llegó a Cámara hace {dias_en_camara} días. '
                'Verificar la notificación de radicación — desde ahí corren 10 días hábiles para '
                'expresar agravios (Art. 259 CPCCN). Si no se expresaron, actuar urgente.')
        return ('watch',
            'El expediente está en Cámara. Verificar que los agravios hayan sido presentados '
            'y contestados (Art. 259 CPCCN — 10 días hábiles desde la notificación de radicación). '
            'Si todo está sustanciado, presentar escrito instando se dicte sentencia.')

    # Cédula notificada reciente → puede haber plazo corriendo
    dias_cedula = dias_desde_acto(r'C[EÉ]DULA NOTIFICADA|NOTIF[IÍ]QUESE|NOTIFICACI[OÓ]N')
    if dias_cedula is not None and dias_cedula < 20:
        return ('watch',
            f'Cédula o notificación registrada hace {dias_cedula} días. '
            'Verificar el contenido — puede haber un plazo procesal corriendo. '
            'Actuar dentro del plazo correspondiente al tipo de proceso.')

    # Dictamen fiscal → puede requerir respuesta o traslado
    dias_dictamen = dias_desde_acto(r'DICTAMEN FISCAL|DICTAMEN DEL MINISTERIO|VISTA FISCAL')
    if dias_dictamen is not None and dias_dictamen < 30:
        return ('watch',
            f'Dictamen fiscal registrado hace {dias_dictamen} días. '
            'Verificar el contenido — si es adverso, puede requerir contestación. '
            'Si es favorable, presentar escrito para que el juzgado lo tenga presente al sentenciar.')

    # Amparo mora: traslado para informe art. 28 corriendo
    if tipo == 'amparo_mora':
        dias_trasl_amp = dias_desde_acto(
            r'TRASLADO.*INFORME.*ART.*28|INFORME.*ART.*28|CORRE TRASLADO|CÓRRASE TRASLADO'
        )
        if dias_trasl_amp is not None and dias_trasl_amp <= 12:
            return ('watch',
                f'Amparo por mora: se corrió traslado para el informe del art. 28 hace {dias_trasl_amp} días. '
                'El Estado tiene 5 días hábiles para responder (Art. 28 Ley 19.549). '
                'Monitorear si contesta en término; si no lo hace, solicitar se tenga por no evacuado.')

    # "Concédase en relación" → expediente en tránsito a Cámara
    if re.search(r'CONC[EÉ]DASE EN RELACI[OÓ]N|CONCEDIDO EN RELACI[OÓ]N', acts_texto):
        return ('watch',
            'Se concedió el recurso en relación. El expediente está en tránsito hacia Cámara. '
            'Verificar la notificación de radicación ante el tribunal de alzada — '
            'desde ahí corren 10 días hábiles para expresar agravios (Art. 259 CPCCN).')

    # Sin movimiento moderado → impulsar antes de que caduque
    if dias_sin_mov and dias_sin_mov > 90:
        return ('watch',
            f'El expediente lleva {dias_sin_mov} días sin movimiento. '
            'Presentar escrito instando el proceso para evitar la caducidad de instancia '
            '(Art. 310 CPCCN — caduca a los 6 meses de inactividad en primera instancia).')

    # ── NORMAL ────────────────────────────────────────────────────────────────

    if tipo == 'recurso_directo':
        sugerencia = (
            'Recurso directo DNM en trámite. Verificar que todas las notificaciones sean respondidas '
            'en término (5 días hábiles, Art. 77 Ley 25.871). Monitorear avance hacia sentencia.'
        )
    elif tipo == 'amparo_mora':
        sugerencia = (
            'Amparo por mora en trámite. Verificar que el juzgado haya admitido la demanda '
            'y corrido traslado al organismo para el informe del art. 28 (5 días hábiles, Ley 19.549). '
            'Una vez evacuado el informe, el juzgado debe dictar sentencia sin más trámite.'
        )
    else:  # inconstitucionalidad
        dias_inicio = dias_desde_acto(
            r'INICIO.*DEMANDA|PROMUEVE.*DEMANDA|CAMBIO.*INICIO|DEMANDA PROMOVIDA|INICIA DEMANDA'
        )
        if dias_inicio is not None and dias_inicio < 30:
            sugerencia = (
                f'Proceso de conocimiento en etapa inicial ({dias_inicio} días desde el inicio). '
                'Verificar que el juzgado corra traslado de la demanda al Estado. '
                'Una vez notificado, el Estado tiene 15 días hábiles para contestar.'
            )
        else:
            sugerencia = (
                'Proceso de conocimiento en trámite. Monitorear las actuaciones, '
                'verificar plazos de contestación de traslados y preparar escritos de impulso '
                'si el expediente está demorado.'
            )

    return ('normal', sugerencia)


# ── Detección de posible cierre ────────────────────────────────────────────────

def detectar_posible_cierre(case):
    """
    Detecta si un caso podría estar cerrado o terminado.
    Retorna (True, motivo) o (False, '').

    Jerarquía de análisis:
    1. Indicadores de expediente VIVO en tránsito (ping-pong de competencia) → nunca cerrar
    2. Remisión DEFINITIVA a otro fuero → cerrado
    3. Indicadores clásicos de cierre (archivo, sentencia firme, etc.)
    """
    estado   = (case.get('proceduralStage') or '').upper()
    acts     = case.get('recentActuaciones') or []
    acts_txt = ' '.join(a.get('descripcion', '') for a in acts[:10]).upper()

    # ── 1. EXPEDIENTE VIVO EN TRÁNSITO (ping-pong de competencia) ─────────────
    # Estos movimientos indican que el expediente está activo aunque "viaje".
    # Tienen prioridad absoluta: si aparecen, nunca marcar como muerto.
    vivo_patterns = [
        r'VUELVEN AUTOS DE C[AÁ]MARA',
        r'POR DEVUELTOS',
        r'C[UÚ]MPLASE',
        r'ELEVE.*ALZADA.*COMPET|ALZADA.*DIRIMIR',
        r'CONFLICTO.*COMPETENCIA|COMPETENCIA.*CONFLICTO',
        r'TRABA.*CONFLICTO',
        r'NO ACEPTA.*COMPETENCIA',
        r'DECLINA.*COMPETENCIA',
    ]
    for pat in vivo_patterns:
        if re.search(pat, acts_txt):
            return False, ''

    # ── 2. REMISIÓN DEFINITIVA A OTRO FUERO ───────────────────────────────────
    # "Remisión" sola NO alcanza — necesita indicar firmeza o destino final.
    if re.search(r'INCOMPETENCIA.*FIRME|FIRME.*INCOMPETENCIA|HABIENDO QUEDADO FIRME', acts_txt):
        return True, 'Incompetencia firme: el expediente se remitió definitivamente a otro fuero.'

    if re.search(r'H[AÁ]GASE SABER.*RADICACI[OÓ]N|RADICACI[OÓ]N EN EL FUERO', acts_txt):
        return True, 'El expediente ya fue recibido y radicado en otro fuero.'

    if re.search(r'REM[IÍ]TASE LA TOTALIDAD.*ACTUACIONES|REMISI[OÓ]N DEFINITIVA|REMISI[OÓ]N FIRME', acts_txt):
        return True, 'Remisión definitiva de actuaciones a otro fuero.'

    # ── 3. INDICADORES CLÁSICOS DE CIERRE ─────────────────────────────────────
    if re.search(r'\bARCHIVADO\b|\bARCHIVO\b', estado):
        return True, 'El expediente figura como ARCHIVADO.'

    if re.search(r'\bDESISTIMIENTO\b|\bDESISTI[OÓ]\b', acts_txt):
        return True, 'Se registró un DESISTIMIENTO en las actuaciones.'

    if re.search(r'CUMPLIMIENTO.*SENTENCIA|SENTENCIA.*CUMPLID', acts_txt):
        return True, 'Se registró CUMPLIMIENTO DE SENTENCIA.'

    if re.search(r'SENTENCIA FIRME', acts_txt):
        return True, 'Se registró SENTENCIA FIRME.'

    if re.search(r'CADUCIDAD.*INSTANCIA|INSTANCIA.*CADUCID', acts_txt):
        return True, 'Se registró CADUCIDAD DE INSTANCIA.'

    return False, ''


# ── Notificaciones ────────────────────────────────────────────────────────────

_NOTIF_LOCK = threading.Lock()

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
            print(f"    [NOTIF] [WARN] No se pudo adquirir lock de notifications.json")


# ── Worker (hilo de trabajo) ───────────────────────────────────────────────────

def run_worker(worker_id, cases_subset, cases):
    """
    Corre en su propio hilo. Tiene su propio Chrome driver y OCR.
    Actualiza 'cases' (lista compartida) con lock.
    """
    ocr    = ddddocr.DdddOcr(show_ad=False)
    driver = setup_driver()
    updated = 0

    try:
        for case in cases_subset:
            name = case.get('clientName') or case.get('caseNumber', '?')
            print(f'  [W{worker_id}] [{case.get("caseNumber","?")}] {name}')

            result, driver = query_case(driver, case, ocr)

            # Pausa entre casos — evita consultas en ráfaga al PJN
            time.sleep(8)

            if result and result.get('queried'):
                with _CASES_LOCK:
                    idx = next((i for i, c in enumerate(cases) if c['id'] == case['id']), None)
                    if idx is not None:
                        # Guardar estado anterior para comparar cambios
                        old_last_action = cases[idx].get('lastAction', '')
                        old_urgency     = cases[idx].get('urgency', 'normal')
                        old_closed      = cases[idx].get('possibleClosed', False)

                        if result.get('tribunal'):
                            cases[idx]['tribunal']        = result['tribunal']
                        if result.get('proceduralStage'):
                            cases[idx]['proceduralStage'] = result['proceduralStage']
                        if result.get('caratula'):
                            caratula = result['caratula']
                            cases[idx]['caseTitle'] = caratula
                            # Extraer clientName de la carátula si está vacío ("APELLIDO, NOMBRE c/ ...")
                            if not cases[idx].get('clientName', '').strip():
                                m = re.split(r'\s+[Cc]/', caratula)
                                if len(m) > 1:
                                    cases[idx]['clientName'] = m[0].strip()
                            # Actualizar categoría y tipoAccion usando jerarquía correcta
                            tipo = detectTipoAccion(cases[idx])
                            cases[idx]['tipoAccion'] = tipo
                            cases[idx]['category'] = 'amparo' if tipo == 'amparo_mora' else 'constitutional'
                        if result.get('lastAction'):
                            cases[idx]['lastAction']      = result['lastAction']
                        if result.get('lastActionDate'):
                            cases[idx]['lastActionDate']  = result['lastActionDate']
                        if result.get('notes'):
                            cases[idx]['notes']           = result['notes']
                        if result.get('recentActuaciones') is not None:
                            cases[idx]['recentActuaciones'] = result['recentActuaciones']
                        if result.get('caseAnalysis'):
                            cases[idx]['caseAnalysis'] = result['caseAnalysis']
                        cases[idx]['lastUpdated'] = datetime.now().isoformat()
                        urgency, suggestion = evaluar_urgencia(cases[idx])
                        cases[idx]['urgency']    = urgency
                        cases[idx]['suggestion'] = suggestion
                        posible_cierre, motivo_cierre = detectar_posible_cierre(cases[idx])
                        cases[idx]['possibleClosed']       = posible_cierre
                        cases[idx]['possibleClosedReason'] = motivo_cierre
                        if posible_cierre:
                            print(f"    [W{worker_id}] [REVISAR] {motivo_cierre}")
                        else:
                            print(f"    [W{worker_id}] [{urgency.upper()}] {suggestion[:80]}...")
                        updated += 1
                        # Guardar progreso inmediatamente
                        if _acquire_file_lock(_CASES_FILE_LOCK_PATH):
                            try:
                                _atomic_write_json(CASES_FILE, cases)
                            finally:
                                _release_file_lock(_CASES_FILE_LOCK_PATH)
                        else:
                            print(f"    [W{worker_id}] [WARN] No se pudo adquirir lock de cases.json")

                        # ── Generar notificaciones por cambios detectados ──
                        new_last_action = cases[idx].get('lastAction', '')
                        new_urgency     = cases[idx].get('urgency', 'normal')

                        # Nueva actuación en el expediente
                        if new_last_action and new_last_action != old_last_action:
                            fecha = cases[idx].get('lastActionDate', '')
                            msg = f"Nueva actuación: {fecha} — {new_last_action[:200]}"
                            add_notification(cases[idx], 'new_actuacion', msg)
                            print(f"    [NOTIF] Nueva actuación registrada")

                        # Urgencia escaló a urgente
                        if new_urgency == 'urgent' and old_urgency != 'urgent':
                            msg = cases[idx].get('suggestion', '')[:250]
                            add_notification(cases[idx], 'urgency_urgent', msg)
                            print(f"    [NOTIF] Urgencia: URGENTE")

                        # Urgencia escaló a atención (solo si antes era normal)
                        elif new_urgency == 'watch' and old_urgency == 'normal':
                            msg = cases[idx].get('suggestion', '')[:250]
                            add_notification(cases[idx], 'urgency_watch', msg)

                        # Posible cierre detectado (nuevo)
                        if cases[idx].get('possibleClosed') and not old_closed:
                            msg = cases[idx].get('possibleClosedReason', '')
                            add_notification(cases[idx], 'possible_closed', msg)
                            print(f"    [NOTIF] Posible cierre detectado")
            else:
                print(f'  [W{worker_id}] Sin resultados para {case.get("caseNumber","?")}')

    finally:
        quit_driver(driver)

    print(f'  [W{worker_id}] Terminado — {updated} casos actualizados.')
    return updated


# ── Main ───────────────────────────────────────────────────────────────────────

def es_dia_habil():
    """Retorna False si hoy es fin de semana o feriado nacional argentino."""
    hoy = datetime.now().date()

    # Fin de semana
    if hoy.weekday() >= 5:  # 5=sábado, 6=domingo
        return False

    # Feriados nacionales Argentina 2026
    feriados_2026 = {
        (2026,  1,  1),  # Año Nuevo
        (2026,  2, 16),  # Carnaval
        (2026,  2, 17),  # Carnaval
        (2026,  3, 23),  # Puente turístico
        (2026,  3, 24),  # Día de la Memoria
        (2026,  4,  2),  # Día del Veterano de Malvinas
        (2026,  4,  3),  # Viernes Santo
        (2026,  5,  1),  # Día del Trabajador
        (2026,  5, 25),  # Revolución de Mayo
        (2026,  6, 15),  # Puente turístico
        (2026,  6, 17),  # Güemes
        (2026,  6, 19),  # Puente turístico
        (2026,  6, 20),  # Belgrano
        (2026,  7,  9),  # Independencia
        (2026,  8, 17),  # San Martín
        (2026, 10, 12),  # Diversidad Cultural
        (2026, 11, 20),  # Soberanía Nacional
        (2026, 11, 23),  # Puente turístico
        (2026, 12,  8),  # Inmaculada Concepción
        (2026, 12, 25),  # Navidad
    }

    if (hoy.year, hoy.month, hoy.day) in feriados_2026:
        return False

    return True


def main():
    print('=' * 60)
    print('  NegroLex — PJN Auto-Checker (paralelo)')
    print(f'  Workers: {N_WORKERS}')
    print('=' * 60)
    print()

    if AUTO and not es_dia_habil():
        print(f'  Hoy ({datetime.now().strftime("%A %d/%m/%Y")}) no es día hábil — saliendo.')
        return

    if not os.path.exists(CASES_FILE):
        print('No se encontró cases.json. Abrí la app primero.')
        if not AUTO:
            input('\nPresioná Enter para salir.')
        return

    with open(CASES_FILE, encoding='utf-8') as f:
        cases = json.load(f)

    const_cases = [c for c in cases if c.get('category') in ('constitutional', 'amparo')]
    if LIMIT:
        const_cases = const_cases[:LIMIT]
    if not const_cases:
        print('No hay casos constitucionales para consultar.')
        if not AUTO:
            input('\nPresioná Enter para salir.')
        return

    print(f'  {len(const_cases)} casos constitucionales encontrados.')
    print(f'  Distribuyendo entre {N_WORKERS} workers...\n')

    # Distribuir casos en N_WORKERS grupos (round-robin para balancear carga)
    chunks = [[] for _ in range(N_WORKERS)]
    for i, case in enumerate(const_cases):
        chunks[i % N_WORKERS].append(case)

    for i, chunk in enumerate(chunks):
        print(f'  Worker {i+1}: {len(chunk)} casos')
    print()

    total_updated = 0

    with ThreadPoolExecutor(max_workers=N_WORKERS) as executor:
        futures = {
            executor.submit(run_worker, i + 1, chunk, cases): i
            for i, chunk in enumerate(chunks) if chunk
        }
        for future in as_completed(futures):
            try:
                total_updated += future.result()
            except Exception as e:
                print(f'  [ERROR] Worker falló: {e}')

    print()
    print('=' * 60)
    print(f'  Listo. {total_updated} casos actualizados.')
    print('=' * 60)

    if not AUTO:
        try:
            input('\nPresioná Enter para salir.')
        except EOFError:
            pass


# ── Abrir expediente en PJN para visualización ────────────────────────────────

def open_pjn_for_viewing(case_number):
    """
    Abre Chrome visible, navega a scw.pjn.gov.ar, llena fuero/número/año,
    resuelve el captcha y deja el navegador abierto para el usuario.
    NO extrae datos ni actualiza cases.json.
    """
    code, number, year = parse_case_number(case_number)
    if not code:
        print(f"  [open-pjn] Expediente no reconocido: {case_number}")
        return

    print(f"  [open-pjn] Abriendo {case_number} (fuero={code}, nro={number}, año={year})...")

    # Chrome VISIBLE — sin --window-position
    opts = webdriver.ChromeOptions()
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',
        {'source': "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"})

    ocr = ddddocr.DdddOcr(show_ad=False)

    for attempt in range(1, 4):
        try:
            driver.get(PJN_URL)

            if not wait_for(driver, 15, EC.presence_of_element_located(
                    (By.ID, 'formPublica:camaraNumAni'))):
                print(f"  [open-pjn] Página no cargó, reintento {attempt}/3...")
                continue

            Select(driver.find_element(By.ID, 'formPublica:camaraNumAni')).select_by_value(code)
            driver.find_element(By.ID, 'formPublica:numero').send_keys(number)
            driver.find_element(By.ID, 'formPublica:anio').send_keys(year)

            if not solve_captcha(driver, ocr):
                print(f"  [open-pjn] Captcha fallido, reintento {attempt}/3...")
                driver.get(PJN_URL)
                continue

            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(By.ID, 'formPublica:buscarPorNumeroButton')
            )

            if not wait_for(driver, 30, EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(text(),'Expediente:') or contains(text(),'Datos Generales')]"))):
                print(f"  [open-pjn] Sin resultados, reintento {attempt}/3...")
                continue

            print(f"  [open-pjn] Listo — {case_number} abierto en el navegador.")
            # NO cerramos el driver: el usuario interactúa con el resultado
            return

        except Exception as e:
            print(f"  [open-pjn] Error: {str(e).split(chr(10))[0][:100]}")
            try:
                driver.title
            except Exception:
                print("  [open-pjn] Chrome cerrado inesperadamente.")
                return

    print(f"  [open-pjn] No se pudo abrir {case_number} después de 3 intentos.")
    # Dejamos el browser abierto aunque sea en la home


if __name__ == '__main__':
    import sys
    AUTO = '--auto' in sys.argv
    LIMIT = 0
    for _i, _arg in enumerate(sys.argv):
        if _arg == '--limit' and _i + 1 < len(sys.argv):
            LIMIT = int(sys.argv[_i + 1])

    if '--open-pjn' in sys.argv:
        _idx = sys.argv.index('--open-pjn')
        _case_number = sys.argv[_idx + 1] if _idx + 1 < len(sys.argv) else ''
        if _case_number:
            open_pjn_for_viewing(_case_number)
        import sys as _sys; _sys.exit(0)

    main()
