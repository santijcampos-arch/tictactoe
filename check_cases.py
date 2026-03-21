"""
LexCase — Migraciones Auto-Checker
Reads your CSV, queries each case on migraciones.gob.ar,
and saves a results file you can load into the app.
"""

import csv
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
import os

BASE_URL = 'https://www.migraciones.gob.ar/accesible/consultaTramitePrecaria/'
API_URL  = BASE_URL + 'api/ajax_consulta_tramite.php'

# ── Helpers ────────────────────────────────────────────────────────────────

def parse_sheet_date(raw):
    """DD-MM-YYYY or DD/MM/YYYY  →  DD/MM/YYYY (for the API)"""
    if not raw:
        return None
    s = raw.strip().replace(' ', '').replace('-', '/')
    parts = s.split('/')
    if len(parts) != 3:
        return None
    d, m, y = parts
    if len(y) == 2:
        y = '20' + y
    return f"{d.zfill(2)}/{m.zfill(2)}/{y}"

def parse_api_date(raw):
    """API date  →  YYYY-MM-DD (for the app)"""
    if not raw:
        return ''
    s = raw.strip()
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return ''

def map_status(estado):
    if not estado:
        return 'pending'
    e = estado.lower()
    if any(w in e for w in ('activ', 'vigent')):
        return 'active'
    if any(w in e for w in ('vencid', 'cerrad', 'archiv', 'clausur')):
        return 'closed'
    return 'pending'

def query_case(nro_expediente, fecha_nac, cookie):
    """POST to migraciones API and return parsed JSON or error dict."""
    payload_dict = {'nro_expediente': str(nro_expediente), 'fecha_nac': fecha_nac}
    body = urllib.parse.urlencode({'data': json.dumps(payload_dict)}).encode('utf-8')

    req = urllib.request.Request(API_URL, data=body, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8')
    req.add_header('X-Requested-With', 'XMLHttpRequest')
    req.add_header('Referer', BASE_URL)
    req.add_header('User-Agent', 'Mozilla/5.0')
    if cookie:
        req.add_header('Cookie', cookie)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}'}
    except Exception as e:
        return {'error': str(e)}

def get_session_cookie():
    """Visit the main page to grab a session cookie."""
    req = urllib.request.Request(BASE_URL)
    req.add_header('User-Agent', 'Mozilla/5.0')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.headers.get('Set-Cookie', '')
    except Exception:
        return ''

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  NegroLex — Migraciones Auto-Checker")
    print("=" * 60)
    print()
    print("Arrastrá tu archivo CSV a esta ventana y presioná Enter.")
    print("(Exportalo desde Google Sheets: Archivo → Descargar → CSV)")
    print()
    csv_path = input("Archivo CSV: ").strip().strip('"').strip("'")

    if not os.path.exists(csv_path):
        print(f"\nArchivo no encontrado: {csv_path}")
        input("Presioná Enter para salir.")
        return

    # Read CSV
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        rows = [{k.strip().lower(): v.strip() for k, v in row.items()} for row in reader]

    print(f"\nEncontradas {len(rows)} filas. Conectando con migraciones.gob.ar...\n")
    cookie = get_session_cookie()

    results = []
    queried = 0

    for row in rows:
        nombre = row.get('nombre', '').strip()
        nro    = row.get('numero de expte', '').strip()

        # Try both spellings of the birth date column
        fecha_raw = (row.get('fecha de  nacimiento') or
                     row.get('fecha de nacimiento') or '').strip()

        if not nombre:
            continue  # blank row

        if not nro:
            print(f"  — {nombre}: sin número de expediente, salteando")
            results.append({'caseNumber': '', 'clientName': nombre,
                            'queried': False, 'message': 'Sin número de expediente'})
            continue

        fecha = parse_sheet_date(fecha_raw)
        if not fecha:
            print(f"  — {nombre} (#{nro}): sin fecha de nacimiento, salteando")
            results.append({'caseNumber': nro, 'clientName': nombre,
                            'queried': False, 'message': 'Sin fecha de nacimiento en el sheet'})
            continue

        print(f"  Consultando {nombre} (#{nro}, nacido {fecha})... ", end='', flush=True)
        data = query_case(nro, fecha, cookie)

        if isinstance(data.get('error'), str) and data['error'] != '-1':
            msg = data.get('mensaje') or data['error']
            print(f"ERROR — {msg}")
            results.append({'caseNumber': nro, 'clientName': nombre,
                            'queried': False, 'message': msg})
        else:
            dp = data.get('datos_persona', {})
            estado      = dp.get('estado', '')
            vencimiento = parse_api_date(dp.get('fecha_vencimiento_precaria', ''))
            renovacion  = parse_api_date(dp.get('fecha_renovacion_precaria', ''))
            disposicion = dp.get('nro_disposicion', '')
            delegacion  = dp.get('delegacion', '')
            tipo        = dp.get('tipo_tramite', '')

            note_parts = []
            if estado:      note_parts.append(f"Estado: {estado}")
            if disposicion: note_parts.append(f"Disposición: {disposicion}")
            if delegacion:  note_parts.append(f"Delegación: {delegacion}")
            if tipo:        note_parts.append(f"Tipo: {tipo}")
            if renovacion:  note_parts.append(f"Renovación: {renovacion}")

            print(f"OK — {estado or 'sin estado'}")
            queried += 1

            results.append({
                'caseNumber':      nro,
                'clientName':      nombre,
                'queried':         True,
                'status':          map_status(estado),
                'nextDeadline':    vencimiento,
                'proceduralStage': tipo or row.get('intimaciones', '') or 'Under Review',
                'notes':           ' | '.join(note_parts),
                'lastChecked':     datetime.now().strftime('%Y-%m-%d'),
                'rawEstado':       estado,
            })

        time.sleep(8)  # respetar rate limit — 0.6s causó ban de IP en migraciones.gob.ar

    # Save results
    out_path = os.path.join(os.path.dirname(csv_path), 'migraciones_results.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 60)
    print(f"  Listo. {queried} de {len(results)} casos consultados correctamente.")
    print(f"  Resultados guardados en:")
    print(f"  {out_path}")
    print()
    print("  Abrí lawcase.html y hacé click en 'Cargar resultados'")
    print("  para actualizar todos tus casos automáticamente.")
    print("=" * 60)
    input("\nPresioná Enter para salir.")

if __name__ == '__main__':
    main()
