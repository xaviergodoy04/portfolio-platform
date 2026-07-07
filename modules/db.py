"""
Capa de persistencia SQLite (stdlib sqlite3, sin ORM).

- DB en data/portfolio.db (ignorada por git).
- init_db() es idempotente y migra automáticamente los JSON legacy
  (data/alerts.json, data/smart_alerts.json, data/smart_config.json)
  la primera vez, sin borrarlos (quedan como backup).
- Acceso serializado con un lock global: suficiente para una app personal
  con pocos hilos (Flask + scheduler).
"""
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
    date TEXT UNIQUE NOT NULL,
    total_value REAL,
    total_pnl REAL,
    pnl_pct REAL,
    positions_json TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_cache (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
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
    key TEXT PRIMARY KEY,
    value_json TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    endpoint TEXT,
    method TEXT,
    status INTEGER,
    duration_ms REAL
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    added_at TEXT,
    note TEXT DEFAULT ''
);
"""


def init_db():
    """Crea las tablas si no existen y migra los JSON legacy una sola vez."""
    with db_conn() as conn:
        conn.executescript(_SCHEMA)
        _migrate_from_json(conn)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _migrate_from_json(conn: sqlite3.Connection):
    """Importa los JSON legacy solo si la tabla correspondiente está vacía.
    Los archivos JSON NO se borran: quedan como backup."""

    # Alertas de precio
    if conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0:
        data = _read_json(ALERTS_JSON)
        if data:
            for a in data:
                conn.execute(
                    """INSERT OR IGNORE INTO alerts
                       (id, symbol, alert_type, condition, target_price, pct_change,
                        reference_price, note, triggered, triggered_at, triggered_price, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                       (id, symbol, detected_at, price_at_detection, entry_score,
                        growth_score, risk_score, seen, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    "INSERT OR IGNORE INTO smart_config (key, value_json) VALUES (?, ?)",
                    (k, json.dumps(v)),
                )


# ── Portfolio: cache persistente + snapshots diarios ─────────────────────────

def save_portfolio_cache(data: dict):
    """Guarda el último portfolio completo (sobrevive reinicios del server)."""
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO portfolio_cache (id, payload_json, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 payload_json = excluded.payload_json,
                 updated_at = excluded.updated_at""",
            (json.dumps(data), datetime.now().isoformat()),
        )


def load_portfolio_cache():
    """Retorna el último portfolio guardado, o None si no hay."""
    with db_conn() as conn:
        row = conn.execute("SELECT payload_json FROM portfolio_cache WHERE id = 1").fetchone()
    if not row or not row["payload_json"]:
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception:
        return None


def upsert_snapshot(data: dict, date: str = None):
    """Upsert del snapshot del día a partir de un portfolio ya calculado."""
    summary = data.get("summary", {}) or {}
    date = date or datetime.now().strftime("%Y-%m-%d")
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO portfolio_snapshots
               (date, total_value, total_pnl, pnl_pct, positions_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 total_value = excluded.total_value,
                 total_pnl = excluded.total_pnl,
                 pnl_pct = excluded.pnl_pct,
                 positions_json = excluded.positions_json,
                 created_at = excluded.created_at""",
            (
                date,
                summary.get("total_value"),
                summary.get("total_unrealized_pnl"),
                summary.get("total_pnl_pct"),
                json.dumps(data.get("positions", [])),
                datetime.now().isoformat(),
            ),
        )


def get_snapshots(days: int = 90) -> list:
    """Snapshots de los últimos N días, ordenados por fecha ascendente."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT date, total_value, total_pnl, pnl_pct
               FROM portfolio_snapshots WHERE date >= ? ORDER BY date ASC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def has_snapshot_today() -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    with db_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM portfolio_snapshots WHERE date = ?", (today,)
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


# ── Watchlist ────────────────────────────────────────────────────────────────

def get_watchlist() -> list:
    """Entradas de la watchlist, la más reciente primero."""
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT symbol, added_at, note FROM watchlist ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_watchlist_symbols() -> list:
    """Solo los símbolos (para sumarlos al radar y a las smart alerts)."""
    with db_conn() as conn:
        rows = conn.execute("SELECT symbol FROM watchlist").fetchall()
    return [r["symbol"] for r in rows]


def add_to_watchlist(symbol: str, note: str = "") -> dict:
    """Agrega un símbolo a la watchlist. Si ya existe, retorna la entrada actual."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT symbol, added_at, note FROM watchlist WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row:
            return dict(row)
        entry = {"symbol": symbol, "added_at": datetime.now().isoformat(), "note": note or ""}
        conn.execute(
            "INSERT INTO watchlist (symbol, added_at, note) VALUES (?, ?, ?)",
            (entry["symbol"], entry["added_at"], entry["note"]),
        )
    return entry


def remove_from_watchlist(symbol: str) -> bool:
    """Quita un símbolo de la watchlist. True si existía."""
    with db_conn() as conn:
        cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
        return cur.rowcount > 0


# ── Instrumentación de uso de endpoints ──────────────────────────────────────

def log_event(endpoint: str, method: str, status: int, duration_ms: float):
    """Registra un request en la tabla events. Best effort: si la DB está
    lockeada o falla cualquier cosa, se descarta el evento sin romper nada."""
    try:
        with db_conn() as conn:
            conn.execute(
                """INSERT INTO events (ts, endpoint, method, status, duration_ms)
                   VALUES (?, ?, ?, ?, ?)""",
                (datetime.now().isoformat(), endpoint, method, status,
                 round(float(duration_ms), 2)),
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
