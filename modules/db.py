"""
Capa de persistencia SQLite (stdlib sqlite3, sin ORM).

- DB en data/portfolio.db (ignorada por git).
- init_db() es idempotente y migra automáticamente los JSON legacy
  (data/alerts.json, data/smart_alerts.json, data/smart_config.json)
  la primera vez, sin borrarlos (quedan como backup).
- Acceso serializado con un lock global: suficiente para una app personal
  con pocos hilos (Flask + scheduler).
"""
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "portfolio.db")
BACKUPS_DIR = os.path.join(DATA_DIR, "backups")
BACKUPS_KEEP = 14  # cuántos backups diarios conservar

ALERTS_JSON = os.path.join(DATA_DIR, "alerts.json")
SMART_ALERTS_JSON = os.path.join(DATA_DIR, "smart_alerts.json")
SMART_CONFIG_JSON = os.path.join(DATA_DIR, "smart_config.json")

_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    """Conexión nueva por llamada (cada hilo abre la suya)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_conn():
    """Context manager: abre conexión, commitea al salir y cierra siempre."""
    with _lock:
        conn = get_conn()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ── Esquema ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    date TEXT NOT NULL,
    total_value REAL,
    total_pnl REAL,
    pnl_pct REAL,
    positions_json TEXT,
    created_at TEXT,
    UNIQUE (user_id, date)
);

CREATE TABLE IF NOT EXISTS portfolio_cache (
    user_id INTEGER PRIMARY KEY,
    payload_json TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    symbol TEXT NOT NULL,
    alert_type TEXT DEFAULT 'price',
    condition TEXT,
    target_price REAL,
    pct_change REAL,
    reference_price REAL,
    note TEXT DEFAULT '',
    triggered INTEGER DEFAULT 0,
    triggered_at TEXT,
    triggered_price REAL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS smart_alerts (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    symbol TEXT NOT NULL,
    detected_at TEXT,
    price_at_detection REAL,
    entry_score REAL,
    growth_score REAL,
    risk_score REAL,
    seen INTEGER DEFAULT 0,
    payload_json TEXT
);

CREATE TABLE IF NOT EXISTS smart_config (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    ts TEXT,
    endpoint TEXT,
    method TEXT,
    status INTEGER,
    duration_ms REAL
);

CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    added_at TEXT,
    note TEXT DEFAULT '',
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE IF NOT EXISTS feed_health (
    source_name TEXT PRIMARY KEY,
    url TEXT,
    section TEXT,
    status TEXT,
    entries INTEGER,
    last_ok TEXT,
    last_checked TEXT
);

CREATE TABLE IF NOT EXISTS news_feedback (
    user_id INTEGER NOT NULL,
    url_hash TEXT NOT NULL,
    url TEXT,
    title TEXT,
    section TEXT,
    source TEXT,
    symbols TEXT,
    liked INTEGER DEFAULT 0,
    read INTEGER DEFAULT 0,
    liked_at TEXT,
    read_at TEXT,
    PRIMARY KEY (user_id, url_hash)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    ibkr_flex_token TEXT,
    ibkr_flex_query_id TEXT,
    created_at TEXT
);
"""

# Tablas "privadas" cuya PK original queda reemplazada por una compuesta que
# incluye user_id. SQLite no soporta ALTER de constraints: se migran con el
# patrón rename -> create -> copy -> drop. (table, create_sql, copy_cols)
_COMPOSITE_PK_MIGRATIONS = [
    (
        "portfolio_cache",
        """CREATE TABLE portfolio_cache (
               user_id INTEGER PRIMARY KEY,
               payload_json TEXT,
               updated_at TEXT
           )""",
        "user_id, payload_json, updated_at",
        "1 AS user_id, payload_json, updated_at",
    ),
    (
        "watchlist",
        """CREATE TABLE watchlist (
               user_id INTEGER NOT NULL,
               symbol TEXT NOT NULL,
               added_at TEXT,
               note TEXT DEFAULT '',
               PRIMARY KEY (user_id, symbol)
           )""",
        "user_id, symbol, added_at, note",
        "1 AS user_id, symbol, added_at, note",
    ),
    (
        "smart_config",
        """CREATE TABLE smart_config (
               user_id INTEGER NOT NULL,
               key TEXT NOT NULL,
               value_json TEXT,
               PRIMARY KEY (user_id, key)
           )""",
        "user_id, key, value_json",
        "1 AS user_id, key, value_json",
    ),
    (
        "news_feedback",
        """CREATE TABLE news_feedback (
               user_id INTEGER NOT NULL,
               url_hash TEXT NOT NULL,
               url TEXT,
               title TEXT,
               section TEXT,
               source TEXT,
               symbols TEXT,
               liked INTEGER DEFAULT 0,
               read INTEGER DEFAULT 0,
               liked_at TEXT,
               read_at TEXT,
               PRIMARY KEY (user_id, url_hash)
           )""",
        "user_id, url_hash, url, title, section, source, symbols, liked, read, liked_at, read_at",
        "1 AS user_id, url_hash, url, title, section, source, symbols, liked, read, liked_at, read_at",
    ),
    (
        "portfolio_snapshots",
        """CREATE TABLE portfolio_snapshots (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER NOT NULL,
               date TEXT NOT NULL,
               total_value REAL,
               total_pnl REAL,
               pnl_pct REAL,
               positions_json TEXT,
               created_at TEXT,
               UNIQUE (user_id, date)
           )""",
        "user_id, date, total_value, total_pnl, pnl_pct, positions_json, created_at",
        "1 AS user_id, date, total_value, total_pnl, pnl_pct, positions_json, created_at",
    ),
]

# Tablas con PK simple: solo necesitan sumar la columna user_id (default 1
# para las filas ya existentes de Xavier).
_ADD_USER_ID_TABLES = ["alerts", "smart_alerts", "events"]


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_exists(conn, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate_to_multiuser(conn: sqlite3.Connection):
    """
    Agrega user_id a las tablas privadas (idempotente: chequea antes de migrar
    cada tabla). Todas las filas preexistentes quedan en user_id=1 (Xavier) —
    ver bootstrap_admin_user() para la creación de esa cuenta.
    """
    for table in _ADD_USER_ID_TABLES:
        if _table_exists(conn, table) and not _column_exists(conn, table, "user_id"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER DEFAULT 1")

    for table, create_sql, copy_cols, select_cols in _COMPOSITE_PK_MIGRATIONS:
        if not _table_exists(conn, table) or _column_exists(conn, table, "user_id"):
            continue  # no existe todavía (DB nueva) o ya migrada
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        conn.execute(create_sql)
        conn.execute(f"INSERT INTO {table} ({copy_cols}) SELECT {select_cols} FROM {table}_old")
        conn.execute(f"DROP TABLE {table}_old")


def bootstrap_admin_user():
    """Si la tabla users está vacía, crea la cuenta de Xavier (user_id=1) desde
    ADMIN_USERNAME/ADMIN_PASSWORD del .env, o con una contraseña generada que
    se imprime una sola vez si no están seteadas."""
    from werkzeug.security import generate_password_hash
    with db_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return
        import config
        import secrets
        username = getattr(config, "ADMIN_USERNAME", "") or "xavier"
        password = getattr(config, "ADMIN_PASSWORD", "") or secrets.token_urlsafe(12)
        conn.execute(
            """INSERT INTO users (id, username, password_hash, display_name, created_at)
               VALUES (1, ?, ?, ?, ?)""",
            (username, generate_password_hash(password), username, datetime.now().isoformat()),
        )
        if not getattr(config, "ADMIN_PASSWORD", ""):
            print(f"👤 Cuenta admin creada — usuario: {username} · contraseña: {password}")
            print("   (Guardala: no se vuelve a mostrar. Podés fijarla con ADMIN_PASSWORD en .env)")


def init_db():
    """Crea las tablas si no existen y migra los JSON legacy y el esquema
    multi-usuario una sola vez cada uno."""
    with db_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate_to_multiuser(conn)
        _migrate_from_json(conn)
    bootstrap_admin_user()


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _migrate_from_json(conn: sqlite3.Connection):
    """Importa los JSON legacy solo si la tabla correspondiente está vacía.
    Los archivos JSON NO se borran: quedan como backup."""

    # Alertas de precio (todas de Xavier, user_id=1: es la única cuenta que
    # existía cuando estos JSON legacy se generaron)
    if conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0:
        data = _read_json(ALERTS_JSON)
        if data:
            for a in data:
                conn.execute(
                    """INSERT OR IGNORE INTO alerts
                       (id, user_id, symbol, alert_type, condition, target_price, pct_change,
                        reference_price, note, triggered, triggered_at, triggered_price, created_at)
                       VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        a.get("id"), a.get("symbol"), a.get("alert_type", "price"),
                        a.get("condition"), a.get("target_price"), a.get("pct_change"),
                        a.get("reference_price"), a.get("note", ""),
                        1 if a.get("triggered") else 0,
                        a.get("triggered_at"), a.get("triggered_price"), a.get("created_at"),
                    ),
                )

    # Smart alerts (el precio al detectar viene en "current_price" dentro de cada entrada)
    if conn.execute("SELECT COUNT(*) FROM smart_alerts").fetchone()[0] == 0:
        data = _read_json(SMART_ALERTS_JSON)
        if data:
            for e in data:
                conn.execute(
                    """INSERT OR IGNORE INTO smart_alerts
                       (id, user_id, symbol, detected_at, price_at_detection, entry_score,
                        growth_score, risk_score, seen, payload_json)
                       VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        e.get("id"), e.get("symbol"), e.get("detected_at"),
                        e.get("current_price", e.get("price_at_detection")),
                        e.get("entry_score"), e.get("growth_score"), e.get("risk_score"),
                        1 if e.get("seen") else 0, json.dumps(e),
                    ),
                )

    # Config de smart alerts (key/value)
    if conn.execute("SELECT COUNT(*) FROM smart_config").fetchone()[0] == 0:
        data = _read_json(SMART_CONFIG_JSON)
        if isinstance(data, dict):
            for k, v in data.items():
                conn.execute(
                    "INSERT OR IGNORE INTO smart_config (user_id, key, value_json) VALUES (1, ?, ?)",
                    (k, json.dumps(v)),
                )


# ── Portfolio: cache persistente + snapshots diarios (por usuario) ───────────

def save_portfolio_cache(user_id: int, data: dict):
    """Guarda el último portfolio completo de un usuario (sobrevive reinicios)."""
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO portfolio_cache (user_id, payload_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 payload_json = excluded.payload_json,
                 updated_at = excluded.updated_at""",
            (user_id, json.dumps(data), datetime.now().isoformat()),
        )


def load_portfolio_cache(user_id: int):
    """Retorna el último portfolio guardado de un usuario, o None si no hay."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM portfolio_cache WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row or not row["payload_json"]:
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception:
        return None


def upsert_snapshot(user_id: int, data: dict, date: str = None):
    """Upsert del snapshot del día de un usuario a partir de un portfolio ya calculado."""
    summary = data.get("summary", {}) or {}
    date = date or datetime.now().strftime("%Y-%m-%d")
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO portfolio_snapshots
               (user_id, date, total_value, total_pnl, pnl_pct, positions_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET
                 total_value = excluded.total_value,
                 total_pnl = excluded.total_pnl,
                 pnl_pct = excluded.pnl_pct,
                 positions_json = excluded.positions_json,
                 created_at = excluded.created_at""",
            (
                user_id, date,
                summary.get("total_value"),
                summary.get("total_unrealized_pnl"),
                summary.get("total_pnl_pct"),
                json.dumps(data.get("positions", [])),
                datetime.now().isoformat(),
            ),
        )


def get_snapshots(user_id: int, days: int = 90) -> list:
    """Snapshots de un usuario en los últimos N días, ordenados por fecha ascendente."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT date, total_value, total_pnl, pnl_pct
               FROM portfolio_snapshots WHERE user_id = ? AND date >= ? ORDER BY date ASC""",
            (user_id, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def has_snapshot_today(user_id: int) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM portfolio_snapshots WHERE user_id = ? AND date = ?", (user_id, today)
        ).fetchone()
    return row is not None


# ── Backup diario de la base ─────────────────────────────────────────────────

def backup_db() -> str | None:
    """
    Copia data/portfolio.db a data/backups/portfolio-YYYY-MM-DD.db usando la
    API de backup de sqlite3 (conn.backup()), que es segura aunque la DB esté
    siendo escrita (a diferencia de un shutil.copy). Un backup por día: si ya
    existe el de hoy, se sobreescribe (queda el estado más reciente del día).

    Rotación: conserva los últimos BACKUPS_KEEP backups y borra los más viejos.
    Retorna la ruta del backup creado, o None si no hay DB que respaldar.
    """
    if not os.path.exists(DB_PATH):
        return None

    os.makedirs(BACKUPS_DIR, exist_ok=True)
    dest_path = os.path.join(
        BACKUPS_DIR, f"portfolio-{datetime.now().strftime('%Y-%m-%d')}.db"
    )

    # El lock global serializa contra las escrituras de esta app; conn.backup()
    # además es consistente frente a cualquier otro escritor del archivo.
    with _lock:
        src = sqlite3.connect(DB_PATH)
        try:
            dest = sqlite3.connect(dest_path)
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()

    _rotate_backups()
    return dest_path


def _rotate_backups(keep: int = BACKUPS_KEEP):
    """Borra los backups más viejos, conservando los `keep` más recientes.
    El nombre portfolio-YYYY-MM-DD.db ordena cronológicamente de forma natural."""
    try:
        backups = sorted(
            f for f in os.listdir(BACKUPS_DIR)
            if f.startswith("portfolio-") and f.endswith(".db")
        )
    except FileNotFoundError:
        return
    for old in backups[:-keep] if keep > 0 else backups:
        try:
            os.remove(os.path.join(BACKUPS_DIR, old))
        except OSError:
            pass


def has_backup_today() -> bool:
    """True si ya existe el backup de hoy en data/backups/."""
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.exists(os.path.join(BACKUPS_DIR, f"portfolio-{today}.db"))


# ── Watchlist (por usuario) ───────────────────────────────────────────────────

def get_watchlist(user_id: int) -> list:
    """Entradas de la watchlist de un usuario, la más reciente primero."""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT symbol, added_at, note FROM watchlist WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_watchlist_symbols(user_id: int) -> list:
    """Solo los símbolos de un usuario (para sumarlos al radar y a las smart alerts)."""
    with db_conn() as conn:
        rows = conn.execute("SELECT symbol FROM watchlist WHERE user_id = ?", (user_id,)).fetchall()
    return [r["symbol"] for r in rows]


def add_to_watchlist(user_id: int, symbol: str, note: str = "") -> dict:
    """Agrega un símbolo a la watchlist de un usuario. Si ya existe, retorna la entrada actual."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT symbol, added_at, note FROM watchlist WHERE user_id = ? AND symbol = ?",
            (user_id, symbol),
        ).fetchone()
        if row:
            return dict(row)
        entry = {"symbol": symbol, "added_at": datetime.now().isoformat(), "note": note or ""}
        conn.execute(
            "INSERT INTO watchlist (user_id, symbol, added_at, note) VALUES (?, ?, ?, ?)",
            (user_id, entry["symbol"], entry["added_at"], entry["note"]),
        )
    return entry


def remove_from_watchlist(user_id: int, symbol: str) -> bool:
    """Quita un símbolo de la watchlist de un usuario. True si existía."""
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND symbol = ?", (user_id, symbol)
        )
        return cur.rowcount > 0


# ── Health check de fuentes de noticias ─────────────────────────────────────

def upsert_feed_health(source_name: str, url: str, section: str,
                       status: str, entries: int, checked_at: str):
    """Upsert del estado de una fuente RSS. Si la fuente respondió (status ok)
    se actualiza last_ok; si está caída, se conserva el last_ok anterior para
    saber desde cuándo dejó de funcionar."""
    last_ok = checked_at if status == "ok" else None
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO feed_health
               (source_name, url, section, status, entries, last_ok, last_checked)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_name) DO UPDATE SET
                 url = excluded.url,
                 section = excluded.section,
                 status = excluded.status,
                 entries = excluded.entries,
                 last_ok = COALESCE(excluded.last_ok, feed_health.last_ok),
                 last_checked = excluded.last_checked""",
            (source_name, url, section, status, entries, last_ok, checked_at),
        )


def prune_feed_health(current_names: list):
    """Borra fuentes que ya no existen en RSS_SOURCES (renombradas o eliminadas)."""
    if not current_names:
        return
    placeholders = ",".join("?" for _ in current_names)
    with db_conn() as conn:
        conn.execute(
            f"DELETE FROM feed_health WHERE source_name NOT IN ({placeholders})",
            list(current_names),
        )


def get_feed_health() -> list:
    """Estado de todas las fuentes, agrupadas por sección y nombre."""
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT source_name, url, section, status, entries, last_ok, last_checked
               FROM feed_health ORDER BY section, source_name"""
        ).fetchall()
    return [dict(r) for r in rows]


# ── Feedback de noticias (likes / leídas, por usuario) ────────────────────────

def news_url_hash(url: str) -> str:
    """Hash estable de la URL de una noticia (parte de la clave del feedback)."""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()


def set_news_feedback(user_id: int, item_data: dict, liked=None, read=None) -> dict:
    """
    Upsert parcial del feedback de un usuario sobre una noticia: solo pisa los
    campos que vienen (liked y/o read); el otro conserva su valor anterior.
    liked_at / read_at guardan cuándo se activó cada flag (se limpian al
    desactivarlo) y alimentan el decaimiento temporal del perfil de afinidad.
    Retorna el estado resultante: {url_hash, liked, read}.
    """
    url = (item_data.get("url") or "").strip()
    h = news_url_hash(url)
    now = datetime.now().isoformat()
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO news_feedback (user_id, url_hash, url, title, section, source, symbols)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, url_hash) DO UPDATE SET
                 title = excluded.title,
                 section = excluded.section,
                 source = excluded.source,
                 symbols = excluded.symbols""",
            (
                user_id, h, url,
                item_data.get("title") or "",
                item_data.get("section") or "",
                item_data.get("source") or "",
                json.dumps(item_data.get("symbols") or []),
            ),
        )
        if liked is not None:
            conn.execute(
                'UPDATE news_feedback SET liked = ?, liked_at = ? WHERE user_id = ? AND url_hash = ?',
                (1 if liked else 0, now if liked else None, user_id, h),
            )
        if read is not None:
            conn.execute(
                'UPDATE news_feedback SET "read" = ?, read_at = ? WHERE user_id = ? AND url_hash = ?',
                (1 if read else 0, now if read else None, user_id, h),
            )
        row = conn.execute(
            'SELECT url_hash, liked, "read" FROM news_feedback WHERE user_id = ? AND url_hash = ?',
            (user_id, h),
        ).fetchone()
    return {"url_hash": row["url_hash"], "liked": bool(row["liked"]), "read": bool(row["read"])}


def get_news_feedback_map(user_id: int) -> dict:
    """Mapa {url_hash: {liked, read}} de un usuario, para mergear en /api/news
    en O(1) por item."""
    with db_conn() as conn:
        rows = conn.execute(
            'SELECT url_hash, liked, "read" FROM news_feedback '
            'WHERE user_id = ? AND (liked = 1 OR "read" = 1)',
            (user_id,),
        ).fetchall()
    return {
        r["url_hash"]: {"liked": bool(r["liked"]), "read": bool(r["read"])}
        for r in rows
    }


def get_feedback_rows(user_id: int, days: int = 90) -> list:
    """Filas de feedback de un usuario con actividad en los últimos N días
    (para su perfil de afinidad y el tablero de stats). symbols viene ya
    parseado a lista."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with db_conn() as conn:
        rows = conn.execute(
            '''SELECT url, title, section, source, symbols,
                      liked, "read", liked_at, read_at
               FROM news_feedback
               WHERE user_id = ?
                 AND ((liked_at IS NOT NULL AND liked_at >= ?)
                   OR (read_at IS NOT NULL AND read_at >= ?))''',
            (user_id, cutoff, cutoff),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["symbols"] = json.loads(d.get("symbols") or "[]")
        except Exception:
            d["symbols"] = []
        out.append(d)
    return out


# ── Usuarios y sesión ──────────────────────────────────────────────────────

def create_user(username: str, password: str, display_name: str = None) -> dict:
    """Crea un usuario nuevo (hashea la contraseña). Usado solo por el CLI de
    administración — no hay registro público."""
    from werkzeug.security import generate_password_hash
    with db_conn() as conn:
        cur = conn.execute(
            """INSERT INTO users (username, password_hash, display_name, created_at)
               VALUES (?, ?, ?, ?)""",
            (username, generate_password_hash(password), display_name or username,
             datetime.now().isoformat()),
        )
        user_id = cur.lastrowid
    return {"id": user_id, "username": username, "display_name": display_name or username}


def set_password(username: str, password: str) -> bool:
    """Resetea la contraseña de un usuario existente. True si el usuario existía."""
    from werkzeug.security import generate_password_hash
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(password), username),
        )
    return cur.rowcount > 0


def verify_login(username: str, password: str) -> dict | None:
    """Verifica usuario+contraseña. Retorna {id, username, display_name} o None."""
    from werkzeug.security import check_password_hash
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, display_name FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return None
    return {"id": row["id"], "username": row["username"], "display_name": row["display_name"]}


def get_user(user_id: int) -> dict | None:
    """Datos públicos de un usuario (sin password_hash)."""
    with db_conn() as conn:
        row = conn.execute(
            """SELECT id, username, display_name, ibkr_flex_token, ibkr_flex_query_id
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_all_user_ids() -> list:
    """IDs de todos los usuarios (para que el scheduler itere)."""
    with db_conn() as conn:
        rows = conn.execute("SELECT id FROM users").fetchall()
    return [r["id"] for r in rows]


def set_ibkr_credentials(user_id: int, token: str, query_id: str):
    """Guarda el token+queryID de IBKR Flex Query propio de un usuario."""
    with db_conn() as conn:
        conn.execute(
            "UPDATE users SET ibkr_flex_token = ?, ibkr_flex_query_id = ? WHERE id = ?",
            (token, query_id, user_id),
        )


# ── Instrumentación de uso de endpoints ──────────────────────────────────────

def log_event(endpoint: str, method: str, status: int, duration_ms: float, user_id: int = None):
    """Registra un request en la tabla events. Best effort: si la DB está
    lockeada o falla cualquier cosa, se descarta el evento sin romper nada."""
    try:
        with db_conn() as conn:
            conn.execute(
                """INSERT INTO events (ts, endpoint, method, status, duration_ms, user_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), endpoint, method, status,
                 round(float(duration_ms), 2), user_id),
            )
    except Exception:
        pass


def get_usage_stats(days: int = 30) -> dict:
    """Uso agregado por endpoint (count, latencia promedio, errores 5xx)
    más la serie diaria de requests totales."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with db_conn() as conn:
        by_endpoint = conn.execute(
            """SELECT endpoint,
                      COUNT(*) AS count,
                      ROUND(AVG(duration_ms), 2) AS avg_duration_ms,
                      SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS errors
               FROM events WHERE ts >= ?
               GROUP BY endpoint ORDER BY count DESC""",
            (cutoff,),
        ).fetchall()
        by_day = conn.execute(
            """SELECT substr(ts, 1, 10) AS date, COUNT(*) AS count
               FROM events WHERE ts >= ?
               GROUP BY date ORDER BY date ASC""",
            (cutoff,),
        ).fetchall()
    return {
        "days": days,
        "endpoints": [dict(r) for r in by_endpoint],
        "by_day": [dict(r) for r in by_day],
        "total_requests": sum(r["count"] for r in by_day),
    }


# Inicializar al importar (idempotente)
init_db()
