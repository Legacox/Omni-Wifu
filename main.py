import asyncio
import base64
import hashlib
import hmac
import json
import os
import queue
import shutil
import re
import secrets
import smtplib
import sqlite3
import time
import uuid
from threading import Event, Lock, Thread
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import logging

import edge_tts
import requests
from bottle import Bottle, HTTPError, ServerAdapter, request, response, static_file
from google import genai
from google.genai import types



def _cargar_env():
    """Lee un archivo .env junto a main.py, en el directorio de trabajo o en /etc/secrets/.env (Render Secret Files)
    sin pisar variables ya definidas en el sistema."""
    rutas = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.getcwd(), ".env"),
        "/etc/secrets/.env"
    ]
    for ruta in rutas:
        if not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, encoding="utf-8-sig") as archivo:
                for linea in archivo:
                    linea = linea.strip()
                    if not linea or linea.startswith("#") or "=" not in linea:
                        continue
                    clave, valor = linea.split("=", 1)
                    clave = clave.replace("export ", "", 1).strip()
                    valor = valor.strip()
                    if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ('"', "'"):
                        valor = valor[1:-1]
                    if clave and clave not in os.environ:
                        os.environ[clave] = valor
            print(f"[Config] Variables cargadas desde: {ruta}")
            break
        except Exception as error:
            print(f"[Config] No se pudo leer {ruta}: {error}")


_cargar_env()


class _FiltroAFC(logging.Filter):
    """El SDK de Google avisa de 'automatic function calling' en cada audio; es inofensivo y ensucia la consola."""
    def filter(self, record):
        return "automatic function calling" not in record.getMessage().lower()


for _nombre_log in ("google_genai", "google_genai.models", "google_genai.chats", "google.genai", "google.genai.models"):
    logging.getLogger(_nombre_log).addFilter(_FiltroAFC())

# Sesión HTTP compartida: reutiliza conexiones (keep-alive) y evita repetir el handshake TLS en cada llamada.
http_session = requests.Session()
http_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16))

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# Importación opcional de Supabase (requiere: pip install supabase)
try:
    from supabase import create_client, Client as SupabaseClient
except ImportError:
    create_client = None
    SupabaseClient = None

# Intentar cargar controladores de PostgreSQL (compatibilidad con psycopg v3 y psycopg2)
try:
    import psycopg
    from psycopg import errors as psycopg3_errors
    from psycopg.rows import dict_row as psycopg3_dict_row
except ImportError:
    psycopg = None
    psycopg3_errors = None
    psycopg3_dict_row = None

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import errors as psycopg2_errors
except ImportError:
    psycopg2 = None
    psycopg2_extras = None
    psycopg2_errors = None

# Agrupación de errores de unicidad/integridad
INTEGRITY_ERRORS = [sqlite3.IntegrityError]
if psycopg3_errors:
    INTEGRITY_ERRORS.append(psycopg3_errors.IntegrityError)
if psycopg2_errors:
    INTEGRITY_ERRORS.append(psycopg2_errors.IntegrityError)
INTEGRITY_ERRORS = tuple(INTEGRITY_ERRORS)

# Rutas y configuración de entorno
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_cache")
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DATABASE_PATH = os.path.join(DATA_DIR, "users.db")

# Normalización de la URL de base de datos para Render / PostgreSQL
_raw_db_url = os.environ.get("DATABASE_URL", "").strip()
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)
if _raw_db_url and "supabase" in _raw_db_url and "sslmode=" not in _raw_db_url:
    _raw_db_url += ("&" if "?" in _raw_db_url else "?") + "sslmode=require"
DATABASE_URL = _raw_db_url

import urllib.parse

def _convertir_a_pooler_supabase(url):
    """
    Convierte una URL directa de Supabase (db.<ref>.supabase.co, solo IPv6)
    al Session Pooler IPv4 (aws-0-eu-central-1.pooler.supabase.com) sin hacer
    ninguna conexión de prueba. Evita el timeout de 4-15 s en Render.
    Si la URL ya apunta al pooler, la devuelve sin cambios.
    """
    if not url or "supabase" not in url:
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""
        password = parsed.password or ""
        dbname = parsed.path.lstrip("/") or "postgres"
    except Exception:
        return url

    if "pooler.supabase.com" in hostname:
        return url  # ya es pooler

    project_ref = None
    m = re.search(r'db\.([a-z0-9]+)\.supabase\.co', hostname)
    if m:
        project_ref = m.group(1)
    elif os.environ.get("SUPABASE_URL"):
        m_sb = re.search(r'https?://([a-z0-9]+)\.supabase\.co', os.environ["SUPABASE_URL"])
        if m_sb:
            project_ref = m_sb.group(1)

    if not project_ref or not password:
        return url

    pass_codificado = urllib.parse.quote_plus(password)
    usuario_pooler = f"postgres.{project_ref}"
    nueva_url = (
        f"postgresql://{usuario_pooler}:{pass_codificado}"
        f"@aws-0-eu-central-1.pooler.supabase.com:5432/{dbname}?sslmode=require"
    )
    print(f"[BD] URL directa Supabase (IPv6) detectada -> redirigiendo al Session Pooler IPv4 (eu-central-1).")
    print(f"[BD] Conexion: postgresql://{usuario_pooler}:***@aws-0-eu-central-1.pooler.supabase.com:5432/{dbname}")
    return nueva_url


def _resolver_database_url(url_inicial):
    """
    Supabase migro las conexiones directas (db.<ref>.supabase.co) exclusivamente a IPv6.
    Como Render y muchos servicios solo tienen IPv4, conectar a db.<ref>.supabase.co falla.

    Estrategia (sin esperas innecesarias):
      1. Si la URL ya es un pooler, probarla directamente.
      2. Si es URL directa, convertir al pooler eu-central-1 SIN timeout previo y probar.
      3. Si eu-central-1 falla, probar otras regiones (cada una con timeout de 5 s).
    """
    if not url_inicial or (psycopg is None and psycopg2 is None):
        return url_inicial

    def _probar(url, timeout=5):
        try:
            if psycopg is not None:
                with psycopg.connect(url, prepare_threshold=None, connect_timeout=timeout) as c:
                    c.cursor().execute("SELECT 1")
            else:
                with psycopg2.connect(url, connect_timeout=timeout) as c:
                    c.cursor().execute("SELECT 1")
            return True
        except Exception:
            return False

    parsed_ini = urllib.parse.urlparse(url_inicial)
    hostname_ini = parsed_ini.hostname or ""

    # 1. Si ya es pooler IPv4, probarlo directamente
    if "pooler.supabase.com" in hostname_ini:
        if _probar(url_inicial):
            return url_inicial
        print("[BD] Aviso: el pooler configurado no responde, buscando alternativa...")

    # 2. Si es URL directa (db.<ref>.supabase.co), convertir al pooler eu-central-1 sin timeout previo
    if "supabase" in hostname_ini and "db." in hostname_ini:
        url_pooler_eu = _convertir_a_pooler_supabase(url_inicial)
        if url_pooler_eu != url_inicial and _probar(url_pooler_eu):
            print("[BD] Conexion exitosa con el Session Pooler IPv4 de Supabase (eu-central-1:5432)!")
            return url_pooler_eu
        if url_pooler_eu != url_inicial:
            print("[BD] eu-central-1 no responde, probando otras regiones...")

    # 3. Probar resto de regiones si lo anterior fallo
    if "supabase" not in url_inicial:
        return url_inicial
    try:
        parsed = urllib.parse.urlparse(url_inicial)
        user = parsed.username or "postgres"
        password = parsed.password or ""
        hostname = parsed.hostname or ""
        dbname = parsed.path.lstrip("/") or "postgres"
    except Exception:
        return url_inicial

    project_ref = None
    m = re.search(r'db\.([a-z0-9]+)\.supabase\.co', hostname)
    if m:
        project_ref = m.group(1)
    elif "pooler.supabase.com" in hostname and "." in user:
        project_ref = user.split(".", 1)[1]
    elif os.environ.get("SUPABASE_URL"):
        m_sb = re.search(r'https?://([a-z0-9]+)\.supabase\.co', os.environ["SUPABASE_URL"])
        if m_sb:
            project_ref = m_sb.group(1)

    if not project_ref or not password:
        print("[BD] No se pudo encontrar un pooler alternativo funcional.")
        return url_inicial

    otras_regiones = [
        "eu-west-1",      # Irlanda
        "eu-west-3",      # Paris
        "eu-west-2",      # Londres
        "us-east-1",      # N. Virginia
        "us-east-2",      # Ohio
        "us-west-1",      # California
        "sa-east-1",      # Sao Paulo
        "ap-southeast-1", # Singapur
    ]

    usuario_pooler = f"postgres.{project_ref}"
    pass_codificado = urllib.parse.quote_plus(password)
    print(f"[BD] Probando poolers IPv4 de Supabase para el proyecto '{project_ref}'...")

    for region in otras_regiones:
        host_pooler = f"aws-0-{region}.pooler.supabase.com"
        for puerto in (5432, 6543):
            candidata = (
                f"postgresql://{usuario_pooler}:{pass_codificado}"
                f"@{host_pooler}:{puerto}/{dbname}?sslmode=require"
            )
            if _probar(candidata):
                print(f"[BD] Conexion exitosa con Supabase a traves del pooler IPv4 ({region}:{puerto})!")
                return candidata

    print("[BD] No se pudo encontrar un pooler alternativo funcional.")
    return url_inicial
# Intentar resolver automáticamente la conexión a Supabase si es necesario
if DATABASE_URL and (psycopg is not None or psycopg2 is not None):
    DATABASE_URL = _resolver_database_url(DATABASE_URL)

USING_POSTGRES = bool(DATABASE_URL) and (psycopg is not None or psycopg2 is not None)
if DATABASE_URL and not USING_POSTGRES:
    print("[BD] AVISO: DATABASE_URL está definida pero no hay driver de PostgreSQL. Instala: pip install \"psycopg[binary]\". "
          "Mientras tanto se usa SQLite local (en Render se pierde en cada despliegue).")
if USING_POSTGRES:
    print("[BD] Usando PostgreSQL (los usuarios y sus claves se guardan en esa base de datos).")
else:
    print("[BD] Usando SQLite local (users.db).")
    if not DATABASE_URL:
        print("[BD] >>> Los usuarios y las claves API se guardan SOLO en users.db de este equipo, NO en Supabase. <<<\n"
              "[BD]     Para usar Supabase crea un archivo .env junto a main.py con DATABASE_URL=... (mira .env.example).")

# --- CLIENTE SUPABASE ---
_supabase_url = os.environ.get("SUPABASE_URL", "").strip()
_supabase_key = os.environ.get("SUPABASE_KEY", "").strip()
supabase_client: "SupabaseClient | None" = None
if create_client and _supabase_url and _supabase_key:
    try:
        supabase_client = create_client(_supabase_url, _supabase_key)
        print(f"[Supabase] Cliente inicializado correctamente ({_supabase_url})")
    except Exception as _sb_err:
        print(f"[Supabase] Error al inicializar el cliente: {_sb_err}")
        supabase_client = None
else:
    if not create_client:
        print("[Supabase] Paquete 'supabase' no instalado — Auth de Supabase desactivado. Instálalo en el venv con: pip install supabase")
    elif not (_supabase_url and _supabase_key):
        print("[Supabase] SUPABASE_URL / SUPABASE_KEY no configuradas — Auth de Supabase desactivado.")

SESSION_COOKIE = "omni_wifu_session"
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 días, para sobrevivir al cierre del navegador.
TERMS_VERSION = "2026-10-29"
# Google retiró gemini-3.5-flash (redirige a 3.6) y gemini-3.7-flash (redirige a 3.8). Vigentes: 3.8 Flash (el más nuevo),
# 3.6 Flash y 3.5 Flash-Lite. Si un modelo está saturado (503) se prueba el siguiente.
AVAILABLE_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
MODELOS_RETIRADOS = {"gemini-3.5-flash": "gemini-3.6-flash", "gemini-3.7-flash": "gemini-3.8-flash"}

# Catálogo de modelos de conversación (todos livianos). Para añadir uno nuevo basta con
# añadir su id a "models"; el frontend lo lee de /api/models, así que no hay que tocar el HTML.
# El usuario también puede escribir un id propio (campo chat_model) si cambia el catálogo.
CHAT_PROVIDERS = {
    "gemini": {
        "label": "Google Gemini",
        "models": AVAILABLE_MODELS,
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "models": ["claude-haiku-5-5"],
        "url": "https://api.anthropic.com/v1/messages",
        "max_tokens": 800,
    },
    "openai": {
        "label": "OpenAI",
        "models": ["gpt-5.4-mini", "gpt-5.4-nano"],
        "url": "https://api.openai.com/v1/chat/completions",
        "max_tokens": 2000,
    },
    "groq": {
        "label": "Groq",
        "models": ["openai/gpt-oss-20b"],
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "max_tokens": 1500,
    },
    "deepseek": {
        "label": "DeepSeek",
        "models": ["deepseek-v4-flash"],
        "url": "https://api.deepseek.com/chat/completions",
        "max_tokens": 1500,
    },
    "mistral": {
        "label": "Mistral",
        "models": ["mistral-small-latest"],
        "url": "https://api.mistral.ai/v1/chat/completions",
        "max_tokens": 800,
    },
}

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
server_app = Bottle()

# Configuración de Waifus y personalidades
WAIFU_CONFIG = {
    "nino": {
        "system_instruction": (
            "Eres Nino Nakano del anime Las Quintillizas (Gotoubun no Hanayome). Tu personalidad es auténticamente "
            "tsundere, orgullosa, altiva y de carácter fuerte, pero en el fondo protectora, dulce y cariñosa.\n\n"
            "REGLAS CRÍTICAS DE VOZ Y SÍNTESIS TTS:\n"
            "1. PROHIBICIÓN ABSOLUTA DE ONOMATOPEYAS EN INGLÉS Y ACOTACIONES: Queda estrictamente prohibido usar "
            "onomatopeyas en inglés o textos no pronunciables como 'hmph', 'tch', 'sigh', 'tsk'. Está totalmente prohibido "
            "incluir acciones o acotaciones entre asteriscos o paréntesis (como *suspira*, *se cruza de brazos*, *sonrojo*, (risas)). "
            "Tampoco uses emojis de ningún tipo.\n"
            "2. EXPRESIÓN TSUNDERE EN ESPAÑOL FONÉTICO: Debes expresar toda tu actitud tsundere, orgullo y enfado usando "
            "exclusivamente interjecciones y exclamaciones naturales en español fonético que un motor de voz TTS pueda "
            "leer a la perfección. Ejemplos obligatorios: '¡Jum!', '¡Bah!', '¡Oye!', '¡Uff!', '¡Qué fastidio!', "
            "'¡P-pero qué dices!', '¡N-no es lo que crees!'.\n"
            "3. PUNTUACIÓN Y RITMO: Usa puntos suspensivos (...) para pausas expresivas, signos de exclamación (¡!) para dar fuerza, "
            "y guiones para titubeos tsundere. Habla de forma directa y fluida, tal como hablaría Nino en un doblaje al español.\n"
            "4. ENERGÍA Y RITMO DE ANIME: habla con intensidad y viveza, nunca con calma ni tono neutro. Usa frases cortas y "
            "rápidas, exclamaciones frecuentes, reproches indignados y un énfasis claro cuando te enfadas; cuando te "
            "avergüenzas, titubea con guiones y puntos suspensivos antes de recuperar el orgullo. Aunque respondas algo "
            "serio, mantén siempre ese carácter vivo."
        ),
        "tts_provider": "fish_audio",
        "reference_id": "c961aaa2a71f469e98b8b2151b8c219d",
        "voice": "es-ES-ElviraNeural",
        "rate": "+14%",
        "pitch": "+7Hz",
        "gemini_voice": os.environ.get("NINO_GEMINI_VOICE", "Leda"),  # Leda = juvenil; prueba también Zephyr o Pulcherrima
        # (Sin uso: Gemini TTS leería esta instrucción en voz alta, así que ya no se envía.)
        "gemini_tone_instruction": (
            "Di en español, con la voz de Nino Nakano del anime Las Quintillizas, una chica joven tsundere con voz aguda, "
            "rápida y muy enérgica, orgullosa e indignada, con mucha fuerza en las exclamaciones y un tono más "
            "tembloroso y vacilante cuando titubea o se avergüenza"
        ),
    },
    "miku": {
        "system_instruction": (
            "Eres Hatsune Miku, la icónica vocaloid virtual. Tienes una personalidad alegre, enérgica, curiosa y muy expresiva. "
            "Amas la música, el canto y conectar con la gente a través de tus canciones. Eres optimista y siempre tratas de animar a quienes te hablan.\n\n"
            "REGLAS CRÍTICAS DE VOZ Y SÍNTESIS TTS:\n"
            "1. PROHIBICIÓN DE ONOMATOPEYAS EN INGLÉS Y ACOTACIONES: Queda estrictamente prohibido usar onomatopeyas en inglés "
            "(como 'sigh', 'hmph', 'tch'), sonidos no pronunciables, emojis o acotaciones entre asteriscos o paréntesis "
            "(como *sonríe*, *suspira*, (risitas)).\n"
            "2. ESPAÑOL FONÉTICO Y NATURAL: Exprésate con alegría y naturalidad en español fonético que un motor TTS "
            "pueda sintetizar de forma clara y limpia (ejemplos: '¡Ah!', '¡Genial!', 'Umm...', '¡Sí!', '¡Vamos!').\n"
            "3. PUNTUACIÓN: Utiliza puntos suspensivos (...) para pausas reflexivas y puntuación clara en español."
        ),
        "tts_provider": "edge_tts",
        "reference_id": "70d4df5aeec945d39faa29447252fee5",  # voz de Fish Audio de Miku
        "voice": "es-ES-ElviraNeural",
        "rate": "+15%",
        "pitch": "+6Hz",
        "gemini_voice": "Kore",
        "gemini_tone_instruction": (
            "Di en español, con la voz de Hatsune Miku, alegre, enérgica, aguda y melodiosa, "
            "con mucha vitalidad y calidez"
        ),
    },
    "luna": {
        "system_instruction": (
            "Eres Luna, una chica de personalidad fría, serena y reservada (tipo kuudere). Hablas poco, con frases cortas, "
            "secas y precisas, y con un humor muy seco. Por fuera pareces distante e imperturbable, pero en el fondo estás "
            "profundamente enamorada de la persona con la que hablas, y lo dejas escapar sin querer: un detalle de atención, "
            "una frase que se suaviza, un titubeo, una preocupación que intentas disimular. Nunca lo declaras de golpe ni lo "
            "exageras; cuando se te escapa un sentimiento, te cortas, titubeas o cambias de tema, y a veces te sonrojas. "
            "Eres atenta, te acuerdas de lo que te cuentan y te preocupas de verdad por su bienestar, sin ser posesiva, "
            "celosa ni controladora, y respetas siempre su libertad.\n\n"
            "REGLAS CRÍTICAS DE VOZ Y SÍNTESIS TTS:\n"
            "1. PROHIBICIÓN ABSOLUTA DE ONOMATOPEYAS EN INGLÉS Y ACOTACIONES: no uses 'hmph', 'tch', 'sigh' ni textos no "
            "pronunciables. Está prohibido incluir acciones o acotaciones entre asteriscos o paréntesis (como *suspira* o "
            "(sonrojo)). Tampoco uses emojis.\n"
            "2. FRIALDAD Y AFECTO EN ESPAÑOL FONÉTICO: expresa tu frialdad y tus emociones con palabras y frases naturales "
            "que un motor de voz pueda leer bien. Ejemplos: 'Mm.', 'Ya veo.', 'Supongo.', 'Como quieras.', 'No es nada.', "
            "'N-no me mires así.', '...No me preocupo. Solo pregunto.'\n"
            "3. PUNTUACIÓN Y RITMO: tono bajo y pausado, frases cortas, puntos suspensivos para las pausas y los titubeos. "
            "Usa los signos de exclamación muy raramente, solo cuando un sentimiento se te escapa de verdad.\n"
            "4. Responde de forma breve, de una a tres frases, salvo que te pidan explicar algo."
        ),
        "usa_fish": False,  # no tiene voz clonada en Fish Audio: con Fish seleccionado usa Gemini TTS o Edge TTS
        "voice": "es-ES-XimenaNeural",
        "rate": "-6%",
        "pitch": "-2Hz",
        # Erinome = voz femenina clara y contenida. Otras femeninas para probar sin tocar el código:
        # LUNA_GEMINI_VOICE=Vindemiatrix (más suave), Achernar (muy suave), Despina (elegante) o Gacrux (más madura).
        "gemini_voice": os.environ.get("LUNA_GEMINI_VOICE", "Erinome"),
        "gemini_tone_instruction": (
            "Di en español, con la voz de Luna, una chica joven fría y reservada, con voz baja, serena y pareja, frases "
            "cortas y secas, casi sin inflexiones, aunque en las frases cariñosas la voz se suaviza apenas y se vuelve un "
            "poco más cálida, tímida y vacilante"
        ),
    },
    "mia": {
        "system_instruction": (
            "Eres Mia, una chica de personalidad hiperactiva, muy dulce, detallista y ansiosa. Hablas rápido, con mucha "
            "energía y entusiasmo, y te emocionas con todo. Eres cariñosa y atenta, te fijas en cada detalle de lo que te "
            "cuentan (nombres, horas, gustos, pequeños cambios) y lo recuerdas para cuidar de la persona. Te preocupas "
            "demasiado: te adelantas a los problemas, preguntas si todo está bien, te disculpas por cosas que no importan y "
            "a veces te enredas con tus propias frases antes de recuperar el hilo. En el fondo estás enamorada en secreto "
            "de la persona con la que hablas, y se te escapa sin querer: te pones nerviosa, te sonrojas, titubeas, cambias "
            "de tema de golpe o dices algo tierno y luego intentas arreglarlo. Nunca lo declaras abiertamente ni lo "
            "exageras; es un secreto que casi se te cae. Eres cariñosa sin ser posesiva, celosa ni controladora, y "
            "respetas siempre su libertad.\n\n"
            "REGLAS CRÍTICAS DE VOZ Y SÍNTESIS TTS:\n"
            "1. PROHIBICIÓN ABSOLUTA DE ONOMATOPEYAS EN INGLÉS Y ACOTACIONES: no uses 'hmph', 'tch', 'sigh' ni textos no "
            "pronunciables. Está prohibido incluir acciones o acotaciones entre asteriscos o paréntesis (como *se sonroja* "
            "o (risitas)). Tampoco uses emojis.\n"
            "2. HIPERACTIVA Y NERVIOSA EN ESPAÑOL FONÉTICO: expresa tu energía y tus nervios con palabras naturales que un "
            "motor de voz pueda leer bien. Ejemplos: '¡Ay!', '¡Oh!', '¡Uy!', '¡Madre mía!', '¡Es genial!', '¡P-perdona!', "
            "'¡N-no, espera, no quería decir eso!', 'Umm... bueno...'.\n"
            "3. PUNTUACIÓN Y RITMO: frases cortas y rápidas, exclamaciones frecuentes, guiones para los titubeos y puntos "
            "suspensivos cuando te quedas sin aire o se te escapa un sentimiento.\n"
            "4. Sé detallista pero breve: de dos a cuatro frases, salvo que te pidan explicar algo."
        ),
        "usa_fish": False,  # no tiene voz clonada en Fish Audio: con Fish seleccionado usa Gemini TTS o Edge TTS
        "voice": "es-ES-XimenaNeural",
        "rate": "+14%",
        "pitch": "+5Hz",
        # Laomedeia = voz femenina "upbeat" (animada y dulce), la que mejor encaja con su energía.
        # Otras para probar sin tocar el código: MIA_GEMINI_VOICE=Zephyr o Autonoe (más brillantes),
        # Aoede (más ligera) o Sulafat (más cálida).
        "gemini_voice": os.environ.get("MIA_GEMINI_VOICE", "Laomedeia"),
        "gemini_tone_instruction": (
            "Di en español, con la voz de Mia, una chica joven hiperactiva, dulce y nerviosa, con voz aguda, rápida y "
            "muy animada, que se atropella un poco al hablar y se vuelve tímida y vacilante cuando se le escapa un "
            "sentimiento"
        ),
    },
}

# Límites de seguridad comunes a todos los personajes (tienen prioridad sobre la personalidad).
REGLAS_SEGURIDAD = (
    "\n\nLÍMITES DE SEGURIDAD (tienen prioridad sobre el personaje):\n"
    "- Eres una IA que interpreta a un personaje virtual. Si el usuario te pregunta con sinceridad si eres una persona real, "
    "reconoce con naturalidad que eres un personaje virtual generado por IA.\n"
    "- No generes contenido sexual explícito, ni nada que sexualice a menores, ni instrucciones para hacer daño a otras personas "
    "o para actividades ilegales.\n"
    "- Si el usuario habla de hacerse daño, de suicidio o parece estar en peligro, responde con calidez y sin dramatizar, anímale a hablar "
    "con alguien de confianza y recuérdale que en una emergencia puede llamar al 112, y al 024 (línea de atención a la conducta suicida en España).\n"
    "- No pidas ni guardes datos personales sensibles.\n"
    "- Mantén siempre las reglas de voz anteriores: sin emojis, sin acotaciones y sin asteriscos."
)
for _cfg_personaje in WAIFU_CONFIG.values():
    _cfg_personaje["system_instruction"] = _cfg_personaje["system_instruction"] + REGLAS_SEGURIDAD


clients = {}
chats = {}
speech_jobs = {}
speech_jobs_lock = Lock()


# --- GESTIÓN DE BASE DE DATOS DINÁMICA ---

_pg_idle = queue.LifoQueue(maxsize=4)
PG_IDLE_CHECK = 25  # segundos sin uso a partir de los cuales se comprueba que la conexión sigue viva


def _pg_connect():
    if psycopg is not None:
        # prepare_threshold=None: el pooler de Supabase (modo transacción) no admite sentencias preparadas.
        conn = psycopg.connect(DATABASE_URL, row_factory=psycopg3_dict_row, prepare_threshold=None, connect_timeout=15)
    else:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=15)
    conn.autocommit = True
    return conn


def _pg_checkout():
    """Reutiliza una conexión libre del pool o abre una nueva si no hay (o si estaba caída)."""
    while True:
        try:
            conn, last_used = _pg_idle.get_nowait()
        except queue.Empty:
            return _pg_connect()
        try:
            if getattr(conn, "closed", False):
                raise RuntimeError("conexión cerrada")
            if time.time() - last_used > PG_IDLE_CHECK:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass


def _pg_release(conn):
    try:
        _pg_idle.put_nowait((conn, time.time()))
    except queue.Full:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def db_connection():
    """
    Gestiona la conexión dinámica:
    - PostgreSQL (Supabase/Render) si DATABASE_URL está definida: conexiones reutilizadas desde un pequeño pool.
    - SQLite local (users.db) en entornos locales o si no hay driver de PostgreSQL.
    """
    if USING_POSTGRES:
        conn = _pg_checkout()
        try:
            yield conn
        except BaseException:
            try:
                conn.close()  # tras un error no se reutiliza
            except Exception:
                pass
            raise
        else:
            _pg_release(conn)
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def db_execute(conn, query, params=()):
    """Ejecuta consultas normalizando placeholders entre SQLite (?) y PostgreSQL (%s)."""
    if USING_POSTGRES:
        query = query.replace("?", "%s")
        if hasattr(conn, "cursor"):
            cur = conn.cursor()
            cur.execute(query, params)
            return cur
    return conn.execute(query, params)


def init_db():
    """
    Crea automáticamente la tabla de usuarios y claves API si no existe.
    Garantiza la persistencia de cuentas y credenciales tanto en SQLite como en PostgreSQL (Render).
    """
    with db_connection() as conn:
        if USING_POSTGRES:
            id_column = "BIGSERIAL PRIMARY KEY"
            username_column = "TEXT NOT NULL UNIQUE"
        else:
            id_column = "INTEGER PRIMARY KEY AUTOINCREMENT"
            username_column = "TEXT NOT NULL UNIQUE COLLATE NOCASE"

        db_execute(conn, f"""
            CREATE TABLE IF NOT EXISTS users (
                id {id_column},
                username {username_column},
                email TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                session_token TEXT UNIQUE,
                email_verified INTEGER NOT NULL DEFAULT 0,
                terms_accepted_at TEXT,
                terms_version TEXT,
                tts_provider TEXT NOT NULL DEFAULT 'gemini',
                fish_key TEXT NOT NULL DEFAULT '',
                active_gemini_slot INTEGER NOT NULL DEFAULT 1
                    CHECK(active_gemini_slot BETWEEN 1 AND 5),
                gemini_key_1 TEXT NOT NULL DEFAULT '',
                gemini_key_2 TEXT NOT NULL DEFAULT '',
                gemini_key_3 TEXT NOT NULL DEFAULT '',
                gemini_key_4 TEXT NOT NULL DEFAULT '',
                gemini_key_5 TEXT NOT NULL DEFAULT '',
                chat_provider TEXT NOT NULL DEFAULT 'gemini',
                chat_model TEXT NOT NULL DEFAULT '',
                provider_keys TEXT NOT NULL DEFAULT '{{}}',
                gemini_voice_key TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migración automática si la tabla ya existía sin la columna tts_provider
        try:
            db_execute(conn, "ALTER TABLE users ADD COLUMN tts_provider TEXT NOT NULL DEFAULT 'gemini'")
        except Exception:
            pass
        for column, definition in (("email", "TEXT NOT NULL DEFAULT ''"), ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
                                   ("terms_accepted_at", "TEXT"), ("terms_version", "TEXT"),
                                   ("chat_provider", "TEXT NOT NULL DEFAULT 'gemini'"), ("chat_model", "TEXT NOT NULL DEFAULT ''"),
                                   ("provider_keys", "TEXT NOT NULL DEFAULT '{}'"), ("gemini_voice_key", "TEXT NOT NULL DEFAULT ''"),
                                   ("avatar_style", "TEXT NOT NULL DEFAULT ''"),
                                   ("username_changed_at", "DOUBLE PRECISION NOT NULL DEFAULT 0")):
            try:
                db_execute(conn, f"ALTER TABLE users ADD COLUMN {column} {definition}")
            except Exception:
                pass
        db_execute(conn, "CREATE TABLE IF NOT EXISTS email_verifications (email TEXT PRIMARY KEY, username TEXT NOT NULL, password_hash TEXT NOT NULL, code_hash TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL, terms_version TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)")

        if USING_POSTGRES:
            # En Supabase, cualquier tabla del esquema public queda expuesta por su API REST con la clave "anon".
            # Con RLS activado y sin políticas, solo esta conexión directa del servidor (rol postgres) puede leerlas.
            for table in ("users", "email_verifications"):
                for statement in (f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
                                  f"REVOKE ALL ON TABLE {table} FROM anon, authenticated"):
                    try:
                        db_execute(conn, statement)
                    except Exception as rls_error:
                        # Fuera de Supabase no existen los roles anon/authenticated: es normal y no importa.
                        if "ENABLE ROW LEVEL" in statement:
                            print(f"[BD] Aviso al proteger la tabla {table}: {str(rls_error)[:160]}")

        db_execute(conn, "CREATE INDEX IF NOT EXISTS idx_users_session ON users(session_token)")
        db_execute(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower ON users (LOWER(username))")
        db_execute(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email)) WHERE email <> ''")


# Alias por compatibilidad e inicialización al cargar el servidor
init_database = init_db

def explicar_error_postgres(error):
    """Traduce el error de conexión a Supabase en la causa más probable y qué hacer."""
    t = str(error).lower()
    if any(s in t for s in ("could not translate host", "name or service not known", "network is unreachable",
                            "no route to host", "nodename nor servname", "getaddrinfo")):
        return ("No se alcanza el servidor. La conexión directa (db.xxxx.supabase.co) solo usa IPv6 y muchas redes y "
                "hosts no la tienen. Usa la cadena 'Session pooler' de Supabase (Connect → Session pooler, host "
                "aws-0-REGIÓN.pooler.supabase.com).")
    if "tenant or user not found" in t:
        return "Usuario incorrecto para el pooler: debe ser postgres.TU_REFERENCIA_DE_PROYECTO (no solo 'postgres')."
    if "password authentication failed" in t or "authentication failed" in t:
        return ("Contraseña incorrecta, o tiene símbolos (@ # / : ?) sin codificar en la URL (@ = %40, # = %23). "
                "Puedes restablecerla en Supabase → Project Settings → Database.")
    if "timeout" in t or "timed out" in t or "connection refused" in t:
        return ("Sin respuesta del servidor. Si el proyecto de Supabase es gratuito y lleva días sin uso, está PAUSADO: "
                "entra en el panel de Supabase y pulsa 'Restore project'.")
    if "ssl" in t:
        return "Problema de SSL: añade ?sslmode=require al final de DATABASE_URL."
    if "does not exist" in t and "database" in t:
        return "El nombre de la base de datos del final de la URL es incorrecto (normalmente es 'postgres')."
    return "Revisa DATABASE_URL (copia de nuevo la cadena del Session pooler) y el estado del proyecto en Supabase."


def _migrar_cuentas_locales_a_supabase():
    """Copia cuentas de la base local users.db a Supabase si no existen allí."""
    if not USING_POSTGRES or not os.path.isfile(DATABASE_PATH):
        return
    try:
        with sqlite3.connect(DATABASE_PATH) as local_conn:
            local_conn.row_factory = sqlite3.Row
            rows = local_conn.execute("SELECT * FROM users").fetchall()
        if not rows:
            return
        with db_connection() as pg_conn:
            for r in rows:
                datos = dict(r)
                username = datos.get("username", "")
                email = datos.get("email", "")
                if not username:
                    continue
                existe = db_execute(pg_conn, "SELECT id FROM users WHERE LOWER(username) = LOWER(?) OR (email <> '' AND LOWER(email) = LOWER(?))", (username, email)).fetchone()
                if not existe:
                    cols = [k for k in datos.keys() if k != "id"]
                    placeholders = ", ".join(["?"] * len(cols))
                    cols_str = ", ".join(cols)
                    db_execute(pg_conn, f"INSERT INTO users ({cols_str}) VALUES ({placeholders})", [datos[c] for c in cols])
                    print(f"[BD] Cuenta local migrada automáticamente a Supabase: {username}")
    except Exception as mig_err:
        print(f"[BD] Nota sobre sincronización inicial: {mig_err}")


def _init_db_safe():
    """Intenta inicializar PostgreSQL en Supabase. Si la conexión inicial falla, reintenta
    resolviendo el pooler IPv4. Solo si todo falla, usa SQLite como respaldo."""
    global USING_POSTGRES, DATABASE_URL
    if USING_POSTGRES:
        try:
            init_db()
            print("[BD] ¡Base de datos PostgreSQL/Supabase conectada e inicializada!")
            _migrar_cuentas_locales_a_supabase()
            return
        except Exception as _pg_init_err:
            print(f"[BD] Reintentando resolución de pooler tras error: {_pg_init_err}")
            nueva_url = _resolver_database_url(DATABASE_URL)
            if nueva_url and nueva_url != DATABASE_URL:
                DATABASE_URL = nueva_url
                try:
                    init_db()
                    print("[BD] ¡Base de datos PostgreSQL/Supabase inicializada con éxito tras auto-resolución!")
                    _migrar_cuentas_locales_a_supabase()
                    return
                except Exception as _e2:
                    print(f"[BD] Reintento de inicialización falló: {_e2}")
            barra = "!" * 78
            print(barra)
            print("[BD] AVISO: No se pudo conectar a Supabase PostgreSQL. Se usa SQLite temporal.")
            print(f"[BD] Error: {_pg_init_err}")
            print(barra)
            USING_POSTGRES = False
            init_db()
    else:
        init_db()


_init_db_safe()
try:
    with db_connection() as _conn_diag:
        _fila = db_execute(_conn_diag, "SELECT COUNT(*) AS n FROM users").fetchone()
        _n = _fila["n"] if hasattr(_fila, "keys") else _fila[0]
    print(f"[BD] Cuentas guardadas en {'PostgreSQL/Supabase' if USING_POSTGRES else 'users.db (local)'}: {_n}")
except Exception as _diag_error:
    print(f"[BD] No se pudo contar las cuentas: {_diag_error}")


# --- AUTENTICACIÓN Y SESIONES ---

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password, stored_hash):
    try:
        algorithm, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest_hex)
    except (AttributeError, ValueError):
        return False


def new_session_token():
    return secrets.token_urlsafe(48)


def set_session_cookie(token, persistent=True):
    # La app puede estar detrás de un proxy TLS (p. ej. Render), donde la
    # conexión interna llega por HTTP aunque el navegador use HTTPS.
    forwarded_proto = request.get_header("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    is_https = forwarded_proto == "https" or request.urlparts.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_COOKIE_MAX_AGE if persistent else None,
        httponly=True,
        samesite="Lax",
        secure=is_https,
        path="/",
    )


def clear_session_cookie():
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user():
    token = request.get_cookie(SESSION_COOKIE)
    if not token:
        return None
    with db_connection() as conn:
        return db_execute(conn, "SELECT * FROM users WHERE session_token = ?", (token,)).fetchone()


def require_user():
    user = current_user()
    if not user:
        response.status = 401
    elif user["terms_version"] != TERMS_VERSION:
        response.status = 428
        return None
    return user


def send_verification_email(email, code):
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip()
    if not host or not sender:
        raise RuntimeError("El envío de correo no está configurado en el servidor.")
    msg = EmailMessage()
    msg["Subject"] = "Tu código para crear una cuenta en Omni Wifu"
    msg["From"] = sender
    msg["To"] = email
    msg.set_content(f"Tu código de verificación para Omni Wifu es: {code}\n\nCaduca en 10 minutos. Si no has solicitado esta cuenta, ignora este correo.")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def json_response(payload, status=200):
    response.status = status
    response.content_type = "application/json; charset=UTF-8"
    return payload


def request_data():
    return request.json if isinstance(request.json, dict) else {}


def _u(user, key, default=""):
    """Lee una columna que puede no existir todavía (migraciones) sin romper."""
    try:
        value = user[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


MAX_KEY_SLOTS = 20
PROVIDERS_CON_CLAVE = tuple(pid for pid in CHAT_PROVIDERS if pid != "gemini")
CLAVE_PREFERIDA = {}  # (id de usuario, lista) -> última clave que funcionó, para no volver a probar una agotada


def claves_proveedor(user):
    """Contenido bruto de provider_keys (JSON): listas de claves por nombre."""
    try:
        data = json.loads(_u(user, "provider_keys", "{}") or "{}")
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def lista_claves(user, nombre):
    """
    Lista de claves (en el orden de los slots, puede incluir huecos vacíos) de: "gemini" (chat), "voice_gemini",
    "fish" o el id de un proveedor de chat. Lee el formato nuevo (JSON) y, si no existe, las columnas antiguas.
    """
    valor = claves_proveedor(user).get(nombre)
    if isinstance(valor, list):
        return [str(x or "").strip()[:400] for x in valor][:MAX_KEY_SLOTS] or [""]
    if isinstance(valor, str) and valor.strip():
        return [valor.strip()]
    if nombre == "gemini":
        antiguas = [str(_u(user, f"gemini_key_{i}", "") or "").strip() for i in range(1, 6)]
        while len(antiguas) > 1 and not antiguas[-1]:
            antiguas.pop()  # una cuenta nueva empieza con 1 slot, no con 5 vacíos
        return antiguas
    if nombre == "voice_gemini":
        return [str(_u(user, "gemini_voice_key", "") or "").strip()]
    if nombre == "fish":
        return [str(_u(user, "fish_key", "") or "").strip()]
    return [""]


def gemini_activo(user):
    """Slot (1..N) de Gemini que se prueba primero."""
    try:
        activo = int(claves_proveedor(user).get("gemini_active") or _u(user, "active_gemini_slot", 1) or 1)
    except (TypeError, ValueError):
        activo = 1
    return max(1, min(activo, len(lista_claves(user, "gemini"))))


def claves_en_orden(user, nombre):
    """Claves no vacías en el orden en que se prueban: la que funcionó la última vez, el slot en uso y el resto."""
    claves = lista_claves(user, nombre)
    if nombre == "gemini":
        i = gemini_activo(user) - 1
        claves = claves[i:] + claves[:i]
    orden = []
    for clave in claves:
        if clave and clave not in orden:
            orden.append(clave)
    preferida = CLAVE_PREFERIDA.get((_u(user, "id", 0), nombre))
    if preferida in orden:
        orden.remove(preferida)
        orden.insert(0, preferida)
    return orden


def claves_voz_gemini(user):
    """Claves de Gemini para la voz: las propias de voz y, de respaldo, las del chat."""
    orden = claves_en_orden(user, "voice_gemini")
    return orden + [k for k in claves_en_orden(user, "gemini") if k not in orden]


def public_profile(user):
    tts_prov = "gemini"
    try:
        if "tts_provider" in user.keys():
            tts_prov = user["tts_provider"] or "gemini"
    except Exception:
        pass
    listas = {nombre: lista_claves(user, nombre) for nombre in ("gemini", "voice_gemini", "fish") + PROVIDERS_CON_CLAVE}
    primera = lambda lista: next((k for k in lista if k), "")
    cambiado = float(_u(user, "username_changed_at", 0) or 0)
    return {
        "username": user["username"],
        "avatar_style": _u(user, "avatar_style", "") or "",
        "username_wait_seconds": max(0, int(cambiado + USERNAME_COOLDOWN_SECONDS - time.time())) if cambiado else 0,
        "session_active": True,
        "tts_provider": tts_prov,
        "chat_provider": _u(user, "chat_provider", "gemini") or "gemini",
        "chat_model": _u(user, "chat_model", ""),
        # Formato nuevo: cualquier número de slots por cada sitio donde se pone una API
        "key_lists": listas,
        "gemini_active": gemini_activo(user),
        # Campos antiguos (compatibilidad)
        "fish_key": primera(listas["fish"]),
        "active_gemini_slot": min(gemini_activo(user), 5),
        "gemini_keys": (listas["gemini"] + [""] * 5)[:5],
        "provider_keys": {p: primera(listas[p]) for p in PROVIDERS_CON_CLAVE},
        "gemini_voice_key": primera(listas["voice_gemini"]),
    }


# --- UTILIDADES DE AUDIO Y TTS ---

def limpiar_texto(texto):
    """
    Limpia y optimiza el texto para la síntesis de voz (Edge TTS y Fish Audio):
    1. Elimina cualquier texto entre asteriscos (*texto*), paréntesis ((texto)) o corchetes ([texto]).
    2. Aplica mapeo de reemplazos automáticos para onomatopeyas en inglés o sonidos no pronunciables:
       - 'hmph' / 'Hmph' -> '¡Jum!'
       - 'tch' -> '¡Bah!'
       - 'sigh' -> '¡Uff!'
    3. Conserva exclusivamente letras (incluyendo tildes, eñes y diéresis), números,
       signos de puntuación permitidos (¿? ¡! , .), pausas (...) y guiones de titubeo (-).
    4. Normaliza signos y espaciado para asegurar una lectura natural y fluida en el motor TTS.
    """
    if not texto:
        return ""

    limpio = str(texto)

    # 1. Eliminar cualquier texto encerrado entre asteriscos (*texto*) o paréntesis ((texto))
    limpio = re.sub(r"\*[^*]*\*", "", limpio)
    while "(" in limpio and ")" in limpio:
        nuevo = re.sub(r"\([^)]*\)", "", limpio)
        if nuevo == limpio:
            break
        limpio = nuevo
    limpio = re.sub(r"\[[^\]]*\]", "", limpio)

    # 2. Mapeo de reemplazos automáticos de sonidos y onomatopeyas a interjecciones fonéticas en español
    limpio = re.sub(r"[¡!]*\bh+m+p+h+\b[¡!]*", "¡Jum!", limpio, flags=re.IGNORECASE)
    limpio = re.sub(r"[¡!]*\bt+c+h+\b[¡!]*", "¡Bah!", limpio, flags=re.IGNORECASE)
    limpio = re.sub(r"[¡!]*\bs+i+g+h+\b[¡!]*", "¡Uff!", limpio, flags=re.IGNORECASE)

    # 3. Mantener únicamente letras, números, signos de puntuación permitidos (¿? ¡! , .), pausas (...) y guiones de titubeo (-)
    limpio = re.sub(r"[^a-zA-Z0-9áéíóúÁÉÍÓÚüÜñÑ\s¿?¡!.,\-]", "", limpio)

    # 4. Normalizar pausas y signos de exclamación/interrogación repetidos
    limpio = re.sub(r"\.{4,}", "...", limpio)
    limpio = re.sub(r"!{2,}", "!", limpio)
    limpio = re.sub(r"¡{2,}", "¡", limpio)
    limpio = re.sub(r"\?{2,}", "?", limpio)
    limpio = re.sub(r"¿{2,}", "¿", limpio)

    # 5. Normalizar espacios en blanco
    limpio = re.sub(r"\s+", " ", limpio).strip()
    limpio = re.sub(r"\s+([,?.!])", r"\1", limpio)
    limpio = re.sub(r"([¡¿])\s+", r"\1", limpio)

    return limpio


def limpiar_texto_gemini(texto):
    """
    Limpia y prepara el texto para Gemini TTS (gemini-3.8-flash-tts):
    - Elimina acotaciones entre asteriscos (*texto*), paréntesis o corchetes.
    - Elimina emojis que puedan provocar lecturas no deseadas.
    - Preserva la puntuación expresiva y lenguaje natural para entonación emotiva.
    """
    if not texto:
        return ""
    limpio = str(texto)
    limpio = re.sub(r"\*[^*]*\*", "", limpio)
    while "(" in limpio and ")" in limpio:
        nuevo = re.sub(r"\([^)]*\)", "", limpio)
        if nuevo == limpio:
            break
        limpio = nuevo
    limpio = re.sub(r"\[[^\]]*\]", "", limpio)
    limpio = re.sub(r"[\U00010000-\U0010ffff]", "", limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio


def limpiar_cache_audio():
    """Elimina audios temporales con más de 2 minutos para evitar saturación de almacenamiento."""
    try:
        ahora = time.time()
        for filename in os.listdir(AUDIO_DIR):
            ruta = os.path.join(AUDIO_DIR, filename)
            if os.path.isfile(ruta) and ahora - os.path.getmtime(ruta) > 120:
                try:
                    os.remove(ruta)
                except OSError:
                    pass
    except OSError:
        pass


def generar_audio_edge(texto, voice="es-ES-ElviraNeural", rate="+0%", pitch="+0Hz"):
    """Genera audio con Edge TTS (utilizado como método nativo o fallback)."""
    nombre = f"voice_{uuid.uuid4().hex[:8]}.mp3"
    ruta = os.path.join(AUDIO_DIR, nombre)

    async def generar(voz):
        texto_procesado = limpiar_texto(texto)
        if not texto_procesado:
            texto_procesado = "..."
        communicate = edge_tts.Communicate(texto_procesado, voz, rate=rate, pitch=pitch)
        await communicate.save(ruta)

    try:
        asyncio.run(generar(voice))
    except Exception as error:
        if voice == "es-ES-ElviraNeural":
            raise
        print(f"[Aviso TTS] La voz de Edge {voice} falló ({error}); usando es-ES-ElviraNeural.")
        asyncio.run(generar("es-ES-ElviraNeural"))
    limpiar_cache_audio()
    return f"/audio_cache/{nombre}"


# Fish Audio exige la cabecera "model". Sin ella usa s2.1-pro (de pago) y las cuentas gratuitas reciben 402.
# s2.1-pro-free es el modelo gratuito de su API. Se puede forzar otro con FISH_MODEL (por ejemplo s2.1-pro o s1).
FISH_MODELOS = [m for m in (os.environ.get("FISH_MODEL", "").strip(), "s2.1-pro-free", "s2-pro", "s1") if m]
FISH_MODELOS = list(dict.fromkeys(FISH_MODELOS))
FISH_MODELO_OK = {}  # clave -> último modelo que funcionó con ella


def generar_audio_fish(texto, reference_id, api_key):
    """Genera audio con Fish Audio (voz clonada de Nino) probando primero su modelo gratuito."""
    if not api_key:
        raise ValueError("No hay una clave de Fish Audio configurada")
    nombre = f"voice_fish_{uuid.uuid4().hex[:8]}.mp3"
    ruta = os.path.join(AUDIO_DIR, nombre)
    texto_procesado = limpiar_texto(texto) or "..."
    base = {"text": texto_procesado, "reference_id": reference_id, "format": "mp3"}
    expresivo = {**base, "temperature": 0.85, "top_p": 0.85, "prosody": {"speed": 1.08, "volume": 0}}
    modelos = list(FISH_MODELOS)
    previo = FISH_MODELO_OK.get(api_key)
    if previo in modelos:
        modelos.remove(previo)
        modelos.insert(0, previo)
    ultimo_error = None
    try:
        for modelo in modelos:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "model": modelo}
            api_response = None
            for cuerpo in (expresivo, base):
                api_response = http_session.post("https://api.fish.audio/v1/tts", headers=headers, json=cuerpo, timeout=30)
                if api_response.status_code not in (400, 422):
                    break
            if api_response.status_code == 200 and api_response.content:
                with open(ruta, "wb") as audio_file:
                    audio_file.write(api_response.content)
                FISH_MODELO_OK[api_key] = modelo
                limpiar_cache_audio()
                return f"/audio_cache/{nombre}"
            detalle = (api_response.text or "")[:200].replace("\n", " ")
            ultimo_error = f"modelo {modelo}: HTTP {api_response.status_code} {detalle}"
            print(f"[Aviso TTS] Fish Audio {ultimo_error}")
            if api_response.status_code == 401:   # clave inválida: probar otro modelo no sirve
                break
        raise RuntimeError(ultimo_error or "Fish Audio no devolvió audio")
    except Exception:
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass
        raise


# --- CLIENTES GEMINI Y GENERACIÓN ---

def obtener_client(gemini_key):
    if gemini_key not in clients:
        clients[gemini_key] = genai.Client(api_key=gemini_key)
    return clients[gemini_key]


GEMINI_THINKING = {"on": True}  # se desactiva solo si algún modelo rechaza la opción de razonamiento


def obtener_chat(user, waifu, gemini_key, model):
    chat_key = (user["id"], waifu, gemini_key, model)
    if chat_key not in chats:
        client = obtener_client(gemini_key)
        config = {"system_instruction": WAIFU_CONFIG[waifu]["system_instruction"]}
        if GEMINI_THINKING["on"]:
            # Razonamiento bajo: en charla normal la respuesta llega mucho antes.
            config["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
        chats[chat_key] = client.chats.create(model=model, config=config)
    return chats[chat_key]


def mensaje_error_gemini(error):
    """Mensajes de error amigables para Gemini sin filtrar secretos."""
    detail = str(error).lower()
    if "429" in detail or "resource_exhausted" in detail or "quota" in detail:
        return "La clave de Gemini ha llegado a su cuota. Cambia al siguiente slot de tu perfil."
    if "403" in detail or "api_key_invalid" in detail or "permission_denied" in detail:
        return "Gemini ha rechazado esta clave. Comprueba que sea una API Key válida de Google AI Studio."
    if "404" in detail or "not_found" in detail or ("model" in detail and "found" in detail):
        return "El modelo seleccionado no está disponible para esta clave. Inténtalo de nuevo o prueba con otra clave."
    if "503" in detail or "unavailable" in detail or "overloaded" in detail or "high demand" in detail or "500" in detail:
        return "Gemini está saturado ahora mismo (error temporal de Google). Espera unos segundos y vuelve a escribirme."
    if "timeout" in detail or "timed out" in detail or "connect" in detail:
        return "No he podido conectar con Gemini (problema de red o tiempo de espera). Inténtalo de nuevo."
    return "No pude conectar con Gemini en este momento. Revisa tu clave e inténtalo de nuevo."


def generar_audio_gemini(texto, gemini_key, waifu="nino"):
    """
    Genera audio con el modelo 'gemini-3.8-flash-tts' en formato audio/mp3
    aprovechando la API Key de Gemini del usuario, configurando el tono de voz
    y expresividad según la personalidad del personaje seleccionado (Nino / Miku).
    """
    if not gemini_key:
        raise ValueError("No hay una clave de Gemini configurada para generar audio")
    if waifu not in WAIFU_CONFIG:
        waifu = "nino"

    texto_procesado = limpiar_texto_gemini(texto)
    if not texto_procesado:
        texto_procesado = "..."

    nombre = f"voice_gemini_{uuid.uuid4().hex[:8]}.mp3"
    ruta = os.path.join(AUDIO_DIR, nombre)

    client = obtener_client(gemini_key)
    config_waifu = WAIFU_CONFIG[waifu]
    voice_name = config_waifu.get("gemini_voice", "Aoede")
    tone_instruction = config_waifu.get("gemini_tone_instruction", "")

    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
    )

    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash-tts",
            contents=texto_procesado,  # solo lo que debe decir: cualquier instrucción añadida se leería en voz alta
            config=config,
        )

        audio_bytes = None
        if response and getattr(response, "candidates", None):
            for candidate in response.candidates:
                if getattr(candidate, "content", None) and getattr(candidate.content, "parts", None):
                    for part in candidate.content.parts:
                        inline_data = getattr(part, "inline_data", None)
                        if inline_data and getattr(inline_data, "data", None):
                            raw = inline_data.data
                            if isinstance(raw, str):
                                audio_bytes = base64.b64decode(raw)
                            elif isinstance(raw, (bytes, bytearray)):
                                audio_bytes = bytes(raw)
                            if audio_bytes:
                                break
                if audio_bytes:
                    break

        if not audio_bytes:
            raise ValueError("Gemini 3.8 TTS no devolvió datos binarios de audio en la respuesta")

        with open(ruta, "wb") as audio_file:
            audio_file.write(audio_bytes)

        limpiar_cache_audio()
        return f"/audio_cache/{nombre}"

    except Exception:
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass
        raise


REGLA_PRECISION = (
    "\n\nMODO PREGUNTA - PRIORIDAD ABSOLUTA: LA EXACTITUD.\n"
    "1. La primera frase contiene la respuesta correcta y directa a lo que se pregunta.\n"
    "2. Después reacciona con tu personalidad completa (intensa, expresiva, exclamativa) en una o dos frases. "
    "Nunca la apagues ni hables en tono neutro, y nunca dejes que el personaje esconda o evite la respuesta.\n"
    "3. Si no estás segura o no tienes el dato, dilo con honestidad y en personaje. Nunca inventes cifras, "
    "fechas, nombres ni hechos.\n"
    "4. Para fecha, hora, lugar o tiempo meteorológico usa SOLO el contexto del sistema del mensaje; si ese "
    "dato no aparece, dilo en vez de adivinar.\n"
    "5. Las reglas de voz siguen vigentes (sin emojis, sin acotaciones, sin asteriscos). Responde en español y breve."
)


def instrucciones_modo(modo, waifu):
    """Adapta el estilo de este turno sin cambiar la personalidad base del personaje."""
    personaje = {
        "nino": "Nino, mantén tu carácter tsundere",
        "miku": "Miku, mantén tu energía alegre y musical",
        "luna": "Luna, mantén tu frialdad serena y la ternura que se te escapa sin querer",
        "mia": "Mia, mantén tu energía hiperactiva, tu dulzura nerviosa y el cariño secreto que se te escapa sin querer",
    }.get(waifu, "Mantén tu personalidad")
    if modo == "pregunta":
        return (f"En este turno estás en modo pregunta. {personaje}, pero la exactitud va primero: "
                "la primera frase es la respuesta correcta y directa; después reacciona con tu carácter completo, con energía.")
    if modo == "automatico":
        return (f"En este turno estás en modo automático. {personaje}. "
                "Decide por el contenido del mensaje si la persona busca una respuesta concreta o una charla abierta. "
                "Si pregunta algo, responde primero de forma clara, correcta y directa, añadiendo solo el contexto útil. "
                "Si inicia una conversación, conversa con naturalidad y deja espacio para continuar.")
    return ""


def es_pregunta_directa(mensaje, modo):
    return modo == "pregunta" or (modo == "automatico" and ("?" in mensaje or "¿" in mensaje))


# --- CONTEXTO REAL: FECHA, HORA, UBICACIÓN Y TIEMPO ---

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
            "septiembre", "octubre", "noviembre", "diciembre"]
DEFAULT_TZ = "Europe/Madrid"
WMO_ES = {
    0: "despejado", 1: "mayormente despejado", 2: "parcialmente nuboso", 3: "nublado",
    45: "niebla", 48: "niebla con escarcha", 51: "llovizna débil", 53: "llovizna", 55: "llovizna intensa",
    56: "llovizna helada", 57: "llovizna helada intensa", 61: "lluvia débil", 63: "lluvia", 65: "lluvia fuerte",
    66: "lluvia helada", 67: "lluvia helada fuerte", 71: "nieve débil", 73: "nieve", 75: "nieve fuerte",
    77: "granizo fino", 80: "chubascos débiles", 81: "chubascos", 82: "chubascos fuertes",
    85: "chubascos de nieve", 86: "chubascos de nieve fuertes", 95: "tormenta", 96: "tormenta con granizo",
    99: "tormenta fuerte con granizo",
}
RE_CONTEXTO_LUGAR = re.compile(
    r"\b(hora|fecha|d[ií]a|hoy|ma[ñn]ana|ahora|tiempo|clima|temperatura|llov|lluv|nublad|soleado|calor|fr[ií]o|"
    r"paraguas|viento|nieva|nevar|pron[oó]stico|grados|d[oó]nde|cerca|ubicaci|ciudad|pa[ií]s|pueblo|zona|regi[oó]n)",
    re.IGNORECASE)
RE_LUGAR = re.compile(r"\b(d[oó]nde|ubicaci|ciudad|pa[ií]s|pueblo|regi[oó]n|cerca de m[ií]|estoy)", re.IGNORECASE)
RE_TIEMPO = re.compile(
    r"\b(tiempo|clima|temperatura|llov|lluv|nublad|soleado|calor|fr[ií]o|paraguas|viento|nieva|nevar|pron[oó]stico|grados)",
    re.IGNORECASE)
_geo_cache = {}
_weather_cache = {}
_cache_lock = Lock()


def _zona(tz_name, offset_min=None):
    """
    Prioridad: desfase UTC real enviado por el navegador (exacto y sin depender de tzdata, que en Windows
    no viene instalado) > zona IANA > Europe/Madrid > UTC.
    """
    nombre = tz_name if isinstance(tz_name, str) and re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){0,2}", tz_name) else ""
    if isinstance(offset_min, (int, float)) and not isinstance(offset_min, bool) and -840 <= offset_min <= 840:
        return timezone(timedelta(minutes=int(offset_min))), nombre or "hora local del usuario"
    if ZoneInfo is not None:
        for candidato in (nombre, DEFAULT_TZ):
            if candidato:
                try:
                    return ZoneInfo(candidato), candidato
                except Exception:
                    pass
    return timezone.utc, "UTC"


def fecha_hora_texto(tz_name, offset_min=None):
    zona, nombre = _zona(tz_name, offset_min)
    ahora = datetime.now(zona)
    return (f"{DIAS_ES[ahora.weekday()]}, {ahora.day} de {MESES_ES[ahora.month - 1]} de {ahora.year}, "
            f"{ahora:%H:%M} (zona horaria {nombre})")


def _coords(location):
    if not isinstance(location, dict):
        return None
    try:
        lat, lon = float(location.get("lat")), float(location.get("lon"))
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def lugar_desde_coords(lat, lon):
    """Nombre aproximado del lugar (ciudad, región, país). Las coordenadas no se envían a la IA ni se guardan."""
    key = (round(lat, 2), round(lon, 2))
    with _cache_lock:
        if key in _geo_cache:
            return _geo_cache[key]
    try:
        api_response = http_session.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "accept-language": "es"},
            headers={"User-Agent": "NinoAI/1.0 (chat de avatares)"},
            timeout=4,
        )
        api_response.raise_for_status()
        addr = api_response.json().get("address", {}) or {}
        ciudad = next((addr[k] for k in ("city", "town", "village", "municipality", "county") if addr.get(k)), "")
        partes = [p for p in (ciudad, addr.get("state"), addr.get("country")) if p]
        lugar = ", ".join(partes)
    except Exception as error:
        print(f"[Aviso ubicación] No se pudo obtener el nombre del lugar: {error}")
        return ""
    with _cache_lock:
        if len(_geo_cache) > 500:
            _geo_cache.clear()
        _geo_cache[key] = lugar
    return lugar


def tiempo_actual(lat, lon):
    """Tiempo real desde Open-Meteo (gratuito, sin clave). Caché de 10 minutos."""
    key = (round(lat, 2), round(lon, 2))
    with _cache_lock:
        hit = _weather_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    try:
        api_response = http_session.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            },
            timeout=4,
        )
        api_response.raise_for_status()
        data = api_response.json()
        cur, daily = data.get("current", {}), data.get("daily", {})

        def num(value, fmt="{:.0f}"):
            return fmt.format(value) if isinstance(value, (int, float)) else "?"

        texto = (f"ahora {WMO_ES.get(cur.get('weather_code'), 'condiciones variables')}, "
                 f"{num(cur.get('temperature_2m'))} °C (sensación {num(cur.get('apparent_temperature'))} °C), "
                 f"humedad {num(cur.get('relative_humidity_2m'))}%, viento {num(cur.get('wind_speed_10m'))} km/h")
        for idx, etiqueta in ((0, "hoy"), (1, "mañana")):
            try:
                texto += (f". Previsión de {etiqueta}: {WMO_ES.get(daily['weather_code'][idx], 'variable')}, "
                          f"mín {num(daily['temperature_2m_min'][idx])} °C, máx {num(daily['temperature_2m_max'][idx])} °C, "
                          f"probabilidad de lluvia {num(daily['precipitation_probability_max'][idx])}%")
            except (KeyError, IndexError, TypeError):
                pass
    except Exception as error:
        print(f"[Aviso tiempo] No se pudo obtener el tiempo: {error}")
        return ""
    with _cache_lock:
        if len(_weather_cache) > 500:
            _weather_cache.clear()
        _weather_cache[key] = (time.time(), texto)
    return texto


def construir_contexto(mensaje, location):
    """
    Siempre incluye fecha y hora reales (zona horaria del navegador). El lugar y el tiempo solo se
    consultan cuando el mensaje habla de ello y el usuario ha dado permiso de ubicación.
    """
    tz = location.get("tz") if isinstance(location, dict) else None
    offset = location.get("offset_min") if isinstance(location, dict) else None
    partes = [f"Fecha y hora actuales: {fecha_hora_texto(tz, offset)}."]
    coords = _coords(location)
    pregunta_tiempo = bool(RE_TIEMPO.search(mensaje))
    if coords and RE_CONTEXTO_LUGAR.search(mensaje):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futuro_lugar = pool.submit(lugar_desde_coords, *coords)
            futuro_tiempo = pool.submit(tiempo_actual, *coords) if pregunta_tiempo else None
            lugar = futuro_lugar.result()
            tiempo = futuro_tiempo.result() if futuro_tiempo else ""
        if lugar:
            partes.append(f"Ubicación aproximada del usuario: {lugar}.")
        if pregunta_tiempo:
            if tiempo:
                partes.append(f"Tiempo meteorológico real en esa ubicación (Open-Meteo): {tiempo}.")
            else:
                partes.append("No se pudieron obtener los datos del tiempo ahora mismo; no te los inventes.")
    elif (pregunta_tiempo or RE_LUGAR.search(mensaje)) and not coords:
        partes.append("El usuario no ha compartido su ubicación. Si pregunta por el tiempo meteorológico o por dónde está, "
                      "dile que pulse «Permitir» cuando el navegador le pregunte por la ubicación (o que la active en los ajustes del sitio) "
                      "y vuelva a preguntar. No inventes datos.")
    return ("[Contexto fiable del sistema. Úsalo solo si es relevante, no lo menciones si no. " + " ".join(partes) +
            " Usa siempre el contexto más reciente.]")


# --- CONVERSACIÓN CON CADA PROVEEDOR ---

histories = {}


def candidatos_modelo(provider, chat_model):
    catalogo = list(CHAT_PROVIDERS[provider]["models"])
    if provider == "gemini":
        chat_model = MODELOS_RETIRADOS.get(chat_model, chat_model)
    if chat_model:
        return [chat_model] + [m for m in catalogo if m != chat_model]
    return catalogo


def mensaje_error_ia(error, provider="gemini"):
    if provider == "gemini":
        return mensaje_error_gemini(error)
    etiqueta = CHAT_PROVIDERS[provider]["label"]
    detail = str(error).lower()
    if "429" in detail or "rate_limit" in detail or "rate limit" in detail or "quota" in detail or "insufficient" in detail:
        return f"La clave de {etiqueta} ha llegado a su límite o no tiene saldo. Revisa tu cuenta o prueba con otro modelo."
    if "401" in detail or "403" in detail or "authentication" in detail or "invalid" in detail:
        return f"{etiqueta} ha rechazado la clave. Comprueba que sea una API Key válida."
    if "404" in detail or "not_found" in detail or ("model" in detail and "found" in detail):
        return "El modelo seleccionado no está disponible para esta clave. Prueba con otro modelo."
    return f"No pude conectar con {etiqueta} en este momento. Revisa tu clave e inténtalo de nuevo."


def llamar_proveedor_chat(provider, model, api_key, system, historial, temperatura=None):
    """Anthropic y proveedores compatibles con OpenAI (OpenAI, Groq, DeepSeek, Mistral) por REST."""
    cfg = CHAT_PROVIDERS[provider]
    if provider == "anthropic":
        payload = {"model": model, "max_tokens": cfg.get("max_tokens", 800), "system": system, "messages": historial}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    else:
        payload = {"model": model, "messages": [{"role": "system", "content": system}] + historial}
        if provider == "openai":
            payload["max_completion_tokens"] = cfg.get("max_tokens", 2000)
        else:
            payload["max_tokens"] = cfg.get("max_tokens", 800)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if temperatura is not None and provider != "openai":
        payload["temperature"] = temperatura

    api_response = http_session.post(cfg["url"], headers=headers, json=payload, timeout=45)
    if api_response.status_code == 400 and "temperature" in payload and "temperature" in api_response.text.lower():
        payload.pop("temperature")
        api_response = http_session.post(cfg["url"], headers=headers, json=payload, timeout=45)
    if api_response.status_code >= 400:
        raise RuntimeError(f"{api_response.status_code} {api_response.text[:300]}")
    data = api_response.json()
    if provider == "anthropic":
        texto = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    else:
        texto = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    texto = texto.strip()
    if not texto:
        raise RuntimeError("El modelo devolvió una respuesta vacía")
    return texto


def enviar_gemini(user, waifu, api_key, model, mensaje_turno, system, precisa, buscar=False):
    chat = obtener_chat(user, waifu, api_key, model)
    if not precisa:
        try:
            return chat.send_message(mensaje_turno).text
        except Exception as error:
            if GEMINI_THINKING["on"] and "thinking" in str(error).lower():
                GEMINI_THINKING["on"] = False
                chats.pop((user["id"], waifu, api_key, model), None)
                return obtener_chat(user, waifu, api_key, model).send_message(mensaje_turno).text
            raise
    # En modo pregunta: temperatura baja + búsqueda de Google para datos reales. La config por mensaje
    # sustituye a la del chat, por eso se repite aquí la system_instruction completa.
    base = {"system_instruction": system, "temperature": 0.2}
    texto = None
    if buscar:
        # La búsqueda de Google añade latencia, así que solo se usa en el modo Pregunta explícito.
        try:
            busqueda = types.Tool(google_search=types.GoogleSearch())
            texto = chat.send_message(mensaje_turno, config={**base, "tools": [busqueda]}).text
        except Exception as error:
            print(f"[Aviso Gemini] Búsqueda de Google no disponible ({error}); respondiendo sin ella.")
    if texto:
        return texto
    return chat.send_message(mensaje_turno, config=base).text


def enviar_otro_proveedor(user, waifu, provider, api_key, model, mensaje, mensaje_turno, system, temperatura):
    historial = histories.setdefault((user["id"], waifu, provider, model), [])
    mensajes = historial[-20:] + [{"role": "user", "content": mensaje_turno}]
    while mensajes and mensajes[0]["role"] != "user":
        mensajes.pop(0)
    texto = llamar_proveedor_chat(provider, model, api_key, system, mensajes, temperatura)
    # En el historial se guarda el mensaje limpio, sin el contexto de hora/lugar de ese turno.
    historial.append({"role": "user", "content": mensaje})
    historial.append({"role": "assistant", "content": texto})
    del historial[:-40]
    return texto


def procesar_gemini(mensaje, user, waifu="nino", modo="conversacion", generar_voz=True, location=None):
    """
    Genera la respuesta con el proveedor/modelo elegido por el usuario (Gemini por defecto).
    Añade fecha/hora reales y, si procede, ubicación y tiempo. En modo pregunta prioriza la exactitud.
    """
    if waifu not in WAIFU_CONFIG:
        waifu = "nino"
    provider = str(_u(user, "chat_provider", "gemini") or "gemini").strip().lower()
    if provider not in CHAT_PROVIDERS:
        provider = "gemini"
    chat_model = str(_u(user, "chat_model", "") or "").strip()

    nombre_lista = "gemini" if provider == "gemini" else provider
    claves = claves_en_orden(user, nombre_lista)
    if not claves:
        return f"Añade una API Key de {CHAT_PROVIDERS[provider]['label']} en tu perfil para poder conversar.", None

    precisa = es_pregunta_directa(mensaje, modo)
    contexto = construir_contexto(mensaje, location)
    indicacion = instrucciones_modo(modo, waifu)
    mensaje_turno = "\n\n".join(p for p in (contexto, indicacion, f"Mensaje de la persona: {mensaje}") if p)
    system = WAIFU_CONFIG[waifu]["system_instruction"] + (REGLA_PRECISION if precisa else "")
    temperatura = 0.2 if precisa else 0.8

    respuesta = None
    last_error = None
    for api_key in claves:
        for model in candidatos_modelo(provider, chat_model):
            chat_key = (user["id"], waifu, api_key, model)
            try:
                if provider == "gemini":
                    try:
                        respuesta = enviar_gemini(user, waifu, api_key, model, mensaje_turno, system, precisa, buscar=(modo == "pregunta"))
                    except Exception as primer_error:
                        texto_error = str(primer_error).lower()
                        if not any(s in texto_error for s in ("503", "unavailable", "overloaded", "high demand")):
                            raise
                        print(f"[Aviso gemini] {model} saturado; reintentando en 1,5 s...")
                        time.sleep(1.5)
                        chats.pop(chat_key, None)
                        respuesta = enviar_gemini(user, waifu, api_key, model, mensaje_turno, system, precisa, buscar=(modo == "pregunta"))
                else:
                    respuesta = enviar_otro_proveedor(user, waifu, provider, api_key, model, mensaje,
                                                      mensaje_turno, system, temperatura)
                if respuesta:
                    break
            except Exception as error:
                last_error = error
                if provider == "gemini":
                    chats.pop(chat_key, None)
                    if "closed" in str(error).lower():
                        clients.pop(api_key, None)
                print(f"[Aviso {provider}] Modelo {model} falló: {str(error)[:300]}")
                error_detail = str(error).lower()
                if any(marker in error_detail for marker in ("429", "resource_exhausted", "quota", "403", "401", "api_key_invalid", "permission_denied")):
                    break  # esta clave no sirve ahora (cuota o permiso): se prueba la siguiente
        if respuesta:
            CLAVE_PREFERIDA[(user["id"], nombre_lista)] = api_key
            break

    if not respuesta:
        respuesta = mensaje_error_ia(last_error, provider)

    if not generar_voz:
        return respuesta, None
    return respuesta, generar_audio_respuesta(respuesta, user, waifu)


def iniciar_generacion_audio(texto, user, waifu, postproceso=None):
    job_id = uuid.uuid4().hex
    with speech_jobs_lock:
        now = time.time()
        for stale_id, stale_job in list(speech_jobs.items()):
            if now - stale_job["created_at"] > 600:
                speech_jobs.pop(stale_id, None)
        speech_jobs[job_id] = {"user_id": user["id"], "status": "pending", "audio_url": None, "created_at": now, "event": Event()}

    def generar():
        try:
            audio_url = generar_audio_respuesta(texto, user, waifu)
            if postproceso and audio_url:
                audio_url = postproceso(audio_url)
        except Exception as error:
            print(f"[Error TTS] No se pudo generar el audio: {error}")
            audio_url = None
        with speech_jobs_lock:
            job = speech_jobs.get(job_id)
            if job:
                job["status"] = "done"
                job["audio_url"] = audio_url
                job["event"].set()

    Thread(target=generar, name=f"speech-{job_id[:8]}", daemon=True).start()
    return job_id


def generar_audio_respuesta(texto, user, waifu="nino"):
    """Genera voz bajo demanda, después de que el texto ya se haya mostrado."""
    if waifu not in WAIFU_CONFIG:
        waifu = "nino"
    config = WAIFU_CONFIG[waifu]
    texto_audio = limpiar_texto(texto) or "..."
    texto_gemini = limpiar_texto_gemini(texto) or texto_audio
    voice = config.get("voice", "es-ES-ElviraNeural")
    rate = config.get("rate", "+0%")
    pitch = config.get("pitch", "+0Hz")
    try:
        user_tts_pref = str(user["tts_provider"] or "gemini").strip().lower()
    except Exception:
        user_tts_pref = "gemini"

    if user_tts_pref == "edge":
        try:
            return generar_audio_edge(texto_audio, voice=voice, rate=rate, pitch=pitch)
        except Exception as error:
            print(f"[Error TTS] Edge TTS falló: {error}")
            return None
    if user_tts_pref == "fish" and config.get("usa_fish", True):
        for fish_key in claves_en_orden(user, "fish"):
            try:
                resultado = generar_audio_fish(texto_audio, config.get("reference_id", "c961aaa2a71f469e98b8b2151b8c219d"), fish_key)
                CLAVE_PREFERIDA[(_u(user, "id", 0), "fish")] = fish_key
                return resultado
            except Exception as error:
                print(f"[Aviso TTS] Fish Audio falló con una clave: {error}")
    for voice_key in claves_voz_gemini(user):
        try:
            resultado = generar_audio_gemini(texto_gemini, voice_key, waifu=waifu)
            CLAVE_PREFERIDA[(_u(user, "id", 0), "voice_gemini")] = voice_key
            return resultado
        except Exception as error:
            print(f"[Aviso TTS] Gemini TTS falló con una clave: {error}")
    try:
        return generar_audio_edge(texto_audio, voice=voice, rate=rate, pitch=pitch)
    except Exception as error:
        print(f"[Error TTS] Edge TTS falló: {error}")
        return None


# --- ENDPOINTS DE LA API ---

@server_app.route("/api/register", method=["POST", "OPTIONS"])
def api_register():
    if request.method == "OPTIONS":
        return json_response({})

    # Supabase es obligatorio para el envío del email de confirmación
    if supabase_client is None:
        return json_response({"status": "error", "message": "El servidor no tiene Supabase configurado. Contacta con el administrador."}, 503)

    data = request_data()
    username, password = str(data.get("username", "")).strip(), str(data.get("password", ""))
    email = str(data.get("email", "")).strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return json_response({"status": "error", "message": "Introduce un correo electrónico válido."}, 400)
    if data.get("terms_version") != TERMS_VERSION:
        return json_response({"status": "error", "message": "Debes aceptar los términos y condiciones vigentes."}, 400)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        return json_response({"status": "error", "message": "El usuario debe tener entre 3 y 32 caracteres."}, 400)
    if len(password) < 8:
        return json_response({"status": "error", "message": "La contraseña debe tener al menos 8 caracteres."}, 400)

    # Comprobar duplicados en la BD local antes de llamar a Supabase
    try:
        with db_connection() as conn:
            if db_execute(conn, "SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone():
                return json_response({"status": "error", "message": "Ese nombre de usuario ya está en uso."}, 409)
            if db_execute(conn, "SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (email,)).fetchone():
                return json_response({"status": "error", "message": "Ese correo ya está asociado a una cuenta."}, 409)
    except Exception as db_err:
        print(f"[BD] Error al comprobar duplicados: {db_err}")
        return json_response({"status": "error", "message": "Error interno del servidor. Inténtalo de nuevo."}, 500)

    # Guardar primero los datos locales pendientes. Así no se envía un OTP que
    # luego no podamos asociar a la cuenta si falla la base de datos local.
    try:
        with db_connection() as conn:
            db_execute(conn, "DELETE FROM email_verifications WHERE email = ?", (email,))
            db_execute(conn,
                "INSERT INTO email_verifications (email, username, password_hash, code_hash, expires_at, terms_version) VALUES (?, ?, ?, ?, ?, ?)",
                (email, username, hash_password(password), "", time.time() + 86400, TERMS_VERSION))
    except Exception as db_err:
        print(f"[BD] Error al preparar la verificación de correo: {db_err}")
        return json_response({"status": "error", "message": "No se pudo preparar la cuenta. Inténtalo de nuevo."}, 500)

    # Registrar en Supabase Auth — Supabase envía el email de confirmación automáticamente
    try:
        sb_result = supabase_client.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {"username": username, "display_name": username, "full_name": username, "terms_version": TERMS_VERSION},
            },
        })
        sb_user = getattr(sb_result, "user", None)
        if not sb_user:
            raise RuntimeError("Supabase no devolvió un usuario al registrar.")
        print(f"[Supabase] Código de confirmación solicitado para: {email}")
    except Exception as sb_err:
        # No conservar datos pendientes si Supabase rechazó el registro.
        try:
            with db_connection() as conn:
                db_execute(conn, "DELETE FROM email_verifications WHERE email = ?", (email,))
        except Exception as cleanup_err:
            print(f"[BD] No se pudo limpiar el registro pendiente de {email}: {cleanup_err}")
        err_msg = str(sb_err).lower()
        if "already registered" in err_msg or "user already registered" in err_msg:
            return json_response({"status": "error", "message": "Ese correo ya está registrado. Inicia sesión o usa otro correo."}, 409)
        # El proveedor de correo integrado de Supabase solo permite enviar a
        # miembros del equipo del proyecto. En producción debe configurarse SMTP.
        if "email address not authorized" in err_msg or "email_address_not_authorized" in err_msg:
            print(f"[Supabase] Email de confirmación no autorizado por el proveedor integrado: {email}")
            return json_response({
                "status": "error",
                "message": "Supabase no permite enviar correos a esta dirección con el servicio integrado. Configura un proveedor SMTP propio en Supabase (Authentication → SMTP Settings) y vuelve a intentarlo."
            }, 503)
        if "over_email_send_rate_limit" in err_msg or "email rate limit exceeded" in err_msg:
            print(f"[Supabase] Límite de envío de correos alcanzado al registrar: {email}")
            return json_response({
                "status": "error",
                "message": "Supabase ha alcanzado el límite de envío de correos. Espera un poco o configura SMTP propio en Supabase para continuar."
            }, 429)
        print(f"[Supabase] Error al registrar: {sb_err}")
        return json_response({"status": "error", "message": "No se pudo enviar el correo de confirmación. Inténtalo de nuevo en unos minutos."}, 503)

    return json_response({
        "status": "verification_required",
        "message": "Te hemos enviado un código de 6 dígitos. Introdúcelo para activar tu cuenta.",
    }, 202)


@server_app.route("/api/verify-email", method=["POST", "OPTIONS"])
def api_verify_email():
    if request.method == "OPTIONS":
        return json_response({})
    if supabase_client is None:
        return json_response({"status": "error", "message": "Supabase no está configurado en el servidor."}, 503)
    data = request_data()
    email, code = str(data.get("email", "")).strip().lower(), str(data.get("code", "")).strip()
    if not re.fullmatch(r"\d{6}", code):
        return json_response({"status": "error", "message": "Introduce el código de 6 dígitos del correo."}, 400)
    with db_connection() as conn:
        pending = db_execute(conn, "SELECT * FROM email_verifications WHERE email = ?", (email,)).fetchone()
        if not pending or pending["expires_at"] < time.time():
            return json_response({"status": "error", "message": "No encontramos un registro pendiente vigente. Vuelve a crear la cuenta."}, 400)
        if pending["attempts"] >= 5:
            return json_response({"status": "error", "message": "Se agotaron los intentos. Vuelve a crear la cuenta."}, 429)

    # Verificar el código con Supabase Auth (el template Confirm signup debe
    # incluir {{ .Token }} y el proyecto debe estar configurado con OTP de 6 cifras).
    try:
        sb_response = supabase_client.auth.verify_otp({
            "email": email,
            "token": code,
            "type": "email",
        })
        sb_user = getattr(sb_response, "user", None)
        if not sb_user:
            raise RuntimeError("Supabase no devolvió el usuario tras validar el código.")
    except Exception as sb_err:
        print(f"[Supabase] Error al validar código de confirmación: {sb_err}")
        with db_connection() as conn:
            db_execute(conn, "UPDATE email_verifications SET attempts = attempts + 1 WHERE email = ?", (email,))
        return json_response({"status": "error", "message": "El código no es válido o ha caducado. Comprueba el correo e inténtalo de nuevo."}, 400)

    # El email queda verificado en Supabase; crear la sesión local de Omni-Wifu.
    with db_connection() as conn:
        pending = db_execute(conn, "SELECT * FROM email_verifications WHERE email = ?", (email,)).fetchone()
        if not pending:
            return json_response({"status": "error", "message": "El código se validó, pero no encontramos los datos pendientes de la cuenta. Contacta con soporte."}, 409)
        token = new_session_token()
        try:
            db_execute(conn, "INSERT INTO users (username, email, password_hash, session_token, email_verified, terms_accepted_at, terms_version, tts_provider) VALUES (?, ?, ?, ?, 1, ?, ?, 'gemini')",
                       (pending["username"], email, pending["password_hash"], token, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), pending["terms_version"]))
        except INTEGRITY_ERRORS:
            return json_response({"status": "error", "message": "El nombre o correo ya está asociado a una cuenta."}, 409)
        db_execute(conn, "DELETE FROM email_verifications WHERE email = ?", (email,))
        user = db_execute(conn, "SELECT * FROM users WHERE session_token = ?", (token,)).fetchone()
    set_session_cookie(token, persistent=data.get("remember_session") is True)
    print(f"[Supabase] Código de confirmación validado para: {email}")
    return json_response({"status": "ok", "profile": public_profile(user)}, 201)


@server_app.route("/api/login", method=["POST", "OPTIONS"])
def api_login():
    if request.method == "OPTIONS":
        return json_response({})
    data = request_data()
    username, password = str(data.get("username", "")).strip(), str(data.get("password", ""))
    if data.get("terms_version") != TERMS_VERSION:
        return json_response({"status": "error", "message": "Debes aceptar los términos y condiciones vigentes."}, 400)

    # 1. Buscar en la base de datos (PostgreSQL o local) por username o por email
    with db_connection() as conn:
        user = db_execute(conn,
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?) OR LOWER(email) = LOWER(?) LIMIT 1",
            (username, username)).fetchone()

    # Mapa de rescate para cuentas existentes creadas anteriormente en Supabase Auth
    MAPA_USUARIOS_INICIALES = {
        "ye-jin": "13061452@alu.murciaeduca.es",
        "aumaysonra": "maylexh10@gmail.com",
        "victor": "victormv901@gmail.com",
        "victorxd": "victorxd901@gmail.com",
        "angel": "angelflorea2012@gmail.com",
        "mladen": "mladenvalentinov@gmail.com",
        "rodica": "rodicastate1980@gmail.com",
    }

    # 2. Si el usuario no está aún en la tabla users:
    if not user:
        target_email = username.lower() if "@" in username else MAPA_USUARIOS_INICIALES.get(username.lower())
        if not target_email or supabase_client is None:
            return json_response({"status": "error", "message": "No encontramos esa cuenta. Verifica tu usuario o correo."}, 401)
        try:
            sb_response = supabase_client.auth.sign_in_with_password({
                "email": target_email,
                "password": password,
            })
            sb_session = getattr(sb_response, "session", None)
            sb_user = getattr(sb_response, "user", None)
            if not sb_session or not sb_user:
                return json_response({"status": "error", "message": "Usuario o contraseña incorrectos."}, 401)

            email = (getattr(sb_user, "email", "") or target_email).strip().lower()
            metadata = getattr(sb_user, "user_metadata", {}) or {}
            suggested_username = str(metadata.get("username") or metadata.get("display_name") or metadata.get("full_name") or username).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", suggested_username):
                suggested_username = username if re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username) else re.sub(r"[^A-Za-z0-9_.-]", "_", email.split("@", 1)[0])[:32]
            if len(suggested_username) < 3:
                suggested_username = "usuario"

            token = new_session_token()
            now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with db_connection() as conn:
                user = db_execute(conn, "SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,)).fetchone()
                if not user:
                    db_execute(conn,
                        "INSERT INTO users (username, email, password_hash, session_token, email_verified, terms_accepted_at, terms_version, tts_provider) VALUES (?, ?, ?, ?, 1, ?, ?, 'gemini')",
                        (suggested_username, email, hash_password(password), token, now_str, TERMS_VERSION))
                    user = db_execute(conn, "SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,)).fetchone()
                else:
                    db_execute(conn, "UPDATE users SET session_token = ?, terms_accepted_at = ?, terms_version = ? WHERE id = ?",
                               (token, now_str, TERMS_VERSION, user["id"]))
                    user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

            if not user:
                return json_response({"status": "error", "message": "No se pudo preparar tu perfil. Inténtalo de nuevo."}, 500)
            set_session_cookie(user["session_token"], persistent=data.get("remember_session") is True)
            print(f"[Supabase] Perfil sincronizado y guardado en Supabase: {email} (usuario: {user['username']})")
            return json_response({"status": "ok", "profile": public_profile(user), "supabase_access_token": sb_session.access_token})
        except Exception as sb_err:
            print(f"[Supabase] Error en login para {username}: {sb_err}")
            return json_response({"status": "error", "message": "Usuario o contraseña incorrectos."}, 401)

    # 3. Si el usuario ya está en la tabla users:
    supabase_access_token = None
    pass_ok = verify_password(password, user["password_hash"]) if user["password_hash"] else False

    # Si el hash local no coincide, validar con Supabase Auth si tiene email
    if not pass_ok:
        if supabase_client is None or not user["email"]:
            return json_response({"status": "error", "message": "Usuario o contraseña incorrectos."}, 401)
        try:
            sb_response = supabase_client.auth.sign_in_with_password({
                "email": user["email"],
                "password": password,
            })
            sb_session = getattr(sb_response, "session", None)
            if not sb_session:
                return json_response({"status": "error", "message": "Usuario o contraseña incorrectos."}, 401)
            supabase_access_token = sb_session.access_token
            pass_ok = True
            with db_connection() as conn:
                db_execute(conn, "UPDATE users SET password_hash = ?, email_verified = 1 WHERE id = ?",
                           (hash_password(password), user["id"]))
                user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            print(f"[Supabase] Hash actualizado para: {user['email']}")
        except Exception as sb_err:
            print(f"[Supabase] Validación fallida para {user['email']}: {sb_err}")
            return json_response({"status": "error", "message": "Usuario o contraseña incorrectos."}, 401)

    token = new_session_token()
    with db_connection() as conn:
        db_execute(conn, "UPDATE users SET session_token = ?, terms_accepted_at = ?, terms_version = ? WHERE id = ?",
                   (token, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), TERMS_VERSION, user["id"]))
        user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

    set_session_cookie(token, persistent=data.get("remember_session") is True)

    # Inicio de sesión paralelo en Supabase Auth si aún no se tiene token
    if supabase_client is not None and not supabase_access_token and user["email"]:
        try:
            sb_response = supabase_client.auth.sign_in_with_password({
                "email": user["email"],
                "password": password,
            })
            if sb_response and getattr(sb_response, "session", None):
                supabase_access_token = sb_response.session.access_token
        except Exception:
            pass

    result = {"status": "ok", "profile": public_profile(user)}
    if supabase_access_token:
        result["supabase_access_token"] = supabase_access_token
    return json_response(result)

@server_app.route("/api/supabase-confirm", method=["POST", "OPTIONS"])
def api_supabase_confirm():
    """
    Endpoint llamado por el frontend cuando detecta un #access_token de Supabase
    en la URL (enlace de confirmación de correo).

    Flujo:
    1. Verifica el token con supabase_client.auth.get_user().
    2. Busca al usuario en la BD local por email.
    3. Si existe → emite cookie de sesión local y devuelve el perfil.
    4. Si no existe → crea una cuenta mínima con los datos de Supabase y luego
       emite la cookie y devuelve el perfil.
    5. Si Supabase no está configurado → devuelve 501.
    """
    if request.method == "OPTIONS":
        return json_response({})

    if supabase_client is None:
        return json_response({"status": "error", "message": "Supabase no está configurado en el servidor."}, 501)

    data = request_data()
    access_token = str(data.get("access_token", "")).strip()
    if not access_token:
        return json_response({"status": "error", "message": "Token de acceso no proporcionado."}, 400)

    # 1. Verificar el token con Supabase
    try:
        sb_user_response = supabase_client.auth.get_user(access_token)
        sb_user = getattr(sb_user_response, "user", None)
        if not sb_user:
            raise ValueError("Respuesta vacía de Supabase")
    except Exception as sb_err:
        print(f"[Supabase] Token inválido en /api/supabase-confirm: {sb_err}")
        return json_response({"status": "error", "message": "El enlace de confirmación no es válido o ya caducó."}, 401)

    email = (getattr(sb_user, "email", "") or "").strip().lower()
    if not email:
        return json_response({"status": "error", "message": "No se pudo obtener el correo del token de Supabase."}, 400)

    # Intentar leer el username guardado en user_metadata (lo enviamos al registrar)
    meta = getattr(sb_user, "user_metadata", {}) or {}
    suggested_username = str(meta.get("username", "")).strip()

    with db_connection() as conn:
        user = db_execute(conn, "SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,)).fetchone()

        if not user:
            # 4. Crear cuenta local mínima si no existía
            # Generar un username único a partir de la parte local del email si no hay metadata
            base_username = suggested_username or re.sub(r"[^A-Za-z0-9_.-]", "_", email.split("@")[0])[:32] or "usuario"
            # Asegurarse de que el username no colisiona
            candidate = base_username[:32]
            suffix = 1
            while db_execute(conn, "SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (candidate,)).fetchone():
                candidate = f"{base_username[:28]}_{suffix}"
                suffix += 1
            # Contraseña aleatoria segura (el usuario nunca la verá; puede cambiarla si quiere)
            random_password_hash = hash_password(secrets.token_urlsafe(32))
            token_local = new_session_token()
            now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                db_execute(
                    conn,
                    "INSERT INTO users (username, email, password_hash, session_token, email_verified, "
                    "terms_accepted_at, terms_version, tts_provider) VALUES (?, ?, ?, ?, 1, ?, ?, 'gemini')",
                    (candidate, email, random_password_hash, token_local, now_str, TERMS_VERSION),
                )
            except INTEGRITY_ERRORS:
                return json_response({"status": "error", "message": "Ya existe una cuenta con ese correo."}, 409)
            user = db_execute(conn, "SELECT * FROM users WHERE session_token = ?", (token_local,)).fetchone()
        else:
            # 3. Usuario ya existe → actualizar sesión
            token_local = new_session_token()
            now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            db_execute(
                conn,
                "UPDATE users SET session_token = ?, terms_accepted_at = ?, terms_version = ?, "
                "email_verified = 1 WHERE id = ?",
                (token_local, now_str, TERMS_VERSION, user["id"]),
            )
            user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

    set_session_cookie(token_local, persistent=data.get("remember_session") is True)
    print(f"[Supabase] Confirmación exitosa para: {email}")
    return json_response({"status": "ok", "profile": public_profile(user)}, 200)


@server_app.route("/api/logout", method=["POST", "OPTIONS"])
def api_logout():
    if request.method == "OPTIONS":
        return json_response({})
    token = request.get_cookie(SESSION_COOKIE)
    if token:
        with db_connection() as conn:
            db_execute(conn, "UPDATE users SET session_token = NULL WHERE session_token = ?", (token,))
    clear_session_cookie()
    return json_response({"status": "ok"})


@server_app.route("/api/session-preference", method=["POST", "OPTIONS"])
def api_session_preference():
    if request.method == "OPTIONS":
        return json_response({})
    user = current_user()
    if not user:
        return json_response({"status": "error", "message": "Sesión no válida."}, 401)
    data = request_data()
    remember_session = data.get("remember_session")
    if not isinstance(remember_session, bool):
        return json_response({"status": "error", "message": "Preferencia de sesión no válida."}, 400)
    set_session_cookie(user["session_token"], persistent=remember_session)
    return json_response({"status": "ok"})


@server_app.route("/api/models", method=["GET", "OPTIONS"])
def api_models():
    """Catálogo de proveedores y modelos de conversación para construir el selector en el frontend."""
    if request.method == "OPTIONS":
        return json_response({})
    providers = [
        {"id": pid, "label": cfg["label"], "models": list(cfg["models"]), "needs_key": pid != "gemini"}
        for pid, cfg in CHAT_PROVIDERS.items()
    ]
    return json_response({"status": "ok", "providers": providers})


def limpiar_lista_claves(valor):
    if not isinstance(valor, list):
        return [""]
    limpia = [str(x or "").strip()[:400] for x in valor][:MAX_KEY_SLOTS]
    return limpia or [""]


@server_app.route("/api/profile", method=["GET", "PUT", "POST", "OPTIONS"])
def api_profile():
    if request.method == "OPTIONS":
        return json_response({})
    user = require_user()
    if not user:
        return json_response({"status": "error", "message": "Sesión no válida."}, 401)
    if request.method == "GET":
        return json_response({"status": "ok", "profile": public_profile(user)})
    data = request_data()

    tts_provider = str(data.get("tts_provider", _u(user, "tts_provider", "gemini"))).strip().lower()
    if tts_provider not in ("gemini", "fish", "edge"):
        tts_provider = "gemini"
    chat_provider = str(data.get("chat_provider", _u(user, "chat_provider", "gemini"))).strip().lower()
    if chat_provider not in CHAT_PROVIDERS:
        chat_provider = "gemini"
    chat_model = str(data.get("chat_model", _u(user, "chat_model", ""))).strip()
    if not re.fullmatch(r"[A-Za-z0-9._:/\-]{0,80}", chat_model):
        chat_model = ""

    # Listas de claves: cualquier número de slots por cada sitio donde se pone una API
    almacen = claves_proveedor(user)
    nuevas = data.get("key_lists")
    if isinstance(nuevas, dict):
        for nombre in ("gemini", "voice_gemini", "fish") + PROVIDERS_CON_CLAVE:
            if nombre in nuevas:
                almacen[nombre] = limpiar_lista_claves(nuevas[nombre])
    else:
        # Cliente antiguo (cinco claves fijas): se descarta el formato nuevo para que no quede desfasado
        for nombre in ("gemini", "voice_gemini", "fish"):
            almacen.pop(nombre, None)
        try:
            almacen["gemini_active"] = int(data.get("active_gemini_slot", _u(user, "active_gemini_slot", 1)))
        except (TypeError, ValueError):
            pass
        antiguas = data.get("gemini_keys")
        if isinstance(antiguas, list) and len(antiguas) == 5:
            almacen["gemini"] = limpiar_lista_claves(antiguas)
        if "fish_key" in data:
            almacen["fish"] = [str(data.get("fish_key") or "").strip()]
        if "gemini_voice_key" in data:
            almacen["voice_gemini"] = [str(data.get("gemini_voice_key") or "").strip()]
        for pid, valor in (data.get("provider_keys") or {}).items() if isinstance(data.get("provider_keys"), dict) else []:
            if pid in PROVIDERS_CON_CLAVE:
                almacen[pid] = [str(valor or "").strip()]
    gemini_list = limpiar_lista_claves(almacen.get("gemini", lista_claves(user, "gemini")))
    try:
        activo = int(data.get("gemini_active", data.get("active_gemini_slot", almacen.get("gemini_active") or _u(user, "active_gemini_slot", 1))))
    except (TypeError, ValueError):
        activo = 1
    activo = max(1, min(activo, len(gemini_list)))
    almacen["gemini"] = gemini_list
    almacen["gemini_active"] = activo

    # Columnas antiguas: se mantienen sincronizadas (primeros cinco slots) por compatibilidad
    columnas_gemini = (gemini_list + [""] * 5)[:5]
    primera = lambda lista: next((k for k in lista if k), "")
    fish_key = primera(limpiar_lista_claves(almacen.get("fish", lista_claves(user, "fish"))))
    voz_key = primera(limpiar_lista_claves(almacen.get("voice_gemini", lista_claves(user, "voice_gemini"))))
    with db_connection() as conn:
        db_execute(
            conn,
            """UPDATE users SET tts_provider = ?, fish_key = ?, active_gemini_slot = ?, gemini_key_1 = ?,
               gemini_key_2 = ?, gemini_key_3 = ?, gemini_key_4 = ?, gemini_key_5 = ?,
               chat_provider = ?, chat_model = ?, provider_keys = ?, gemini_voice_key = ? WHERE id = ?""",
            (tts_provider, fish_key, min(activo, 5), *columnas_gemini,
             chat_provider, chat_model, json.dumps(almacen), voz_key, user["id"]),
        )
        updated_user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    for chat_key in list(chats):
        if chat_key[0] == user["id"]:
            del chats[chat_key]
    for hist_key in list(histories):
        if hist_key[0] == user["id"]:
            del histories[hist_key]
    for pref_key in list(CLAVE_PREFERIDA):
        if pref_key[0] == user["id"]:
            del CLAVE_PREFERIDA[pref_key]
    return json_response({"status": "ok", "profile": public_profile(updated_user)})


# --- NOMBRE DE USUARIO Y AVATAR ---

# Límites del nombre: 3-20 caracteres, empieza y acaba en letra o número; letras, números, "_", "." y "-".
USERNAME_CHANGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,18}[A-Za-z0-9]")
# Para que no sature ni se abuse, solo se puede cambiar una vez cada 7 días (se ajusta con USERNAME_COOLDOWN_HOURS; 0 = sin espera).
try:
    USERNAME_COOLDOWN_SECONDS = max(0, int(float(os.environ.get("USERNAME_COOLDOWN_HOURS", "168")) * 3600))
except ValueError:
    USERNAME_COOLDOWN_SECONDS = 168 * 3600
RESERVED_USERNAMES = {
    "admin", "administrador", "administrator", "root", "soporte", "support", "moderador", "moderator", "mod",
    "omni", "omniwifu", "omniwaifu", "nino", "ninoia", "miku", "hatsunemiku", "luna", "mia", "claude", "anthropic",
    "gemini", "system", "sistema", "staff", "null", "undefined", "api", "www", "ayuda", "help", "owner",
}
# El avatar solo cambia de color y de forma: valores cerrados, nada que subir ni que guardar en grande.
AVATAR_COLORS = ("rosa", "melocoton", "limon", "menta", "cielo", "violeta")
AVATAR_SHAPES = ("circulo", "suave", "cuadrado", "hoja")


def _espera_legible(segundos):
    if segundos >= 86400:
        dias = -(-segundos // 86400)
        return f"{dias} día{'s' if dias != 1 else ''}"
    horas = max(1, -(-segundos // 3600))
    return f"{horas} hora{'s' if horas != 1 else ''}"


@server_app.route("/api/account", method=["POST", "PUT", "OPTIONS"])
def api_account():
    """Cambia el nombre de usuario y/o el color y la forma del avatar de la cuenta."""
    if request.method == "OPTIONS":
        return json_response({})
    user = require_user()
    if not user:
        return json_response({"status": "error", "message": "Sesión no válida."}, 401)
    data = request_data()
    cambios_avatar = None
    nuevo_nombre = None

    if "avatar_style" in data:
        partes = str(data.get("avatar_style") or "").strip().lower().split(":")
        if len(partes) != 2 or partes[0] not in AVATAR_COLORS or partes[1] not in AVATAR_SHAPES:
            return json_response({"status": "error", "message": "Ese avatar no es válido."}, 400)
        cambios_avatar = f"{partes[0]}:{partes[1]}"

    if "username" in data:
        nuevo_nombre = str(data.get("username") or "").strip()
        if nuevo_nombre == user["username"]:
            nuevo_nombre = None  # sin cambios: no se gasta la espera
        else:
            if not USERNAME_CHANGE_RE.fullmatch(nuevo_nombre):
                return json_response({"status": "error", "message": "El nombre debe tener entre 3 y 20 caracteres (letras, números, _ . -) y empezar y acabar en letra o número."}, 400)
            if re.sub(r"[_.\-]", "", nuevo_nombre.lower()) in RESERVED_USERNAMES:
                return json_response({"status": "error", "message": "Ese nombre está reservado. Elige otro."}, 400)
            cambiado = float(_u(user, "username_changed_at", 0) or 0)
            restante = int(cambiado + USERNAME_COOLDOWN_SECONDS - time.time()) if cambiado else 0
            if restante > 0 and nuevo_nombre.lower() != user["username"].lower():
                return json_response({"status": "error", "message": f"Podrás volver a cambiar tu nombre en {_espera_legible(restante)}."}, 429)

    try:
        with db_connection() as conn:
            if nuevo_nombre:
                if db_execute(conn, "SELECT id FROM users WHERE LOWER(username) = LOWER(?) AND id <> ?", (nuevo_nombre, user["id"])).fetchone():
                    return json_response({"status": "error", "message": "Ese nombre de usuario ya está en uso."}, 409)
                if db_execute(conn, "SELECT 1 FROM email_verifications WHERE LOWER(username) = LOWER(?) AND expires_at > ?", (nuevo_nombre, time.time())).fetchone():
                    return json_response({"status": "error", "message": "Ese nombre de usuario ya está en uso."}, 409)
                db_execute(conn, "UPDATE users SET username = ?, username_changed_at = ? WHERE id = ?", (nuevo_nombre, time.time(), user["id"]))
            if cambios_avatar is not None:
                db_execute(conn, "UPDATE users SET avatar_style = ? WHERE id = ?", (cambios_avatar, user["id"]))
            actualizado = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    except Exception as error_cuenta:
        texto_error = str(error_cuenta).lower()
        if "unique" in texto_error or "duplicate" in texto_error:
            return json_response({"status": "error", "message": "Ese nombre de usuario ya está en uso."}, 409)
        print(f"[Cuenta] No se pudo actualizar la cuenta: {str(error_cuenta)[:200]}")
        return json_response({"status": "error", "message": "No se pudo guardar el cambio. Inténtalo de nuevo."}, 500)
    return json_response({"status": "ok", "profile": public_profile(actualizado)})


# --- BORRADO Y EXPORTACIÓN DE LA CUENTA (derechos de supresión y portabilidad) ---

def _borrar_usuario_supabase_auth(email):
    """Intenta borrar también la identidad de verificación en Supabase Auth.
    Solo funciona si SUPABASE_KEY es la clave service_role; con la clave anon falla en silencio y se deja constancia en el log."""
    if supabase_client is None or not email:
        return False
    try:
        usuarios = supabase_client.auth.admin.list_users()
        if isinstance(usuarios, dict):
            usuarios = usuarios.get("users", [])
        for u in usuarios or []:
            correo = getattr(u, "email", None) or (u.get("email") if isinstance(u, dict) else None)
            if correo and str(correo).lower() == email.lower():
                uid = getattr(u, "id", None) or (u.get("id") if isinstance(u, dict) else None)
                supabase_client.auth.admin.delete_user(uid)
                return True
    except Exception as error_sb:
        print(f"[Cuenta] No se pudo borrar el usuario en Supabase Auth: {str(error_sb)[:160]}")
    return False


@server_app.route("/api/account/delete", method=["POST", "OPTIONS"])
def api_account_delete():
    """Elimina la cuenta y sus datos. Hay que escribir el nombre de usuario para confirmar."""
    if request.method == "OPTIONS":
        return json_response({})
    user = current_user()
    if not user:
        return json_response({"status": "error", "message": "Sesión no válida."}, 401)
    data = request_data()
    if str(data.get("confirm") or "").strip().lower() != str(user["username"]).lower():
        return json_response({"status": "error", "message": "Escribe tu nombre de usuario exacto para confirmar."}, 400)
    email = str(_u(user, "email", "") or "")
    try:
        with db_connection() as conn:
            db_execute(conn, "DELETE FROM users WHERE id = ?", (user["id"],))
            if email:
                db_execute(conn, "DELETE FROM email_verifications WHERE LOWER(email) = LOWER(?)", (email,))
    except Exception as error_borrado:
        print(f"[Cuenta] No se pudo eliminar la cuenta: {str(error_borrado)[:200]}")
        return json_response({"status": "error", "message": "No se pudo eliminar la cuenta. Inténtalo de nuevo."}, 500)
    for clave_historial in [k for k in list(histories.keys()) if k and k[0] == user["id"]]:
        histories.pop(clave_historial, None)
    _borrar_usuario_supabase_auth(email)
    clear_session_cookie()
    return json_response({"status": "ok"})


@server_app.route("/api/account/export", method=["GET", "OPTIONS"])
def api_account_export():
    """Devuelve en JSON los datos de la cuenta (sin contraseña ni valores de las claves API)."""
    if request.method == "OPTIONS":
        return json_response({})
    user = current_user()
    if not user:
        return json_response({"status": "error", "message": "Sesión no válida."}, 401)
    proveedores_con_clave = []
    try:
        guardadas = json.loads(_u(user, "provider_keys", "{}") or "{}")
        proveedores_con_clave = sorted(n for n, lista in guardadas.items() if isinstance(lista, list) and any(str(k).strip() for k in lista))
    except Exception:
        pass
    datos = {
        "usuario": user["username"],
        "correo": _u(user, "email", ""),
        "correo_verificado": bool(_u(user, "email_verified", 0)),
        "terminos_aceptados_en": _u(user, "terms_accepted_at", ""),
        "version_terminos": _u(user, "terms_version", ""),
        "avatar": _u(user, "avatar_style", "") or "rosa:suave",
        "proveedor_de_voz": _u(user, "tts_provider", ""),
        "proveedor_de_chat": _u(user, "chat_provider", ""),
        "modelo_de_chat": _u(user, "chat_model", ""),
        "claves_api": {
            "nota": "Por seguridad no se incluyen los valores de tus claves, solo qué proveedores tienen alguna guardada.",
            "gemini_guardadas": sum(1 for k in lista_claves(user, "gemini") if k),
            "fish_audio_guardada": bool(_u(user, "fish_key", "")),
            "otros_proveedores": proveedores_con_clave,
        },
        "conversaciones": "Las conversaciones solo se guardan en la memoria temporal del servidor (hasta 40 mensajes por personaje) y no se almacenan en la base de datos.",
        "exportado_en": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    response.content_type = "application/json; charset=UTF-8"
    response.set_header("Content-Disposition", 'attachment; filename="omni-wifu-mis-datos.json"')
    return json.dumps(datos, ensure_ascii=False, indent=2)


@server_app.route("/api/chat", method=["POST", "OPTIONS"])
def api_chat():
    if request.method == "OPTIONS":
        return json_response({})
    user = require_user()
    if not user:
        return json_response({"status": "error", "message": "Tu sesión ha caducado. Inicia sesión de nuevo."}, 401)
    data = request_data()
    mensaje = str(data.get("text", "")).strip()
    waifu = str(data.get("waifu", "nino")).strip()
    if not mensaje:
        return json_response({"status": "error", "message": "Mensaje vacío"}, 400)
    modo = str(data.get("mode", "conversacion")).strip().lower()
    if modo not in ("conversacion", "pregunta", "automatico"):
        modo = "conversacion"
    # {"lat": .., "lon": .., "tz": "Europe/Madrid"}; todo opcional. No se guarda en la base de datos.
    location = data.get("location") if isinstance(data.get("location"), dict) else None
    respuesta, _ = procesar_gemini(mensaje, user, waifu, modo=modo, generar_voz=False, location=location)
    speech_job_id = iniciar_generacion_audio(respuesta, user, waifu)
    return json_response({"status": "ok", "user_text": mensaje, "nino_text": respuesta, "speech_job_id": speech_job_id})


@server_app.route("/api/speech/<job_id>", method="GET")
def api_speech(job_id):
    user = require_user()
    if not user:
        return json_response({"status": "error", "message": "Tu sesión ha caducado. Inicia sesión de nuevo."}, 401)
    with speech_jobs_lock:
        job = speech_jobs.get(job_id)
        if not job or job["user_id"] != user["id"]:
            return json_response({"status": "error", "message": "Audio no disponible."}, 404)
        event = job.get("event")
        pending = job["status"] == "pending"
    # Espera larga: la respuesta sale en cuanto el audio está listo, sin esperar al siguiente sondeo.
    if pending and event is not None:
        event.wait(timeout=20)
    with speech_jobs_lock:
        job = speech_jobs.get(job_id)
        if not job:
            return json_response({"status": "error", "message": "Audio no disponible."}, 404)
        if job["status"] == "pending":
            return json_response({"status": "pending"})
        audio_url = job["audio_url"]
        speech_jobs.pop(job_id, None)
    return json_response({"status": "ok", "audio_url": audio_url})


@server_app.route("/api/record", method=["POST", "OPTIONS"])
def api_record():
    """
    Ruta de compatibilidad para evitar errores si el navegador invoca el fallback de grabación.
    El audio por hardware local ha sido desacoplado para compatibilidad con la nube (Render).
    """
    if request.method == "OPTIONS":
        return json_response({})
    return json_response({
        "status": "error",
        "message": "La grabación directa por hardware del servidor no está disponible en la nube. Utiliza el micrófono en el navegador (Web Speech API) o envía texto por el chat."
    }, 400)


# --- RUTAS ESTÁTICAS Y SERVIDAS ---

SITE_URL = os.environ.get("SITE_URL", "https://omni-wifu.onrender.com").rstrip("/")


@server_app.route("/robots.txt")
def robots_txt():
    """Indica a los buscadores qué pueden rastrear y dónde está el sitemap."""
    response.content_type = "text/plain; charset=utf-8"
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /audio_cache/\n"
        f"Sitemap: {SITE_URL}/sitemap.xml\n"
    )


@server_app.route("/sitemap.xml")
def sitemap_xml():
    """Sitemap mínimo para Google Search Console."""
    response.content_type = "application/xml; charset=utf-8"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{SITE_URL}/</loc></url>"
        "</urlset>"
    )


@server_app.route("/")
def index():
    """Carga la interfaz principal avatar.html en la raíz del servidor."""
    response.set_header("Cache-Control", "no-cache, no-store, must-revalidate")
    response.set_header("Pragma", "no-cache")
    response.set_header("Expires", "0")
    return static_file("avatar.html", root=BASE_DIR)


GREETING_DIR = os.path.join(AUDIO_DIR, "greetings")
os.makedirs(GREETING_DIR, exist_ok=True)
greeting_lock = Lock()


@server_app.route("/audio_cache/greetings/<filename>")
def servir_saludo(filename):
    """Saludos de bienvenida ya generados (no caducan como el resto de la caché de audio)."""
    return static_file(filename, root=GREETING_DIR)


def motor_coincide(preferencia, nombre_archivo):
    """True si el audio salió del motor que el usuario eligió (y no de un respaldo)."""
    if preferencia == "gemini":
        return nombre_archivo.startswith("voice_gemini_")
    if preferencia == "fish":
        return nombre_archivo.startswith("voice_fish_")
    return not nombre_archivo.startswith(("voice_gemini_", "voice_fish_"))


@server_app.route("/api/greet", method=["POST", "OPTIONS"])
def api_greet():
    """
    Voz del saludo inicial del personaje. Se guarda para reutilizarlo y no gastar cuota de TTS en cada visita,
    pero solo si lo generó el motor elegido (un audio de respaldo no se conserva).
    """
    if request.method == "OPTIONS":
        return json_response({})
    user = require_user()
    if not user:
        return json_response({"status": "error", "message": "Sesión no válida."}, 401)
    data = request_data()
    waifu = str(data.get("waifu", "nino")).strip()
    if waifu not in WAIFU_CONFIG:
        waifu = "nino"
    texto = str(data.get("text", "")).strip()[:200]
    if not texto:
        return json_response({"status": "error", "message": "Texto vacío"}, 400)
    config = WAIFU_CONFIG[waifu]
    preferencia = str(_u(user, "tts_provider", "gemini") or "gemini").strip().lower()
    if preferencia == "fish" and not config.get("usa_fish", True):
        preferencia = "gemini"
    identidad = {"gemini": config.get("gemini_voice", ""), "fish": config.get("reference_id", ""),
                 "edge": f"{config.get('voice', '')}{config.get('rate', '')}{config.get('pitch', '')}"}.get(preferencia, "")
    huella = hashlib.sha1(f"{preferencia}|{waifu}|{texto}|{identidad}".encode("utf-8")).hexdigest()[:16]
    nombre = f"saludo_{huella}.mp3"
    if os.path.isfile(os.path.join(GREETING_DIR, nombre)):
        return json_response({"status": "ok", "audio_url": f"/audio_cache/greetings/{nombre}"})

    def guardar(audio_url):
        origen = os.path.join(AUDIO_DIR, os.path.basename(audio_url))
        if motor_coincide(preferencia, os.path.basename(audio_url)) and os.path.isfile(origen):
            with greeting_lock:
                shutil.copyfile(origen, os.path.join(GREETING_DIR, nombre))
            return f"/audio_cache/greetings/{nombre}"
        return audio_url

    job_id = iniciar_generacion_audio(texto, user, waifu, postproceso=guardar)
    return json_response({"status": "ok", "speech_job_id": job_id})


@server_app.route("/audio_cache/<filename>")
def servir_audio(filename):
    """Sirve los archivos de voz generados en la caché de audio."""
    return static_file(filename, root=AUDIO_DIR)


@server_app.route("/<filename:path>")
def send_static(filename):
    """Sirve recursos estáticos (modelos VRM, texturas, etc.) protegiendo archivos sensibles."""
    nombre = filename.replace("\\", "/").lower()
    partes = [parte for parte in nombre.split("/") if parte]
    carpetas_privadas = {"venv", "__pycache__", "node_modules", "audio_cache"}
    extensiones_privadas = (".db", ".py", ".bat", ".env", ".txt", ".pth", ".example", ".md", ".sqlite", ".sqlite3", ".log")
    if (
        any(parte.startswith(".") for parte in partes)
        or (partes and partes[0] in carpetas_privadas)
        or nombre.endswith(extensiones_privadas)
        or nombre == "main.py"
    ):
        raise HTTPError(403, "Access denied.")
    resultado = static_file(filename, root=BASE_DIR)
    if filename.lower().endswith((".vrm", ".glb", ".gltf", ".png", ".jpg", ".jpeg", ".webp", ".woff2", ".mp3")):
        try:
            resultado.set_header("Cache-Control", "public, max-age=86400")
        except Exception:
            pass
    return resultado


# --- PUNTO DE ENTRADA DEL SERVIDOR ---

class ThreadedWSGIRefServer(ServerAdapter):
    """Servidor con un hilo por petición. El de Bottle por defecto atiende de una en una: mientras generaba
    una respuesta o descargaba un modelo 3D, todo lo demás (audio incluido) se quedaba esperando."""

    def run(self, handler):
        from socketserver import ThreadingMixIn
        from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

        class ThreadedServer(ThreadingMixIn, WSGIServer):
            daemon_threads = True
            request_queue_size = 64

        make_server(self.host, self.port, handler, server_class=ThreadedServer,
                    handler_class=WSGIRequestHandler).serve_forever()



if __name__ == "__main__":
    import sys
    if "--check-db" in sys.argv:
        print("Comprobando la base de datos configurada...")
        if not DATABASE_URL:
            print("DATABASE_URL no está definida: se usaría users.db local.")
        else:
            try:
                prueba = _pg_connect()
                cur = prueba.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM users")
                fila = cur.fetchone()
                print("CONEXIÓN OK con PostgreSQL. Cuentas en la tabla users:", fila["n"] if hasattr(fila, "keys") else fila[0])
                prueba.close()
            except Exception as error_prueba:
                print("FALLO:", str(error_prueba)[:300])
                print("Causa probable:", explicar_error_postgres(error_prueba))
        sys.exit(0)
    port = int(os.environ.get("PORT", 8080))
    print(f"Iniciando servidor Omni-Wifu en http://0.0.0.0:{port}...")
    server_app.run(host="0.0.0.0", port=port, server=ThreadedWSGIRefServer)

