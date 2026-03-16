"""
LexCase — PJN Auto-Checker (paralelo)
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
PJN_URL    = 'https://scw.pjn.gov.ar/scw/home.seam'

# ── Configuración de paralelismo ──────────────────────────────────────────────
N_WORKERS = 2  # cantidad de Chrome en paralelo
MAX_CHROME_INSTANCES = 5  # si se superan, el programa se termina

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


# ── Helpers ───────────────────────────────────────────────────────────────────

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

            return parse_result(driver, case_number), driver

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


def get_actuaciones_con_documentos(driver):
    """
    Encuentra la tabla de actuaciones en la página actual.
    Para las últimas 5 filas, extrae el texto y lee documentos si los hay.
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

    prompt = f"""Sos un asistente jurídico. Analizá las siguientes actuaciones de un expediente judicial argentino y explicá en español, de forma clara y concisa (máximo 5 oraciones), qué está pasando en el caso: cuál es el estado actual, qué pasó recientemente y si hay algo urgente o importante a tener en cuenta.

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

def parse_result(driver, case_number):
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

    actuaciones = get_actuaciones_con_documentos(driver)

    if not actuaciones:
        fechas = re.findall(r'(\d{2}/\d{2}/\d{4})', body_text)
        if fechas:
            ultima = fechas[-1]
            actuaciones.append({'fecha': ultima, 'descripcion': ultima, 'documento': ''})
            print(f"    [ACT] Fallback fecha: {ultima}")
        else:
            print("    [ACT] Sin actuaciones encontradas")

    if actuaciones:
        ultima = actuaciones[0]
        result['lastAction']     = ultima['descripcion'][:300]
        result['lastActionDate'] = ultima['fecha']

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
    estado      = (case.get('proceduralStage') or '').upper()
    tribunal    = (case.get('tribunal') or '').upper()
    caratula    = (case.get('caseTitle') or case.get('caratula') or '').upper()
    notas       = (case.get('notes') or '').upper()
    acts_texto  = textos_actuaciones(case)
    last_date   = case.get('lastActionDate')
    dias_sin_mov = dias_desde(last_date)

    def dias_desde_acto(patron):
        acts = case.get('recentActuaciones') or []
        for a in reversed(acts):
            desc = (a.get('descripcion') or '').upper()
            if re.search(patron, desc, re.IGNORECASE):
                d = dias_desde(a.get('fecha'))
                if d is not None:
                    return d
        return None

    en_camara = 'CAMARA' in tribunal

    # ── URGENTES ──────────────────────────────────────────────────────────────

    if re.search(r'SENTENCIA DE C[AÁ]MARA|SENT.*CAM', acts_texto + notas):
        return ('urgent',
            'Hay SENTENCIA DE CÁMARA registrada en las actuaciones. Verificar urgente el contenido. '
            'Si es desfavorable, el plazo para recurso extraordinario ante la CSJN es de 10 días '
            'desde la notificación (Art. 257 CPCCN). Controlar si el plazo no venció.')

    dias_traslado = dias_desde_acto(r'TRASLADO DE DEMANDA|TRASLADO.*DEMAND')
    if dias_traslado and dias_traslado > 75:
        if not re.search(r'CONTEST|PRESENTA.*CONTEST', acts_texto):
            return ('urgent',
                f'El traslado de la demanda se realizó hace {dias_traslado} días. '
                'El Estado tiene 60 días para contestar (Art. 338 CPCCN) y ese plazo ya venció. '
                'Presentar escrito pidiendo que se tenga por vencido el plazo y se fije audiencia preliminar.')

    es_amparo_mora = 'AMPARO POR MORA' in caratula
    if es_amparo_mora:
        dias_informe = dias_desde_acto(r'EVAC[UÚ]A INFORME|SE PRESENTA.*INFORME|INFORME.*ART.*28')
        if dias_informe and dias_informe > 60:
            return ('urgent',
                f'Amparo por mora. El Estado evacuó el informe del art. 28 hace {dias_informe} días. '
                'El juzgado debería haber dictado sentencia (Art. 498 CPCCN - proceso sumarísimo). '
                'Presentar escrito instando urgente al juzgado que dicte sentencia.')

    dias_llamado = dias_desde_acto(r'LLAMADO A SENTENCIA|LLAMESE.*SENTENCIA|AUTOS.*SENTENCIA')
    if dias_llamado and dias_llamado > 90:
        return ('urgent',
            f'El juzgado llamó a sentencia hace {dias_llamado} días. '
            'Presentar escrito instando al juzgado que dicte sentencia. '
            'Verificar si hay alguna diligencia pendiente que trabe el dictado.')

    if dias_sin_mov and dias_sin_mov > 150:
        return ('urgent',
            f'El expediente lleva {dias_sin_mov} días sin movimiento. '
            'Presentar escrito instando el proceso. Verificar si el Estado contestó la demanda; '
            'si no lo hizo dentro de los 60 días, pedir que se tenga por vencido el plazo '
            'y se fije audiencia preliminar (Art. 338 CPCCN).')

    # ── ATENCIÓN ──────────────────────────────────────────────────────────────

    if en_camara:
        dias_en_camara = dias_desde_acto(r'RECEPCION PASE|RECEPCI[OÓ]N.*PASE')
        if dias_en_camara and dias_en_camara < 20:
            return ('watch',
                f'El expediente llegó a Cámara hace {dias_en_camara} días. '
                'Verificar la notificación de radicación — desde ahí corren 10 días para expresar '
                'agravios (Art. 259 CPCCN). Si no se expresaron, actuar urgente.')
        return ('watch',
            'El expediente está en Cámara. Verificar que los agravios hayan sido presentados '
            'y contestados (Art. 259 CPCCN - 10 días desde notificación de radicación). '
            'Si todo está sustanciado, presentar escrito instando que se dicte sentencia.')

    dias_cedula = dias_desde_acto(r'CEDULA|C[EÉ]DULA|DICTAMEN')
    if dias_cedula is not None and dias_cedula < 30:
        return ('watch',
            f'Cédula o dictamen registrado hace {dias_cedula} días. '
            'El juzgado tiene los elementos para resolver. '
            'Si no hay resolución en los próximos días, presentar escrito instando.')

    if es_amparo_mora:
        if re.search(r'TRASLADO.*INFORME.*ART.*28|INFORME.*ART.*28', acts_texto):
            return ('watch',
                'Amparo por mora. Se corrió traslado para el informe del art. 28. '
                'Verificar si el Estado presentó el informe en el plazo fijado. '
                'Si no lo hizo, solicitar que se tenga por no presentado y se dicte sentencia.')

    if dias_sin_mov and dias_sin_mov > 90:
        return ('watch',
            f'El expediente lleva {dias_sin_mov} días sin movimiento. '
            'Presentar escrito instando el proceso para evitar la caducidad de la instancia '
            '(Art. 310 CPCCN — caduca a los 6 meses de inactividad en primera instancia).')

    # ── NORMAL ────────────────────────────────────────────────────────────────

    if 'RECURSO DIRECTO' in caratula:
        sugerencia = ('Recurso directo DNM en trámite normal. Monitorear avance del expediente '
                      'y verificar que todas las notificaciones sean respondidas en término.')
    elif es_amparo_mora:
        sugerencia = ('Amparo por mora en trámite. Verificar que el juzgado haya admitido la demanda '
                      'y corrido traslado al organismo para el informe del art. 28.')
    elif 'MERAMENTE DECLARATIVA' in caratula or 'INCONSTITUCIONALIDAD' in caratula:
        dias_inicio = dias_desde_acto(r'INICIO.*DEMANDA|PROMUEVE.*DEMANDA|CAMBIO.*INICIO')
        if dias_inicio and dias_inicio < 70:
            vence_en = 60 - dias_inicio
            if vence_en > 0:
                sugerencia = (f'Acción declarativa en etapa inicial. El Estado tiene 60 días para contestar '
                              f'la demanda (Art. 338 CPCCN) — vencen en aproximadamente {vence_en} días. '
                              f'Monitorear si contesta en término. Verificar si se solicitó medida cautelar.')
            else:
                sugerencia = ('El plazo de 60 días del Estado para contestar venció. '
                              'Verificar si contestó; si no lo hizo, pedir que se tenga por vencido el plazo.')
        else:
            sugerencia = ('En trámite normal. Monitorear avance del expediente.')
    elif 'PROCESO DE CONOCIMIENTO' in caratula:
        sugerencia = ('Proceso de conocimiento en trámite. Monitorear las actuaciones y verificar '
                      'que los plazos procesales estén al día.')
    else:
        sugerencia = ('En trámite normal. Monitorear el avance del expediente. '
                      'Presentar escrito instando si no hay movimiento en 30 días.')

    return ('normal', sugerencia)


# ── Detección de posible cierre ────────────────────────────────────────────────

def detectar_posible_cierre(case):
    """
    Detecta si un caso podría estar cerrado o terminado.
    Retorna (True, motivo) o (False, '').
    Solo usa indicadores fuertes para evitar falsos positivos.
    """
    estado   = (case.get('proceduralStage') or '').upper()
    acts     = case.get('recentActuaciones') or []
    acts_txt = ' '.join(a.get('descripcion', '') for a in acts).upper()

    if re.search(r'\bARCHIVADO\b|\bARCHIVO\b', estado):
        return True, 'El expediente figura como ARCHIVADO.'

    if re.search(r'\bDESISTIMIENTO\b|\bDESISTIO\b|\bDESISTIÓ\b', acts_txt):
        return True, 'Se registró un DESISTIMIENTO en las actuaciones.'

    if re.search(r'CUMPLIMIENTO.*SENTENCIA|SENTENCIA.*CUMPLID', acts_txt):
        return True, 'Se registró CUMPLIMIENTO DE SENTENCIA en las actuaciones.'

    if re.search(r'SENTENCIA FIRME', acts_txt):
        return True, 'Se registró SENTENCIA FIRME en las actuaciones.'

    if re.search(r'CADUCIDAD.*INSTANCIA|INSTANCIA.*CADUCID', acts_txt):
        return True, 'Se registró CADUCIDAD DE INSTANCIA en las actuaciones.'

    return False, ''


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

            if result and result.get('queried'):
                with _CASES_LOCK:
                    idx = next((i for i, c in enumerate(cases) if c['id'] == case['id']), None)
                    if idx is not None:
                        if result.get('tribunal'):
                            cases[idx]['tribunal']        = result['tribunal']
                        if result.get('proceduralStage'):
                            cases[idx]['proceduralStage'] = result['proceduralStage']
                        if result.get('caratula'):
                            cases[idx]['caseTitle']       = result['caratula']
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
                        with open(CASES_FILE, 'w', encoding='utf-8') as f:
                            json.dump(cases, f, ensure_ascii=False, indent=2)
            else:
                print(f'  [W{worker_id}] Sin resultados para {case.get("caseNumber","?")}')

    finally:
        quit_driver(driver)

    print(f'  [W{worker_id}] Terminado — {updated} casos actualizados.')
    return updated


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print('=' * 60)
    print('  LexCase — PJN Auto-Checker (paralelo)')
    print(f'  Workers: {N_WORKERS}')
    print('=' * 60)
    print()

    if not os.path.exists(CASES_FILE):
        print('No se encontró cases.json. Abrí la app primero.')
        if not AUTO:
            input('\nPresioná Enter para salir.')
        return

    with open(CASES_FILE, encoding='utf-8') as f:
        cases = json.load(f)

    const_cases = [c for c in cases if c.get('category') == 'constitutional']
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


if __name__ == '__main__':
    import sys
    AUTO = '--auto' in sys.argv
    LIMIT = 0
    for _i, _arg in enumerate(sys.argv):
        if _arg == '--limit' and _i + 1 < len(sys.argv):
            LIMIT = int(sys.argv[_i + 1])
    main()
