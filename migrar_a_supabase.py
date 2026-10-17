"""
Copia los usuarios de la base local (users.db) a la base PostgreSQL de Supabase.
Uso (una sola vez):
    set DATABASE_URL=postgresql://...      (en PowerShell: $env:DATABASE_URL="postgresql://...")
    python migrar_a_supabase.py
Si un usuario ya existe en Supabase (mismo nombre o correo) se omite.
"""
import os
import sqlite3
import sys

try:
    import psycopg
except ImportError:
    sys.exit('Falta el driver: pip install "psycopg[binary]"')

url = os.environ.get("DATABASE_URL", "").strip().replace("postgres://", "postgresql://", 1)
if not url:
    sys.exit("Define primero la variable de entorno DATABASE_URL.")
if "supabase" in url and "sslmode=" not in url:
    url += ("&" if "?" in url else "?") + "sslmode=require"

ruta = os.path.join(os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__))), "users.db")
if not os.path.exists(ruta):
    sys.exit(f"No encuentro {ruta}")

# Crea las tablas con el esquema de main.py (también activa la protección RLS).
os.environ["DATABASE_URL"] = url
import main  # noqa: E402,F401

local = sqlite3.connect(ruta)
local.row_factory = sqlite3.Row
filas = local.execute("SELECT * FROM users").fetchall()
copiados = omitidos = 0
with psycopg.connect(url, prepare_threshold=None, autocommit=True) as pg:
    columnas_pg = {r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'users' AND table_schema = 'public'")}
    for fila in filas:
        datos = {k: fila[k] for k in fila.keys() if k in columnas_pg and k != "id"}
        datos["session_token"] = None  # obliga a iniciar sesión de nuevo
        cols = ", ".join(datos)
        marcas = ", ".join(["%s"] * len(datos))
        try:
            pg.execute(f"INSERT INTO users ({cols}) VALUES ({marcas})", list(datos.values()))
            copiados += 1
        except psycopg.errors.IntegrityError:
            omitidos += 1
print(f"Copiados: {copiados}. Omitidos (ya existían): {omitidos}.")
