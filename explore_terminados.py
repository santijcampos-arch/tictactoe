"""
NegroLex — Scraper de expedientes terminados (todos los juzgados CCF)
Scrape las actuaciones de todos los expedientes terminados del PJN para la knowledge base.

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
    # J01-S01
    (1,  1,  '10769/2024'),
    (1,  1,  '10770/2024'),
    (1,  1,  '1111/2025'),
    (1,  1,  '17218/2024'),
    (1,  1,  '20337/2024'),
    (1,  1,  '27063/2024'),
    # J01-S02
    (1,  2,  '12234/2024'),
    (1,  2,  '1504/2025'),
    (1,  2,  '17485/2024'),
    (1,  2,  '19902/2024'),
    (1,  2,  '23380/2024'),
    (1,  2,  '23473/2024'),
    (1,  2,  '27123/2024'),
    (1,  2,  '7103/2020'),
    (1,  2,  '9837/2024'),
    # J02-S03
    (2,  3,  '13226/2024'),
    (2,  3,  '2107/2021'),
    (2,  3,  '2907/2024'),
    # J02-S04
    (2,  4,  '14206/2024'),
    (2,  4,  '14525/2023'),
    (2,  4,  '17792/2023'),
    (2,  4,  '5499/2023'),
    # J03-S05
    (3,  5,  '12233/2024'),
    (3,  5,  '14937/2024'),
    (3,  5,  '17254/2024'),
    (3,  5,  '19817/2024'),
    (3,  5,  '20097/2024'),
    (3,  5,  '2071/2025'),
    (3,  5,  '2072/2025'),
    (3,  5,  '20820/2024'),
    (3,  5,  '3238/2024'),
    (3,  5,  '838/2025'),
    # J03-S06
    (3,  6,  '11054/2024'),
    (3,  6,  '13876/2024'),
    (3,  6,  '14549/2024'),
    (3,  6,  '17254/2024'),
    (3,  6,  '19397/2024'),
    (3,  6,  '22241/2024'),
    (3,  6,  '6317/2023'),
    # J04-S07
    (4,  7,  '24912/2024'),
    (4,  7,  '17076/2024'),
    (4,  7,  '17695/2024'),
    (4,  7,  '17796/2023'),
    (4,  7,  '18747/2022'),
    (4,  7,  '19389/2024'),
    (4,  7,  '8850/2023'),
    (4,  7,  '9758/2024'),
    # J04-S08
    (4,  8,  '2410/2024'),
    (4,  8,  '6285/2024'),
    # J05-S09
    (5,  9,  '15655/2022'),
    (5,  9,  '15660/2022'),
    # J06-S11
    (6, 11,  '13871/2024'),
    (6, 11,  '14940/2024'),
    (6, 11,  '19388/2024'),
    (6, 11,  '22140/2024'),
    (6, 11,  '22297/2024'),
    (6, 11,  '23905/2024'),
    # J06-S12
    (6, 12,  '10644/2024'),
    (6, 12,  '10856/2023'),
    (6, 12,  '16870/2024'),
    (6, 12,  '17722/2023'),
    (6, 12,  '4534/2024'),
    # J07-S13
    (7, 13,  '15806/2024'),
    (7, 13,  '16202/2024'),
    (7, 13,  '16543/2024'),
    (7, 13,  '17439/2024'),
    (7, 13,  '18155/2023'),
    # J07-S14
    (7, 14,  '19165/2024'),
    (7, 14,  '7817/2024'),
    # J08-S15 (ya scrapeados — resume los salteará)
    (8, 15,  '11346/2024'),
    (8, 15,  '13863/2024'),
    (8, 15,  '15131/2024'),
    (8, 15,  '16502/2024'),
    (8, 15,  '17453/2024'),
    (8, 15,  '19404/2024'),
    (8, 15,  '27054/2024'),
    (8, 15,  '27117/2024'),
    (8, 15,  '4333/2023'),
    (8, 15,  '6275/2024'),
    (8, 15,  '9027/2023'),
    (8, 15,  '23471/2024'),
    # J08-S16
    (8, 16,  '13219/2024'),
    (8, 16,  '14301/2023'),
    (8, 16,  '15135/2024'),
    (8, 16,  '17214/2024'),
    (8, 16,  '26367/2024'),
    (8, 16,  '9028/2023'),
    (8, 16,  '922/2025'),
    # J09-S17
    (9, 17,  '10645/2024'),
    (9, 17,  '14771/2024'),
    (9, 17,  '15135/2024'),
    (9, 17,  '20335/2024'),
    (9, 17,  '6314/2024'),
    (9, 17,  '17082/2024'),
    (9, 17,  '19443/2024'),
    # J09-S18
    (9, 18,  '17284/2024'),
    (9, 18,  '24495/2024'),
    (9, 18,  '3237/2025'),
    # J10-S19
    (10, 19, '10859/2023'),
    (10, 19, '14298/2023'),
    (10, 19, '14526/2023'),
    (10, 19, '17317/2024'),
    (10, 19, '19181/2024'),
    (10, 19, '25513/2024'),
    (10, 19, '25558/2024'),
    (10, 19, '6316/2023'),
    (10, 19, '6874/2024'),
    (10, 19, '6887/2024'),
    (10, 19, '9352/2019'),
    (10, 19, '24190/2024'),
    (10, 19, '4218/2025'),
    # J10-S20
    (10, 20, '1013/2025'),
    (10, 20, '12804/2024'),
    (10, 20, '17108/2024'),
    (10, 20, '17222/2024'),
    (10, 20, '17294/2024'),
    (10, 20, '17481/2024'),
    (10, 20, '18162/2023'),
    (10, 20, '19407/2024'),
    (10, 20, '19431/2024'),
    (10, 20, '2401/2024'),
    (10, 20, '4336/2023'),
    (10, 20, '4530/2024'),
    (10, 20, '7811/2024'),
    (10, 20, '8191/2024'),
    (10, 20, '27050/2024'),
    (10, 20, '1871/2025'),
    # J11-S21
    (11, 21, '15658/2022'),
    (11, 21, '17216/2024'),
    (11, 21, '27043/2024'),
    (11, 21, '23908/2024'),
    # J11-S22
    (11, 22, '10091/2024'),
    (11, 22, '10095/2024'),
    (11, 22, '10342/2024'),
    (11, 22, '13607/2021'),
    (11, 22, '25208/2024'),
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

def get_actuaciones_cit(driver, tag='W?'):
    """
    Extrae TODAS las actuaciones del expediente actual paginando.
    Retorna lista de dicts [{tipo, descripcion, fecha, oficina, pdf_href}] de más antigua a más reciente.
    No descarga PDFs — solo lee el texto de la tabla.
    """
    actuaciones = []
    pagina = 1

    while True:
        plog(f'    [ACT] pagina {pagina}...')
        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # Encontrar tabla de actuaciones
        tabla_idx = None
        for i, table in enumerate(soup.find_all('table')):
            if not re.search(r'\d{2}/\d{2}/\d{4}', table.get_text(' ')):
                continue
            rows = table.find_all('tr')
            if len(rows) < 2:
                continue
            header = rows[0].get_text(' ').lower()
            if any(w in header for w in ['tipo', 'descripci', 'fecha', 'actu']):
                tabla_idx = i
                break

        if tabla_idx is None:
            plog(f'    [ACT] No se encontró tabla en página {pagina}')
            break

        # Detectar columnas por encabezado usando Selenium
        sel_tables = driver.find_elements(By.TAG_NAME, 'table')
        if tabla_idx >= len(sel_tables):
            break
        tabla_sel = sel_tables[tabla_idx]
        todas_filas = tabla_sel.find_elements(By.TAG_NAME, 'tr')

        col_fecha = col_tipo = col_desc = col_oficina = -1
        if todas_filas:
            ths = todas_filas[0].find_elements(By.TAG_NAME, 'th')
            if not ths:
                ths = todas_filas[0].find_elements(By.TAG_NAME, 'td')
            for ci, h in enumerate([t.text.strip().lower() for t in ths]):
                if 'fecha' in h:
                    col_fecha = ci
                elif 'tipo' in h:
                    col_tipo = ci
                elif 'descripci' in h or 'detalle' in h:
                    col_desc = ci
                elif 'oficina' in h:
                    col_oficina = ci

        for fila in todas_filas[1:]:
            celdas = [c.text.strip() for c in fila.find_elements(By.TAG_NAME, 'td')]
            if not any(celdas):
                continue

            fecha   = celdas[col_fecha]   if col_fecha   >= 0 and col_fecha   < len(celdas) else ''
            tipo    = celdas[col_tipo]    if col_tipo    >= 0 and col_tipo    < len(celdas) else ''
            desc    = celdas[col_desc]    if col_desc    >= 0 and col_desc    < len(celdas) else ''
            oficina = celdas[col_oficina] if col_oficina >= 0 and col_oficina < len(celdas) else ''

            fecha   = re.sub(r'^Fecha:\s*',          '', fecha).strip()
            tipo    = re.sub(r'^Tipo actuacion:\s*', '', tipo).strip()
            desc    = re.sub(r'^Detalle:\s*',        '', desc).strip()
            oficina = re.sub(r'^Oficina:\s*',        '', oficina).strip()

            if not fecha:
                for t in celdas:
                    fm = re.search(r'\d{1,2}/\d{2}/\d{4}', t)
                    if fm:
                        fecha = fm.group(0)
                        break

            descripcion = ' — '.join(filter(None, [tipo, desc])) if (tipo or desc) else ''

            if descripcion or fecha:
                actuaciones.append({
                    'fecha':       fecha,
                    'tipo':        tipo,
                    'descripcion': descripcion,
                    'oficina':     oficina,
                    'pdf_href':    None,
                })

        # Buscar página siguiente
        siguiente = None
        try:
            cands = driver.find_elements(By.XPATH,
                f"//*[normalize-space(text())='{pagina + 1}']"
                f"[self::a or self::button or self::span or self::td or self::li]"
            )
            for c in cands:
                try:
                    if c.is_displayed():
                        siguiente = c
                        break
                except Exception:
                    pass

            if not siguiente:
                for txt in ['>', '>>', '»', 'Siguiente', 'Next']:
                    cands = driver.find_elements(By.XPATH,
                        f"//*[self::a or self::button or self::span]"
                        f"[contains(normalize-space(text()),'{txt}')]"
                    )
                    for c in cands:
                        try:
                            if c.is_displayed():
                                siguiente = c
                                break
                        except Exception:
                            pass
                    if siguiente:
                        break

            if not siguiente:
                for c in driver.find_elements(By.XPATH,
                    "//a[.//img[contains(@src,'next') or contains(@alt,'next') or contains(@alt,'sig')]]"
                ):
                    try:
                        if c.is_displayed():
                            siguiente = c
                            break
                    except Exception:
                        pass
        except Exception:
            pass

        if not siguiente:
            plog(f'    [ACT] no hay página {pagina + 1} — fin ({len(actuaciones)} total)')
            break

        try:
            plog(f'    [ACT] yendo a página {pagina + 1}...')
            driver.execute_script("arguments[0].click();", siguiente)
            time.sleep(3)
            pagina += 1
        except Exception as e:
            plog(f'    [ACT] error paginando: {e}')
            break

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
