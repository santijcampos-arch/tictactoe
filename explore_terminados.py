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


# ── Driver ─────────────────────────────────────────────────────────────────────

def setup_driver():
    opts = webdriver.ChromeOptions()
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',
        {'source': "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"})
    return driver


def solve_captcha(driver, ocr):
    try:
        WebDriverWait(driver, 15).until(lambda d: any(
            'captcha.pjn.gov.ar' in (f.get_attribute('src') or '')
            for f in d.find_elements(By.TAG_NAME, 'iframe')
        ))
    except TimeoutException:
        return False

    frame = next((f for f in driver.find_elements(By.TAG_NAME, 'iframe')
                  if 'captcha.pjn.gov.ar' in (f.get_attribute('src') or '')), None)
    if not frame:
        return False

    driver.switch_to.frame(frame)
    try:
        btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//*[contains(text(),'VER DESAF')]"))
        )
        driver.execute_script("arguments[0].click();", btn)
        img_el = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.TAG_NAME, 'img'))
        )
    except TimeoutException:
        driver.switch_to.default_content()
        return False

    result = ocr.classification(img_el.screenshot_as_png)
    digits = re.sub(r'\D', '', result)
    if len(digits) != 4:
        driver.switch_to.default_content()
        return False

    try:
        inp = driver.find_element(By.XPATH, "//input[@type='text']")
        inp.clear()
        inp.send_keys(digits)
        inp.send_keys(Keys.RETURN)
    except NoSuchElementException:
        driver.switch_to.default_content()
        return False

    try:
        WebDriverWait(driver, 20).until_not(
            EC.visibility_of_element_located((By.XPATH, "//*[contains(text(),'ENVIANDO')]"))
        )
    except TimeoutException:
        pass

    driver.switch_to.default_content()
    return True


def navegar_a_caso(driver, ocr, case_number):
    m = re.match(r'0*(\d+)/(\d{4})', case_number.strip())
    if not m:
        return False
    number, year = m.group(1), m.group(2)

    for attempt in range(4):
        try:
            driver.get(PJN_URL)
            if not WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.ID, 'formPublica:camaraNumAni'))
            ):
                continue
            Select(driver.find_element(By.ID, 'formPublica:camaraNumAni')).select_by_value(CCF_CODE)
            driver.find_element(By.ID, 'formPublica:numero').send_keys(number)
            driver.find_element(By.ID, 'formPublica:anio').send_keys(year)
            if not solve_captcha(driver, ocr):
                continue
            driver.execute_script(
                "arguments[0].click();",
                driver.find_element(By.ID, 'formPublica:buscarPorNumeroButton')
            )
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.XPATH,
                    "//*[contains(text(),'Expediente:') or contains(text(),'Datos Generales')]"))
            )
            body = driver.find_element(By.TAG_NAME, 'body').text
            if re.search(r'no\s+se\s+encontr[oo]|sin\s+resultado', body, re.IGNORECASE):
                return False
            return True
        except TimeoutException:
            pass
        except Exception as e:
            plog(f'    [{case_number}] error intento {attempt+1}: {str(e)[:60]}')
    return False


# ── Extracción de datos ────────────────────────────────────────────────────────

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
            plog("    [ACT] No se encontró tabla de actuaciones")
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

        plog(f"    [ACT] {len(actuaciones)} actuaciones extraídas")
    except Exception as e:
        plog(f"    [ACT] Error: {e}")

    # Devolver de más antigua a más reciente (el PJN muestra más reciente primero)
    return list(reversed(actuaciones))


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


# ── Scraping ───────────────────────────────────────────────────────────────────

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
            ya_ok = False
            if os.path.exists(RAW_FILE):
                try:
                    with _RAW_LOCK:
                        with open(RAW_FILE, encoding='utf-8') as f:
                            existing = json.load(f)
                    grupo_data = existing.get(grupo_key, [])
                    ya_ok = any(
                        r['case_number'] == numero and r.get('error') is None
                        for r in grupo_data
                    )
                except Exception:
                    pass
            if ya_ok:
                plog(f'  [W{worker_id}] [{grupo_key} {numero}] ya scrapeado — saltando')
                continue

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
