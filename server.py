"""
NegroLex — Local Server
On startup: reads Google Sheets and syncs cases to cases.json.
Also serves migraciones_results.json so the app auto-updates statuses.
Run via START LEXCASE.bat — do not close this window while using the app.
"""

import http.server
import socketserver
import json
import os
import sys
import subprocess
import threading
import time
from datetime import datetime

PORT             = 8000
FOLDER           = os.path.dirname(os.path.abspath(__file__))
CASES_FILE       = os.path.join(FOLDER, 'cases.json')
NOTIF_FILE       = os.path.join(FOLDER, 'notifications.json')
PENDIENTES_FILE  = os.path.join(FOLDER, 'pendientes.json')
PENDIENTES_LOCK  = PENDIENTES_FILE + '.lock'
SHEET_ID         = '1tufvCv5qVUmqma9lzaz-JFAJCp31vTHd2Cns1EXtzOA'
SHEET_NAME       = 'Sheet1'
CONST_SHEET_ID   = '1H0KSyS8hZxikozppoIIn0cM33fBJhboPQ7LvE28JSds'
CONST_SHEET_NAME = 'Contencioso Administrativo'
CIT_SHEET_ID     = '1oScLXq1C4UXnnyTBFgZ1OiApeC9Ley5GvoR1XYpdSvI'
CIT_SHEET_NAME   = 'CCF'

CASES_LOCK = CASES_FILE + '.lock'
NOTIF_LOCK = NOTIF_FILE + '.lock'

def _acquire_lock(lock_path, timeout=10):
    """Adquiere un lock file cross-process via os.O_EXCL (atómico en Windows)."""
    import time
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

def _release_lock(lock_path):
    try:
        os.unlink(lock_path)
    except OSError:
        pass

def _atomic_write(path, data):
    """Escribe data (list/dict) en path de forma atómica via archivo temporal."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _backup(path):
    """Copia path a path.bak si existe."""
    if os.path.exists(path):
        import shutil
        shutil.copy2(path, path + '.bak')

# ── Google Sheets ───────────────────────────────────────────────────────────

def get_sheets_service(write=False):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds_path = os.path.join(FOLDER, 'credentials.json')
    scope = 'https://www.googleapis.com/auth/spreadsheets' if write else 'https://www.googleapis.com/auth/spreadsheets.readonly'
    creds = service_account.Credentials.from_service_account_file(creds_path, scopes=[scope])
    return build('sheets', 'v4', credentials=creds)

def write_case_to_sheet(case):
    """Update vencimiento precaria and estado in Google Sheet for a given expediente."""
    try:
        service = get_sheets_service(write=True)
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=SHEET_NAME
        ).execute()
        values = result.get('values', [])
        if not values:
            return
        headers = [h.strip().lower() for h in values[0]]

        # Find column indexes
        try:
            expte_col   = headers.index('numero de expte')
            venc_col    = next((i for i, h in enumerate(headers) if 'vencimiento' in h), None)
            disp_col    = next((i for i, h in enumerate(headers) if 'disposici' in h), None)
        except ValueError:
            return

        nro = case.get('caseNumber', '')
        for row_idx, row in enumerate(values[1:], start=2):
            padded = row + [''] * (len(headers) - len(row))
            if padded[expte_col].strip() == nro:
                updates = []
                # Update vencimiento precaria
                if venc_col is not None and case.get('nextDeadline'):
                    d = case['nextDeadline']
                    # Convert YYYY-MM-DD to DD/MM/YYYY for the sheet
                    try:
                        from datetime import datetime as dt
                        formatted = dt.strptime(d, '%Y-%m-%d').strftime('%d/%m/%Y')
                    except Exception:
                        formatted = d
                    col_letter = chr(ord('A') + venc_col)
                    updates.append({'range': f'{SHEET_NAME}!{col_letter}{row_idx}', 'values': [[formatted]]})
                # Update disposición
                if disp_col is not None and case.get('disposicion'):
                    col_letter = chr(ord('A') + disp_col)
                    updates.append({'range': f'{SHEET_NAME}!{col_letter}{row_idx}', 'values': [[case['disposicion']]]})

                if updates:
                    service.spreadsheets().values().batchUpdate(
                        spreadsheetId=SHEET_ID,
                        body={'valueInputOption': 'RAW', 'data': updates}
                    ).execute()
                break
    except Exception as e:
        print(f"  [Sheet sync] No se pudo actualizar el sheet: {e}")

def load_rows_from_sheet():
    service = get_sheets_service()
    result  = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=SHEET_NAME
    ).execute()

    values = result.get('values', [])
    if not values:
        return []

    headers = [h.strip().lower() for h in values[0]]
    rows = []
    for row in values[1:]:
        padded = row + [''] * (len(headers) - len(row))
        rows.append({headers[i]: padded[i].strip() for i in range(len(headers))})
    return rows

def parse_sheet_date(raw):
    if not raw:
        return ''
    s = raw.strip().replace('-', '/').replace(' ', '')
    parts = s.split('/')
    if len(parts) != 3:
        return ''
    d, m, y = parts
    if len(y) == 2:
        y = '20' + y
    try:
        return datetime.strptime(f"{d}/{m}/{y}", '%d/%m/%Y').strftime('%Y-%m-%d')
    except ValueError:
        return ''

def generate_id():
    import time, random, string
    return str(int(time.time() * 1000)) + ''.join(random.choices(string.ascii_lowercase, k=4))

def row_to_constitutional_case(row):
    nombre = (row.get('nombre') or '').strip()
    expte  = (row.get('numero de expediente') or row.get('numero de expte') or '').strip()
    return {
        'id':              generate_id(),
        'category':        'constitutional',
        'clientName':      nombre,
        'caseNumber':      expte or '—',
        'caseTitle':       f'Constitucional – {nombre or expte}',
        'tribunal':        '',
        'proceduralStage': 'En trámite',
        'lastAction':      '',
        'status':          'active',
        'nextDeadline':    '',
        'notes':           '',
        'lastUpdated':     datetime.now().isoformat(),
    }

def row_to_case(row):
    nombre      = (row.get('nombre') or '').strip()
    expte       = (row.get('numero de expte') or '').strip()
    stage       = (row.get('intimaciones') or '').strip() or 'En trámite'
    disposicion = (row.get('disposición') or row.get('disposicion') or '').strip()
    notes   = ' | '.join(filter(None, [
        (row.get('antecedentes') or '').strip(),
        (row.get('detalles') or '').strip()
    ]))
    deadline = parse_sheet_date(
        row.get('vencimiento precaria') or row.get('vencimiento precaria ') or ''
    )
    return {
        'id':              generate_id(),
        'category':        'migration',
        'clientName':      nombre,
        'caseNumber':      expte or '—',
        'caseTitle':       f'Migration – {nombre}',
        'proceduralStage': stage,
        'disposicion':     disposicion,
        'status':          'active',
        'nextDeadline':    deadline,
        'notes':           notes,
        'lastUpdated':     datetime.now().isoformat(),
    }

CITIZENSHIP_STAGES = [
    ('pfa_interpol',    'PFA INTERPOL'),
    ('renaper',         'RENAPER'),
    ('cne',             'CNE'),
    ('reincidencia',    'REINCIDENCIA'),
    ('dnm',             'DNM'),
    ('pfa_dactilo',     'PFA DACTILO'),
    ('edicto',          'EDICTO'),
    ('pfa_convenio',    'PFA CONVENIO'),
    ('medios_de_vida',  'Medios de vida'),
    ('fiscal',          'Fiscal'),
    ('sentencia',       'Sentencia'),
    ('carta_ciudadania','Carta de ciudadanía'),
]

def row_to_citizenship_case(row):
    nombre = (row.get('nombre') or row.get('') or '').strip()
    numero = (row.get('número') or row.get('numero') or '').strip()
    fecha  = (row.get('fecha de presentación de la solicitud') or
              row.get('fecha de presentacion de la solicitud') or '').strip()
    juz    = (row.get('juz') or '').strip()
    sec    = (row.get('sec') or '').strip()

    stages = {}
    last_stage_label = ''
    count_completed  = 0
    for key, col_name in CITIZENSHIP_STAGES:
        val = (row.get(col_name.lower()) or '').strip()
        stages[key] = val
        if val:
            last_stage_label = col_name
            count_completed += 1

    carta  = stages.get('carta_ciudadania', '')
    status = 'closed' if carta else 'active'

    return {
        'id':               generate_id(),
        'category':         'citizenship',
        'clientName':       nombre,
        'caseNumber':       numero or '—',
        'caseTitle':        f'Ciudadanía – {nombre}',
        'juzgado':          juz,
        'secretaria':       sec,
        'fechaPresentacion': fecha,
        'proceduralStage':  last_stage_label or 'Sin iniciar',
        'etapasCompletas':  count_completed,
        'stages':           stages,
        'status':           status,
        'nextDeadline':     '',
        'notes':            '',
        'lastUpdated':      datetime.now().isoformat(),
    }

def sync_sheet(sheet_id, sheet_name, row_converter, label):
    """Generic: read one sheet and merge new cases into cases.json."""
    service = get_sheets_service()
    result  = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=sheet_name
    ).execute()
    values = result.get('values', [])
    if not values:
        return []
    headers = [h.strip().lower() for h in values[0]]
    rows = []
    for row in values[1:]:
        padded = row + [''] * (len(headers) - len(row))
        rows.append({headers[i]: padded[i].strip() for i in range(len(headers))})
    return rows

def sync_from_sheet():
    """Read all Google Sheets and merge new cases into cases.json."""
    print("  Conectando con Google Sheets...", flush=True)

    # Load existing cases
    if os.path.exists(CASES_FILE):
        with open(CASES_FILE, encoding='utf-8') as f:
            existing = json.load(f)
    else:
        existing = []

    existing_numbers = {c['caseNumber'] for c in existing}
    added = 0

    # Sync Migration sheet
    try:
        rows = sync_sheet(SHEET_ID, SHEET_NAME, row_to_case, 'Migraciones')
        for row in rows:
            nombre = (row.get('nombre') or '').strip()
            if not nombre:
                continue
            case = row_to_case(row)
            if case['caseNumber'] not in existing_numbers:
                existing.append(case)
                existing_numbers.add(case['caseNumber'])
                added += 1
        print(f"  Migraciones: OK")
    except Exception as e:
        print(f"  Migraciones: error — {e}")

    # Sync Constitutional sheet
    try:
        const_rows = sync_sheet(CONST_SHEET_ID, CONST_SHEET_NAME, row_to_constitutional_case, 'Constitucional')
        for row in const_rows:
            expte = (row.get('numero de expte') or row.get('numero de expediente') or '').strip()
            if not expte:
                continue
            case = row_to_constitutional_case(row)
            if case['caseNumber'] not in existing_numbers:
                existing.append(case)
                existing_numbers.add(case['caseNumber'])
                added += 1
        print(f"  Constitucional: OK")
    except Exception as e:
        print(f"  Constitucional: error — {e}")

    # Sync Citizenship sheet — deduplica por nombre (no siempre hay número de expediente)
    try:
        cit_rows = sync_sheet(CIT_SHEET_ID, CIT_SHEET_NAME, row_to_citizenship_case, 'Ciudadanía')
        if cit_rows:
            print(f"  [Debug] Columnas ciudadanía: {list(cit_rows[0].keys())}")
        existing_cit_names = {c['clientName'].lower().strip() for c in existing if c.get('category') == 'citizenship'}
        for row in cit_rows:
            nombre = (row.get('nombre') or row.get('') or '').strip()
            if not nombre:
                continue
            case = row_to_citizenship_case(row)
            if nombre.lower() not in existing_cit_names:
                existing.append(case)
                existing_cit_names.add(nombre.lower())
                added += 1
        print(f"  Ciudadanía: OK")
    except Exception as e:
        print(f"  Ciudadanía: error — {e}")

    _backup(CASES_FILE)
    if _acquire_lock(CASES_LOCK):
        try:
            _atomic_write(CASES_FILE, existing)
        finally:
            _release_lock(CASES_LOCK)
    else:
        print("  [WARN] No se pudo adquirir el lock de cases.json — sync omitido")

    print(f"  Sincronizado: {len(existing)} casos totales ({added} nuevos).")

# ── HTTP Server ─────────────────────────────────────────────────────────────

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FOLDER, **kwargs)

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == '/cases':
            if os.path.exists(CASES_FILE):
                with open(CASES_FILE, encoding='utf-8') as f:
                    data = f.read()
            else:
                data = '[]'
            self._json(200, data.encode('utf-8'))
        elif self.path == '/notifications':
            if os.path.exists(NOTIF_FILE):
                with open(NOTIF_FILE, encoding='utf-8') as f:
                    data = f.read()
            else:
                data = '[]'
            self._json(200, data.encode('utf-8'))
        elif self.path == '/pendientes':
            if os.path.exists(PENDIENTES_FILE):
                with open(PENDIENTES_FILE, encoding='utf-8') as f:
                    data = f.read()
            else:
                data = '[]'
            self._json(200, data.encode('utf-8'))
        elif self.path == '/version':
            def file_sig(fp):
                try:
                    s = os.stat(fp)
                    return f"{s.st_mtime:.0f}-{s.st_size}"
                except FileNotFoundError:
                    return "0"
            data = {
                "cases": file_sig(CASES_FILE),
                "notif": file_sig(NOTIF_FILE),
            }
            self._json(200, json.dumps(data).encode())
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/cases':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length).decode('utf-8')
            new_cases = json.loads(body)
            # Sync changes back to Google Sheet (vencimiento + disposición)
            if os.path.exists(CASES_FILE):
                try:
                    with open(CASES_FILE, encoding='utf-8') as f:
                        old_cases = {c['caseNumber']: c for c in json.load(f)}
                    import threading
                    def sync():
                        for c in new_cases:
                            old = old_cases.get(c['caseNumber'], {})
                            if (c.get('nextDeadline') != old.get('nextDeadline') or
                                c.get('disposicion')  != old.get('disposicion')):
                                write_case_to_sheet(c)
                    threading.Thread(target=sync, daemon=True).start()
                except Exception:
                    pass
            if _acquire_lock(CASES_LOCK):
                try:
                    _atomic_write(CASES_FILE, new_cases)
                finally:
                    _release_lock(CASES_LOCK)
            self._json(200, b'{"ok":true}')
        elif self.path == '/pjn-update':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length).decode('utf-8')
            data   = json.loads(body)
            updated = False
            if os.path.exists(CASES_FILE):
                with open(CASES_FILE, encoding='utf-8') as f:
                    cases = json.load(f)
                nro = data.get('caseNumber', '').strip()
                for c in cases:
                    if c.get('caseNumber', '').strip() == nro and c.get('category') == 'constitutional':
                        if data.get('tribunal'):        c['tribunal']        = data['tribunal']
                        if data.get('proceduralStage'): c['proceduralStage'] = data['proceduralStage']
                        if data.get('lastAction'):      c['lastAction']      = data['lastAction']
                        if data.get('nextDeadline'):    c['nextDeadline']    = data['nextDeadline']
                        if data.get('caratula'):        c['caseTitle']       = data['caratula']
                        c['lastUpdated'] = datetime.now().isoformat()
                        updated = True
                        break
                if updated:
                    if _acquire_lock(CASES_LOCK):
                        try:
                            _atomic_write(CASES_FILE, cases)
                        finally:
                            _release_lock(CASES_LOCK)
            self._json(200, json.dumps({'ok': updated, 'updated': updated}).encode())
        elif self.path == '/notifications':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length).decode('utf-8')
            notif_data = json.loads(body)
            if _acquire_lock(NOTIF_LOCK):
                try:
                    _atomic_write(NOTIF_FILE, notif_data)
                finally:
                    _release_lock(NOTIF_LOCK)
            self._json(200, b'{"ok":true}')
        elif self.path == '/pendientes':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length).decode('utf-8')
            pendientes_data = json.loads(body)
            if _acquire_lock(PENDIENTES_LOCK):
                try:
                    _atomic_write(PENDIENTES_FILE, pendientes_data)
                finally:
                    _release_lock(PENDIENTES_LOCK)
            self._json(200, b'{"ok":true}')
        elif self.path == '/open-pjn':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length).decode('utf-8')
            data   = json.loads(body)
            case_number = data.get('caseNumber', '').strip()
            if case_number:
                script = os.path.join(FOLDER, 'open_pjn.py')
                exe = sys.executable.replace('pythonw', 'python')
                subprocess.Popen(
                    [exe, script, case_number],
                    cwd=FOLDER,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            self._json(200, b'{"ok":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _json(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

# ── Startup ─────────────────────────────────────────────────────────────────

print("=" * 50)
print("  NegroLex — Iniciando servidor...")
print("=" * 50)

sync_from_sheet()

print()
print(f"  Abriendo en: http://localhost:{PORT}/lawcase.html")
print("  No cierres esta ventana mientras usas la app.")

def _auto_sync_loop():
    """Corre check_cases.py en background cada 6 horas para detectar casos nuevos."""
    script = os.path.join(FOLDER, 'check_cases.py')
    time.sleep(21600)  # primera ejecución recién a las 6 horas
    while True:
        try:
            subprocess.Popen(
                [sys.executable, script, '--auto'],
                cwd=FOLDER,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"  [Auto-sync] Error: {e}", flush=True)
        time.sleep(21600)  # cada 6 horas

# AUTO-SYNC DESHABILITADO — reactivar el domingo 2026-03-23 (ban IP migraciones.gob.ar)
# threading.Thread(target=_auto_sync_loop, daemon=True).start()
print("  Auto-sync DESHABILITADO (ban IP activo).")
print("=" * 50)

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.serve_forever()
