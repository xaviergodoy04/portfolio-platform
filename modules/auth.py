"""
Autenticación por sesión (cookie firmada de Flask, sin dependencias nuevas).

Solo las rutas privadas (portfolio, watchlist, alertas, feedback de noticias,
cuenta) exigen sesión — el resto de la app (Explorar, Noticias, chat) es
navegable sin cuenta. Ver login_required() para las rutas privadas y
current_user_id() para el uso opcional en las públicas.

No hay registro: las cuentas las crea Xavier con
`python -m modules.admin_create_user`.
"""
from functools import wraps

from flask import jsonify, session

from modules import db


def current_user_id() -> int | None:
    """user_id de la sesión actual, o None si es anónima."""
    return session.get("user_id")


def login_required(fn):
    """Decorador para rutas privadas: 401 si no hay sesión."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user_id() is None:
            return jsonify({"error": "Iniciá sesión para acceder a esto"}), 401
        return fn(*args, **kwargs)
    return wrapper


def login(username: str, password: str) -> dict | None:
    """Verifica credenciales y abre la sesión. Retorna el usuario o None."""
    user = db.verify_login(username, password)
    if user:
        session["user_id"] = user["id"]
        session.permanent = True
    return user


def logout():
    session.pop("user_id", None)
