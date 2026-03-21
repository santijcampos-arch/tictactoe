"""
NegroLex — Gmail Checker
Busca emails de Migraciones y agrega las intimaciones a los casos.
"""

import os
import sys
import json
import base64
import re
from datetime import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

FOLDER        = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE    = os.path.join(FOLDER, 'gmail_credentials.json')
TOKEN_FILE    = os.path.join(FOLDER, 'gmail_token.json')
CASES_FILE    = os.path.join(FOLDER, 'cases.json')
SCOPES        = ['https://www.googleapis.com/auth/gmail.readonly']
AUTO          = False  # sobreescrito por --auto en __main__

# ── Auth ────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

# ── Email parsing ────────────────────────────────────────────────────────────

def get_body(msg):
    """Extract plain text body from a Gmail message."""
    payload = msg.get('payload', {})

    def extract(part):
        if part.get('mimeType') == 'text/plain':
            data = part.get('body', {}).get('data', '')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
        if part.get('mimeType') == 'text/html':
            data = part.get('body', {}).get('data', '')
            if data:
                html = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
                return re.sub(r'<[^>]+>', ' ', html)
        for sub in part.get('parts', []):
            result = extract(sub)
            if result:
                return result
        return ''

    return extract(payload).strip()

def find_expediente(text):
    """Extract expediente number — looks for N°: followed by digits."""
    m = re.search(r'N[°o][\s:]*(\d{6,})', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None

def find_intimacion_text(body):
    """Extract what documentation is requested."""
    m = re.search(r'siguiente documentaci[oó]n[:\s]+(.+?)(?:IMPORTANTE|$)', body, re.IGNORECASE | re.DOTALL)
    if m:
        return re.sub(r'\s+', ' ', m.group(1).strip())[:600]
    return ''

def find_plazo(body):
    """Extract the number of days given to comply (e.g. 'dentro de los 30 días')."""
    m = re.search(r'dentro de los\s+(\d+)\s+d[ií]as', body, re.IGNORECASE)
    return int(m.group(1)) if m else 30  # default 30

def parse_email_date(date_str):
    """Parse email Date header to YYYY-MM-DD."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).strftime('%Y-%m-%d')
    except Exception:
        return datetime.now().strftime('%Y-%m-%d')

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  NegroLex — Verificador de Gmail")
    print("=" * 60)
    print()

    # Load cases
    if not os.path.exists(CASES_FILE):
        print("No se encontró cases.json. Abrí la app primero.")
        if not AUTO: input("\nPresioná Enter para salir.")
        return
    with open(CASES_FILE, encoding='utf-8') as f:
        cases = json.load(f)

    # Connect to Gmail
    print("Conectando con Gmail...")
    try:
        service = get_gmail_service()
    except Exception as e:
        print(f"Error al conectar: {e}")
        if not AUTO: input("\nPresioná Enter para salir.")
        return

    # Search for emails from Migraciones
    print("Buscando emails de Migraciones...\n")
    query = 'from:noreply.citaweb@migraciones.gob.ar'
    try:
        result  = service.users().messages().list(userId='me', q=query, maxResults=50).execute()
        messages = result.get('messages', [])
    except Exception as e:
        print(f"Error al buscar emails: {e}")
        if not AUTO: input("\nPresioná Enter para salir.")
        return

    if not messages:
        print("No se encontraron emails de Migraciones.")
        if not AUTO: input("\nPresioná Enter para salir.")
        return

    print(f"Encontrados {len(messages)} emails. Procesando...\n")

    updated = 0
    not_matched = []

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId='me', id=msg_ref['id'], format='full'
        ).execute()

        # Get subject
        headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}
        subject = headers.get('Subject', '(sin asunto)')
        date    = headers.get('Date', '')
        body    = get_body(msg)

        # Only process intimaciones
        if 'ntimac' not in subject.lower():
            continue

        expte  = find_expediente(subject) or find_expediente(body)
        texto  = find_intimacion_text(body)
        plazo  = find_plazo(body)
        fecha  = parse_email_date(headers.get('Date', ''))

        # Calculate deadline
        from datetime import timedelta, date
        fecha_date  = datetime.strptime(fecha, '%Y-%m-%d').date()
        vencimiento = (fecha_date + timedelta(days=plazo)).isoformat()

        if expte:
            idx = next((i for i, c in enumerate(cases) if c.get('caseNumber', '') == expte), None)
            if idx is not None:
                intimacion = {
                    'fecha':       fecha,
                    'plazo':       plazo,
                    'vencimiento': vencimiento,
                    'texto':       texto,
                }
                existing = cases[idx].get('intimaciones', [])
                # Avoid duplicates by date
                if not any(i.get('fecha') == fecha for i in existing):
                    existing.append(intimacion)
                    cases[idx]['intimaciones'] = existing
                    cases[idx]['lastUpdated']  = datetime.now().isoformat()
                    print(f"  ✓ Expte {expte} — intimación del {fecha} agregada (vence {vencimiento})")
                    updated += 1
                else:
                    print(f"  — Expte {expte} — ya registrada, salteando")
            else:
                print(f"  ? Expte {expte} — no encontrado en la app")
                not_matched.append((expte, subject))
        else:
            print(f"  ? Sin número de expte — {subject[:60]}")
            not_matched.append(('—', subject))

    # Save (atomic write)
    tmp = CASES_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CASES_FILE)

    print()
    print("=" * 60)
    print(f"  Listo. {updated} casos actualizados.")
    if not_matched:
        print(f"\n  Emails sin caso coincidente ({len(not_matched)}):")
        for expte, subj in not_matched:
            print(f"    Expte {expte}: {subj[:55]}")
    print("=" * 60)

    # Send Windows notifications
    try:
        import notificar
        notificar.check_and_notify()
    except Exception:
        pass

    if not AUTO: input("\nPresioná Enter para salir.")

if __name__ == '__main__':
    AUTO = '--auto' in sys.argv  # noqa: F811
    main()
