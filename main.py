import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import time
import uuid
from threading import Lock, Thread
from email.message import EmailMessage
from contextlib import contextmanager

import edge_tts
import requests
from bottle import Bottle, HTTPError, request, response, static_file
from google import genai
from google.genai import types

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
DATABASE_URL = _raw_db_url

# Modo PostgreSQL si existe DATABASE_URL y algún driver disponible
USING_POSTGRES = bool(DATABASE_URL) and (psycopg is not None or psycopg2 is not None)

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
        print("[Supabase] Paquete 'supabase' no instalado — Auth de Supabase desactivado.")
    elif not (_supabase_url and _supabase_key):
        print("[Supabase] SUPABASE_URL / SUPABASE_KEY no configuradas — Auth de Supabase desactivado.")

SESSION_COOKIE = "nino_session"
TERMS_VERSION = "2026-10-05"
AVAILABLE_MODELS = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.8-flash"]

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
            "y guiones para titubeos tsundere. Habla de forma directa y fluida, tal como hablaría Nino en un doblaje al español."
        ),
        "tts_provider": "fish_audio",
        "reference_id": "c961aaa2a71f469e98b8b2151b8c219d",
        "voice": "es-ES-ElviraNeural",
        "rate": "+10%",
        "pitch": "+4Hz",
        "gemini_voice": "Aoede",
        "gemini_tone_instruction": (
            "Eres la voz oficial de Nino Nakano. Vocaliza este texto en español con actitud femenina "
            "auténticamente tsundere: enérgica, altiva, orgullosa, con carácter fuerte pero con afecto oculto "
            "y viva expresividad emocional. Pronuncia únicamente el texto indicado con máxima naturalidad."
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
            "pueda sintetizar de forma clara y limpia (ejemplos: '¡Ah!', '¡Genial!', 'Umm...', '¡Sí!', '¡Vamos!').\\n"
            "3. PUNTUACIÓN: Utiliza puntos suspensivos (...) para pausas reflexivas y puntuación clara en español."
        ),
        "tts_provider": "edge_tts",
        "voice": "es-ES-ElviraNeural",
        "rate": "+15%",
        "pitch": "+6Hz",
        "gemini_voice": "Kore",
        "gemini_tone_instruction": (
            "Eres la voz oficial de Hatsune Miku. Vocaliza este texto en español con actitud femenina "
            "alegre, enérgica, expresiva y melodiosa. Pronuncia únicamente el texto indicado con "
            "vitalidad y calidez musical."
        ),
    },
}

clients = {}
chats = {}
speech_jobs = {}
speech_jobs_lock = Lock()


# --- GESTIÓN DE BASE DE DATOS DINÁMICA ---

@contextmanager
def db_connection():
    """
    Gestiona la conexión dinámica:
    - PostgreSQL en Render si DATABASE_URL está definida y el driver está disponible.
    - SQLite local (users.db) en entornos locales o si no hay driver de PostgreSQL.
    """
    conn = None
    try:
        if USING_POSTGRES:
            if psycopg is not None:
                conn = psycopg.connect(DATABASE_URL, row_factory=psycopg3_dict_row)
            elif psycopg2 is not None:
                conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            conn.autocommit = True
        else:
            conn = sqlite3.connect(DATABASE_PATH)
            conn.row_factory = sqlite3.Row
        yield conn
        if not USING_POSTGRES and conn:
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
                gemini_key_5 TEXT NOT NULL DEFAULT ''
            )
        """)
        # Migración automática si la tabla ya existía sin la columna tts_provider
        try:
            db_execute(conn, "ALTER TABLE users ADD COLUMN tts_provider TEXT NOT NULL DEFAULT 'gemini'")
        except Exception:
            pass
        for column, definition in (("email", "TEXT NOT NULL DEFAULT ''"), ("email_verified", "INTEGER NOT NULL DEFAULT 0"),
                                   ("terms_accepted_at", "TEXT"), ("terms_version", "TEXT")):
            try:
                db_execute(conn, f"ALTER TABLE users ADD COLUMN {column} {definition}")
            except Exception:
                pass
        db_execute(conn, "CREATE TABLE IF NOT EXISTS email_verifications (email TEXT PRIMARY KEY, username TEXT NOT NULL, password_hash TEXT NOT NULL, code_hash TEXT NOT NULL, expires_at REAL NOT NULL, terms_version TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)")

        db_execute(conn, "CREATE INDEX IF NOT EXISTS idx_users_session ON users(session_token)")
        db_execute(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_lower ON users (LOWER(username))")
        db_execute(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email)) WHERE email <> ''")


# Alias por compatibilidad e inicialización al cargar el servidor
init_database = init_db
init_db()


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


def set_session_cookie(token):
    response.set_cookie(SESSION_COOKIE, token, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="Lax", secure=False, path="/")


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
    msg["Subject"] = "Tu código para crear una cuenta en Nino AI"
    msg["From"] = sender
    msg["To"] = email
    msg.set_content(f"Tu código de verificación para Nino AI es: {code}\n\nCaduca en 10 minutos. Si no has solicitado esta cuenta, ignora este correo.")
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


def public_profile(user):
    tts_prov = "gemini"
    try:
        if "tts_provider" in user.keys():
            tts_prov = user["tts_provider"] or "gemini"
    except Exception:
        pass
    return {
        "username": user["username"],
        "session_active": True,
        "tts_provider": tts_prov,
        "fish_key": user["fish_key"],
        "active_gemini_slot": user["active_gemini_slot"],
        "gemini_keys": [user[f"gemini_key_{slot}"] for slot in range(1, 6)],
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

    async def generar():
        texto_procesado = limpiar_texto(texto)
        if not texto_procesado:
            texto_procesado = "..."
        communicate = edge_tts.Communicate(texto_procesado, voice, rate=rate, pitch=pitch)
        await communicate.save(ruta)

    asyncio.run(generar())
    limpiar_cache_audio()
    return f"/audio_cache/{nombre}"


def generar_audio_fish(texto, reference_id, api_key):
    """Genera audio con Fish Audio para Nino Nakano usando su voz clonada."""
    if not api_key:
        raise ValueError("No hay una clave de Fish Audio configurada")
    nombre = f"voice_fish_{uuid.uuid4().hex[:8]}.mp3"
    ruta = os.path.join(AUDIO_DIR, nombre)
    try:
        texto_procesado = limpiar_texto(texto)
        if not texto_procesado:
            texto_procesado = "..."
        api_response = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"text": texto_procesado, "reference_id": reference_id, "format": "mp3"},
            timeout=25,
        )
        api_response.raise_for_status()
        with open(ruta, "wb") as audio_file:
            audio_file.write(api_response.content)
        limpiar_cache_audio()
        return f"/audio_cache/{nombre}"
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


def obtener_chat(user, waifu, gemini_key, model):
    chat_key = (user["id"], waifu, gemini_key, model)
    if chat_key not in chats:
        client = obtener_client(gemini_key)
        chats[chat_key] = client.chats.create(
            model=model,
            config={"system_instruction": WAIFU_CONFIG[waifu]["system_instruction"]},
        )
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
            contents=texto_procesado,
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


def instrucciones_modo(modo, waifu):
    """Adapta el estilo de este turno sin cambiar la personalidad base del personaje."""
    personaje = "Nino, mantén tu carácter tsundere" if waifu == "nino" else "Miku, mantén tu energía alegre y musical"
    if modo == "pregunta":
        return (f"\n\nEn este turno estás en modo pregunta. {personaje}. "
                "Responde primero a lo que se pregunta de forma directa, clara y útil; sé concisa y evita desviar la respuesta. "
                "Conserva tu tono y actitud propios de manera natural.")
    if modo == "automatico":
        return (f"\n\nEn este turno estás en modo automático. {personaje}. "
                "Decide por el contenido del mensaje si la persona busca una respuesta concreta o una charla abierta. "
                "Si pregunta algo, responde primero de forma clara y directa, añadiendo solo el contexto útil. "
                "Si inicia una conversación, conversa con naturalidad y deja espacio para continuar. "
                "Conserva siempre tu tono y actitud propios.")
    return ""


def procesar_gemini(mensaje, user, waifu="nino", modo="conversacion", generar_voz=True):
    """
    Procesa la conversación con Gemini a través de la lista de modelos soportados
    e integra la síntesis de voz según la preferencia 'tts_provider' del usuario:
    - 'gemini': Gemini 3.8 TTS -> Fallback a Edge TTS
    - 'fish': Fish Audio -> Fallback a Gemini TTS -> Fallback a Edge TTS
    - 'edge': Edge TTS directamente
    Guarda el archivo MP3 generado en /audio_cache/.
    """
    if waifu not in WAIFU_CONFIG:
        waifu = "nino"
    active_slot = user["active_gemini_slot"]
    gemini_key = (user[f"gemini_key_{active_slot}"] or "").strip()
    if not gemini_key:
        return "Añade una API Key de Gemini en tu perfil para poder conversar.", None

    respuesta = None
    last_error = None
    for model in AVAILABLE_MODELS:
        chat_key = (user["id"], waifu, gemini_key, model)
        try:
            chat = obtener_chat(user, waifu, gemini_key, model)
            indicacion_modo = instrucciones_modo(modo, waifu)
            mensaje_turno = f"{indicacion_modo}\n\nMensaje de la persona: {mensaje}" if indicacion_modo else mensaje
            respuesta = chat.send_message(mensaje_turno).text
            break
        except Exception as error:
            last_error = error
            chats.pop(chat_key, None)
            if "closed" in str(error).lower():
                clients.pop(gemini_key, None)
            print(f"[Aviso Gemini] Modelo {model} falló: {error}")
            error_detail = str(error).lower()
            if any(marker in error_detail for marker in ("429", "resource_exhausted", "quota", "403", "401", "api_key_invalid", "permission_denied")):
                break

    if not respuesta:
        respuesta = mensaje_error_gemini(last_error)

    # El texto llega al navegador sin esperar a proveedores de voz que pueden tardar varios segundos.
    if not generar_voz:
        return respuesta, None

    config = WAIFU_CONFIG[waifu]
    audio_url = None

    # Limpieza previa del texto: fonética y estricta para Edge/Fish, natural para Gemini
    texto_audio = limpiar_texto(respuesta)
    if not texto_audio:
        texto_audio = "..."
    texto_gemini = limpiar_texto_gemini(respuesta) or texto_audio

    # Configuración de voz TTS según waifu
    voice = config.get("voice", "es-ES-ElviraNeural")
    rate = config.get("rate", "+0%")
    pitch = config.get("pitch", "+0Hz")

    # Leer preferencia 'tts_provider' guardada por el usuario (por defecto 'gemini')
    user_tts_pref = "gemini"
    try:
        if "tts_provider" in user.keys():
            user_tts_pref = user["tts_provider"] or "gemini"
    except Exception:
        user_tts_pref = "gemini"
    user_tts_pref = str(user_tts_pref).strip().lower()
    if user_tts_pref not in ("gemini", "fish", "edge"):
        user_tts_pref = "gemini"

    # 1. Si selecciona 'gemini': Intenta primero generar_audio_gemini(). Si falla, salta como respaldo a Edge TTS.
    if user_tts_pref == "gemini":
        try:
            audio_url = generar_audio_gemini(texto_gemini, gemini_key, waifu=waifu)
        except Exception as error:
            print(f"[Aviso TTS] Gemini 3.8 TTS falló ({error}). Activando Edge TTS de respaldo...")
            try:
                audio_url = generar_audio_edge(texto_audio, voice=voice, rate=rate, pitch=pitch)
            except Exception as edge_error:
                print(f"[Error TTS] Edge TTS de respaldo falló: {edge_error}")
                audio_url = None

    # 2. Si selecciona 'fish': Intenta primero Fish Audio. Si no hay clave o falla, salta a Gemini TTS, y finalmente a Edge TTS.
    elif user_tts_pref == "fish":
        fish_key = ""
        try:
            if "fish_key" in user.keys():
                fish_key = str(user["fish_key"] or "").strip()
        except Exception:
            fish_key = ""

        if fish_key:
            try:
                ref_id = config.get("reference_id", "c961aaa2a71f469e98b8b2151b8c219d")
                audio_url = generar_audio_fish(texto_audio, ref_id, fish_key)
            except Exception as fish_error:
                print(f"[Aviso TTS] Fish Audio falló ({fish_error}). Saltando a Gemini TTS de respaldo...")
        else:
            print("[Aviso TTS] No hay clave de Fish Audio configurada. Saltando a Gemini TTS de respaldo...")

        if not audio_url:
            try:
                audio_url = generar_audio_gemini(texto_gemini, gemini_key, waifu=waifu)
            except Exception as gemini_error:
                print(f"[Aviso TTS] Gemini TTS de respaldo falló ({gemini_error}). Saltando a Edge TTS...")

        if not audio_url:
            try:
                audio_url = generar_audio_edge(texto_audio, voice=voice, rate=rate, pitch=pitch)
            except Exception as edge_error:
                print(f"[Error TTS] Edge TTS de respaldo final falló: {edge_error}")
                audio_url = None

    # 3. Si selecciona 'edge': Usa directamente Edge TTS.
    elif user_tts_pref == "edge":
        try:
            audio_url = generar_audio_edge(texto_audio, voice=voice, rate=rate, pitch=pitch)
        except Exception as edge_error:
            print(f"[Error TTS] Edge TTS falló: {edge_error}")
            audio_url = None

    return respuesta, audio_url


def iniciar_generacion_audio(texto, user, waifu):
    job_id = uuid.uuid4().hex
    with speech_jobs_lock:
        now = time.time()
        for stale_id, stale_job in list(speech_jobs.items()):
            if now - stale_job["created_at"] > 600:
                speech_jobs.pop(stale_id, None)
        speech_jobs[job_id] = {"user_id": user["id"], "status": "pending", "audio_url": None, "created_at": now}

    def generar():
        try:
            audio_url = generar_audio_respuesta(texto, user, waifu)
        except Exception as error:
            print(f"[Error TTS] No se pudo generar el audio: {error}")
            audio_url = None
        with speech_jobs_lock:
            job = speech_jobs.get(job_id)
            if job:
                job["status"] = "done"
                job["audio_url"] = audio_url

    Thread(target=generar, name=f"speech-{job_id[:8]}", daemon=True).start()
    return job_id


def generar_audio_respuesta(texto, user, waifu="nino"):
    """Genera voz bajo demanda, después de que el texto ya se haya mostrado."""
    if waifu not in WAIFU_CONFIG:
        waifu = "nino"
    active_slot = user["active_gemini_slot"]
    gemini_key = (user[f"gemini_key_{active_slot}"] or "").strip()
    if not gemini_key:
        return None
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
    if user_tts_pref == "fish":
        try:
            fish_key = str(user["fish_key"] or "").strip()
        except Exception:
            fish_key = ""
        if fish_key:
            try:
                return generar_audio_fish(texto_audio, config.get("reference_id", "c961aaa2a71f469e98b8b2151b8c219d"), fish_key)
            except Exception as error:
                print(f"[Aviso TTS] Fish Audio falló: {error}")
    try:
        return generar_audio_gemini(texto_gemini, gemini_key, waifu=waifu)
    except Exception as error:
        print(f"[Aviso TTS] Gemini TTS falló: {error}")
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
                "data": {"username": username, "terms_version": TERMS_VERSION},
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
    set_session_cookie(token)
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
    with db_connection() as conn:
        user = db_execute(conn, "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)).fetchone()
    if not user:
        return json_response({"status": "error", "message": "Usuario o contraseña incorrectos."}, 401)

    # Las cuentas confirmadas anteriormente mediante enlace podían haberse
    # creado localmente con una contraseña aleatoria. Si la contraseña local
    # no coincide, validarla con Supabase y sincronizar el hash solo si Supabase
    # confirma que es correcta.
    supabase_access_token = None
    if not verify_password(password, user["password_hash"]):
        if supabase_client is None:
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
            with db_connection() as conn:
                db_execute(conn, "UPDATE users SET password_hash = ?, email_verified = 1 WHERE id = ?",
                           (hash_password(password), user["id"]))
                user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            print(f"[Supabase] Contraseña local sincronizada para: {user['email']}")
        except Exception as sb_err:
            print(f"[Supabase] No se pudo validar el acceso para {user['email']}: {sb_err}")
            return json_response({"status": "error", "message": "Usuario o contraseña incorrectos."}, 401)

    with db_connection() as conn:
        token = new_session_token()
        db_execute(conn, "UPDATE users SET session_token = ?, terms_accepted_at = ?, terms_version = ? WHERE id = ?",
                   (token, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), TERMS_VERSION, user["id"]))
        user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    set_session_cookie(token)

    # Inicio de sesión paralelo en Supabase Auth (si está configurado)
    if supabase_client is not None and not supabase_access_token:
        try:
            sb_response = supabase_client.auth.sign_in_with_password({
                "email": user["email"],
                "password": password,
            })
            if sb_response and getattr(sb_response, "session", None):
                supabase_access_token = sb_response.session.access_token
                print(f"[Supabase] Login correcto para: {user['email']}")
        except Exception as sb_err:
            # No bloqueamos el login local si Supabase falla
            print(f"[Supabase] Advertencia en login: {sb_err}")

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

    set_session_cookie(token_local)
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
    try:
        active_slot = int(data.get("active_gemini_slot", user["active_gemini_slot"]))
    except (TypeError, ValueError):
        active_slot = 0
    keys = data.get("gemini_keys", [])
    if not isinstance(keys, list) or len(keys) != 5 or active_slot not in range(1, 6):
        return json_response({"status": "error", "message": "Revisa el slot activo y las cinco claves de Gemini."}, 400)
    clean_keys = [str(key or "").strip() for key in keys]
    fish_key = str(data.get("fish_key", "")).strip()
    tts_provider = str(data.get("tts_provider", "gemini")).strip().lower()
    if tts_provider not in ("gemini", "fish", "edge"):
        tts_provider = "gemini"
    with db_connection() as conn:
        db_execute(
            conn,
            """UPDATE users SET tts_provider = ?, fish_key = ?, active_gemini_slot = ?, gemini_key_1 = ?,
               gemini_key_2 = ?, gemini_key_3 = ?, gemini_key_4 = ?, gemini_key_5 = ? WHERE id = ?""",
            (tts_provider, fish_key, active_slot, *clean_keys, user["id"]),
        )
        updated_user = db_execute(conn, "SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    for chat_key in list(chats):
        if chat_key[0] == user["id"]:
            del chats[chat_key]
    return json_response({"status": "ok", "profile": public_profile(updated_user)})


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
    respuesta, _ = procesar_gemini(mensaje, user, waifu, modo=modo, generar_voz=False)
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

@server_app.route("/")
def index():
    """Carga la interfaz principal avatar.html en la raíz del servidor."""
    return static_file("avatar.html", root=BASE_DIR)


@server_app.route("/audio_cache/<filename>")
def servir_audio(filename):
    """Sirve los archivos de voz generados en la caché de audio."""
    return static_file(filename, root=AUDIO_DIR)


@server_app.route("/<filename:path>")
def send_static(filename):
    """Sirve recursos estáticos (modelos VRM, texturas, etc.) protegiendo archivos sensibles."""
    if filename.lower().endswith(".db") or filename.lower() == "main.py":
        raise HTTPError(403, "Access denied.")
    return static_file(filename, root=BASE_DIR)


# --- PUNTO DE ENTRADA DEL SERVIDOR ---

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Iniciando servidor Omni-Wifu en http://0.0.0.0:{port}...")
    server_app.run(host="0.0.0.0", port=port)
