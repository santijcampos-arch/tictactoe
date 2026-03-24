"""
NegroLex - Analisis de expedientes terminados para knowledge base de juzgados.

Lee terminados_raw.json + telegram_messages.json, llama a Groq por caso y por
juzgado/secretaría, y escribe conocimiento_juzgados.json.

Uso: python analizar_terminados.py
Salida: conocimiento_juzgados.json
"""

import os
import re
import json
import time
import shutil
import threading
from datetime import datetime

from groq import Groq as _Groq

FOLDER          = os.path.dirname(os.path.abspath(__file__))
RAW_FILE        = os.path.join(FOLDER, 'terminados_raw.json')
TELEGRAM_FILE   = os.path.join(FOLDER, 'telegram_messages.json')
CONOCIMIENTO_FILE = os.path.join(FOLDER, 'conocimiento_juzgados.json')
MEMORY_DIR      = r'C:\Users\Usuario\.claude\projects\C--Users-Usuario-Desktop\memory'
MEMORY_FILE     = os.path.join(MEMORY_DIR, 'conocimiento_juzgados.json')
MEMORY_MD       = os.path.join(MEMORY_DIR, 'MEMORY.md')

# Groq rate limiting (misma lógica que check_citizenship.py)
_GROQ_SEM  = threading.Semaphore(1)
_GROQ_LOCK = threading.Lock()
_GROQ_LAST = 0.0

_GROQ_KEY_FILE = os.path.join(FOLDER, 'groq_key.txt')
try:
    with open(_GROQ_KEY_FILE, encoding='utf-8') as _f:
        _GROQ_KEY = _f.read().strip()
    _GROQ_CLIENT = _Groq(api_key=_GROQ_KEY)
    _GROQ_AVAILABLE = True
    print('  [Groq] API lista.')
except Exception as _e:
    _GROQ_CLIENT = None
    _GROQ_AVAILABLE = False
    print(f'  [Groq] No disponible: {_e}')


# -- Carga de datos --

def cargar_terminados():
    """
    Carga terminados_raw.json.
    Retorna dict {grupo_key: [caso_dict, ...]}.
    Solo incluye casos con error: null.
    """
    if not os.path.exists(RAW_FILE):
        print(f'  [ERROR] No existe {RAW_FILE} - ejecutar explore_terminados.py primero')
        return {}
    with open(RAW_FILE, encoding='utf-8') as f:
        data = json.load(f)
    # Filtrar casos con error
    filtrado = {}
    for grupo, casos in data.items():
        ok = [c for c in casos if c.get('error') is None]
        if ok:
            filtrado[grupo] = ok
    return filtrado


def cargar_telegram():
    """
    Carga telegram_messages.json.
    Retorna dict {group_name: [msg_dict, ...]} o {} si no existe.
    La carga puede ser lenta (archivo grande).
    """
    if not os.path.exists(TELEGRAM_FILE):
        print(f'  [WARN] No existe {TELEGRAM_FILE} — se usarán solo actuaciones PJN')
        return {}
    print('  Cargando telegram_messages.json (puede tardar)...')
    with open(TELEGRAM_FILE, encoding='utf-8') as f:
        return json.load(f)


# -- Busqueda de contexto --

def buscar_mensajes_telegram(partido_apellido, telegram_data):
    """
    Busca el primer grupo de Telegram cuyo nombre contenga partido_apellido
    (case-insensitive). Retorna los últimos 20 mensajes como lista de strings.
    Retorna [] si no hay match o partido_apellido es None.
    """
    if not partido_apellido or not telegram_data:
        return []

    apellido_lower = partido_apellido.lower()
    for nombre_grupo, mensajes in telegram_data.items():
        if apellido_lower in nombre_grupo.lower():
            # Tomar hasta 20 mensajes más recientes
            ultimos = mensajes[-20:] if len(mensajes) > 20 else mensajes
            return [
                f"[{m.get('fecha', '')}] {m.get('de', '')}: {m.get('texto', '')[:300]}"
                for m in ultimos
            ]
    return []


# -- Llamadas a Groq --

def llamar_groq(prompt, max_tokens=800):
    """
    Llama a Groq con rate limiting (1 llamado a la vez, 5s mínimo entre llamados).
    Retorna el texto de la respuesta o None si falla.
    """
    global _GROQ_LAST
    if not _GROQ_AVAILABLE:
        return None

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
                max_tokens=max_tokens,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f'  [Groq] Error: {e}')
            return None


def extraer_json_respuesta(texto):
    """
    Extrae el primer objeto JSON de un texto de respuesta del LLM.
    Retorna dict o None.
    """
    if not texto:
        return None
    m = re.search(r'\{.*\}', texto, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# -- Analisis de casos --

def analizar_caso(juz, sec, case_number, actuaciones, mensajes_telegram):
    """
    Llama a Groq para analizar un expediente terminado.
    Retorna dict con etapas_detectadas, actuaciones_inusuales, pedidos_al_cliente, observaciones.
    Retorna None si Groq no está disponible o la respuesta es inválida.
    """
    actuaciones_text = '\n'.join(
        f"[{a.get('fecha', '')}] {a.get('tipo', '')} — {a.get('descripcion', '')}"
        for a in actuaciones
        if a.get('descripcion') or a.get('tipo')
    ) or '(sin actuaciones)'

    telegram_text = '\n'.join(mensajes_telegram) if mensajes_telegram else '(sin mensajes)'

    prompt = f"""Analizá el siguiente expediente de ciudadanía argentina en el fuero CCF.

Juzgado: {juz}, Secretaría: {sec}
Expediente: {case_number}

ACTUACIONES (cronológico, más antigua primero):
{actuaciones_text}

MENSAJES TELEGRAM DEL CLIENTE (si hay):
{telegram_text}

Extraé en JSON:
{{
  "etapas_detectadas": ["pfa_interpol", "renaper", ...],
  "actuaciones_inusuales": ["OFICIO SIDE", ...],
  "pedidos_al_cliente": ["antecedentes SIDE", ...],
  "observaciones": "texto libre"
}}
Solo incluí lo que podás determinar con certeza. Respondé ÚNICAMENTE con JSON válido."""

    print(f'    [Groq] Analizando {case_number}...')
    respuesta = llamar_groq(prompt, max_tokens=600)
    resultado = extraer_json_respuesta(respuesta)
    if resultado:
        print(f'    [Groq] OK — etapas: {resultado.get("etapas_detectadas", [])}')
    else:
        print(f'    [Groq] Sin resultado válido para {case_number}')
    return resultado


def filtrar_patrones_50pct(resultados_casos):
    """
    Retorna las etapas y actuaciones inusuales presentes en >50% de los casos.
    Usado como input para el prompt de agregación.
    """
    n = len(resultados_casos)
    if n == 0:
        return {}

    conteo_etapas = {}
    conteo_inusuales = {}

    for r in resultados_casos:
        if not r:
            continue
        for etapa in r.get('etapas_detectadas', []):
            conteo_etapas[etapa] = conteo_etapas.get(etapa, 0) + 1
        for act in r.get('actuaciones_inusuales', []):
            conteo_inusuales[act] = conteo_inusuales.get(act, 0) + 1

    umbral = n / 2
    return {
        'etapas_frecuentes': [e for e, c in conteo_etapas.items() if c > umbral],
        'actuaciones_inusuales_frecuentes': [a for a, c in conteo_inusuales.items() if c > umbral],
        'n_casos': n,
    }


def agregar_juzgado(juz, sec, resultados_casos):
    """
    Llama a Groq con los resultados de todos los casos del juzgado/secretaría
    para generar el perfil final.
    Retorna el dict del perfil o un perfil vacío si falla.
    """
    n = len(resultados_casos)
    resultados_json = json.dumps(resultados_casos, ensure_ascii=False, indent=2)

    prompt = f"""Analizaste {n} expedientes del Juzgado {juz} Secretaría {sec}.

Resultados por caso:
{resultados_json}

Generá el perfil del juzgado en JSON:
{{
  "particularidades": ["lista de cosas que este juzgado hace distinto a los demás"],
  "secuencia_tipica": ["orden típico de etapas"],
  "alertas_proactivas": [
    {{
      "condicion": {{"etapa_presente": "renaper", "etapa_ausente": "side"}},
      "mensaje": "descripción de la alerta para el estudio"
    }}
  ],
  "alertas_reactivas": [
    {{
      "keywords": ["OFICIO SIDE"],
      "mensaje": "descripción de la acción a tomar"
    }}
  ]
}}
Solo incluí alertas que estén respaldadas por los datos. Respondé ÚNICAMENTE con JSON válido."""

    print(f'  [Groq] Generando perfil J{juz}-S{sec} ({n} casos)...')
    respuesta = llamar_groq(prompt, max_tokens=1000)
    perfil = extraer_json_respuesta(respuesta)
    if not perfil:
        print(f'  [Groq] Sin perfil válido para J{juz}-S{sec} — usando perfil vacío')
        perfil = {
            'particularidades': [],
            'secuencia_tipica': [],
            'alertas_proactivas': [],
            'alertas_reactivas': [],
        }
    return perfil


# -- Procesamiento de grupos --

def procesar_grupo(grupo_key, casos, telegram_data):
    """
    Procesa un grupo (ej: "J08-S15"):
    1. Analiza cada caso con Groq
    2. Filtra patrones al 50%
    3. Genera perfil del juzgado con segunda llamada a Groq
    Retorna el dict del perfil listo para conocimiento_juzgados.json.
    """
    m = re.match(r'J(\d+)-S(\d+)', grupo_key)
    if not m:
        print(f'  [WARN] Formato de grupo inesperado: {grupo_key}')
        return None
    juz = int(m.group(1))
    sec = int(m.group(2))

    print(f'\n[{grupo_key}] Analizando {len(casos)} casos...')
    resultados = []
    for caso in casos:
        mensajes = buscar_mensajes_telegram(
            caso.get('partido_apellido'),
            telegram_data
        )
        resultado = analizar_caso(
            juz, sec,
            caso['case_number'],
            caso.get('actuaciones', []),
            mensajes,
        )
        resultados.append(resultado)

    # Filtrar al 50% en Python (no delegar al LLM)
    patrones = filtrar_patrones_50pct([r for r in resultados if r])
    print(f'  [{grupo_key}] Patrones frecuentes: {patrones}')

    # Segunda llamada: generar perfil con los resultados válidos
    resultados_validos = [r for r in resultados if r]
    perfil = agregar_juzgado(juz, sec, resultados_validos)

    return {
        'nombre': f'Juzgado {juz} - Secretaría {sec}',
        'casos_analizados': len(casos),
        'casos_con_resultado': len(resultados_validos),
        'generado_en': datetime.now().isoformat(),
        'patrones_50pct': patrones,
        **perfil,
    }


# -- Escritura a memoria --

def copiar_a_memoria(conocimiento):
    """
    Copia conocimiento_juzgados.json a la carpeta de memoria de Claude.
    Actualiza MEMORY.md si la entrada aún no existe.
    """
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        shutil.copy2(CONOCIMIENTO_FILE, MEMORY_FILE)
        print(f'  [MEM] Copiado a {MEMORY_FILE}')

        # Agregar entrada en MEMORY.md si no existe
        entrada = '- [conocimiento_juzgados.json](./conocimiento_juzgados.json) — Knowledge base de patrones por juzgado/secretaría CCF (generado de expedientes terminados + Telegram).'
        if os.path.exists(MEMORY_MD):
            with open(MEMORY_MD, encoding='utf-8') as f:
                contenido = f.read()
            if 'conocimiento_juzgados.json' not in contenido:
                with open(MEMORY_MD, 'a', encoding='utf-8') as f:
                    f.write('\n' + entrada + '\n')
                print('  [MEM] MEMORY.md actualizado')
            else:
                print('  [MEM] MEMORY.md ya tiene la entrada')
        else:
            print(f'  [WARN] No se encontró MEMORY.md en {MEMORY_DIR}')
    except Exception as e:
        print(f'  [WARN] No se pudo copiar a memoria: {e}')


# -- Main --

def main():
    print('=' * 60)
    print('  NegroLex - Analisis de Terminados >> Knowledge Base')
    print('=' * 60)

    terminados = cargar_terminados()
    if not terminados:
        print('  Sin datos para analizar.')
        return

    telegram_data = cargar_telegram()

    conocimiento = {}
    for grupo_key, casos in terminados.items():
        perfil = procesar_grupo(grupo_key, casos, telegram_data)
        if perfil:
            conocimiento[grupo_key] = perfil

    if not conocimiento:
        print('  No se genero conocimiento.')
        return

    tmp = CONOCIMIENTO_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(conocimiento, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONOCIMIENTO_FILE)
    print(f'\n  Conocimiento guardado en {CONOCIMIENTO_FILE}')

    copiar_a_memoria(conocimiento)
    print('\n  Listo.')


if __name__ == '__main__':
    main()
