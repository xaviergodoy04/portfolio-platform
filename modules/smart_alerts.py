"""
Smart Alerts — detecta automáticamente activos con alto potencial
que alcanzan un buen punto de entrada técnico.
Persistencia en SQLite (tablas `smart_alerts` y `smart_config`), aisladas
por user_id. Los JSON legacy se migran automáticamente en db.init_db()
bajo el usuario 1.
"""
import json
from datetime import datetime

from modules import db
from modules.radar import scan, UNIVERSE, ALL_SYMBOLS

# Defaults configurables desde el frontend
DEFAULT_CONFIG = {
    "enabled": True,
    "interval_minutes": 30,
    "min_growth_score": 55,   # potencial mínimo para considerar el activo
    "min_entry_score": 55,    # entrada mínima para disparar la notificación
    "extra_symbols": [],      # símbolos extra del usuario
    "notify_seen": [],        # IDs ya vistos para no repetir
}


def _load_config(user_id: int) -> dict:
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT key, value_json FROM smart_config WHERE user_id = ?", (user_id,)
        ).fetchall()
    stored = {}
    for r in rows:
        try:
            stored[r["key"]] = json.loads(r["value_json"])
        except Exception:
            continue
    return {**DEFAULT_CONFIG, **stored}


def _save_config(user_id: int, cfg: dict):
    with db.db_conn() as conn:
        for k, v in cfg.items():
            conn.execute(
                """INSERT INTO smart_config (user_id, key, value_json) VALUES (?, ?, ?)
                   ON CONFLICT(user_id, key) DO UPDATE SET value_json = excluded.value_json""",
                (user_id, k, json.dumps(v)),
            )


def get_config(user_id: int) -> dict:
    return _load_config(user_id)


def update_config(user_id: int, updates: dict) -> dict:
    cfg = _load_config(user_id)
    cfg.update(updates)
    _save_config(user_id, cfg)
    return cfg


def _row_to_entry(row) -> dict:
    """Reconstruye el dict completo desde payload_json, con `seen` de la columna."""
    try:
        entry = json.loads(row["payload_json"]) if row["payload_json"] else {}
    except Exception:
        entry = {}
    entry.setdefault("id", row["id"])
    entry.setdefault("symbol", row["symbol"])
    entry.setdefault("detected_at", row["detected_at"])
    entry["seen"] = bool(row["seen"])
    return entry


def _insert_entry(conn, user_id: int, entry: dict):
    conn.execute(
        """INSERT OR REPLACE INTO smart_alerts
           (id, user_id, symbol, detected_at, price_at_detection, entry_score,
            growth_score, risk_score, seen, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry["id"], user_id, entry["symbol"], entry.get("detected_at"),
            entry.get("current_price"), entry.get("entry_score"),
            entry.get("growth_score"), entry.get("risk_score"),
            1 if entry.get("seen") else 0, json.dumps(entry),
        ),
    )


def _make_id(user_id: int, symbol: str) -> str:
    """ID único por usuario + símbolo + día — dos usuarios con el mismo
    símbolo el mismo día no se pisan la entrada (bug real antes de user_id)."""
    return f"{user_id}_{symbol}_{datetime.now().strftime('%Y-%m-%d')}"


def check_opportunities(user_id: int) -> dict:
    """
    Escanea el mercado y retorna activos que cumplen los umbrales de un usuario.
    Solo notifica activos que no fueron notificados hoy a ESE usuario.
    """
    cfg = _load_config(user_id)

    if not cfg.get("enabled"):
        return {"triggered": [], "scanned": 0, "config": cfg}

    min_growth = cfg.get("min_growth_score", 55)
    min_entry  = cfg.get("min_entry_score", 55)
    extra      = cfg.get("extra_symbols", [])

    # Las señales automáticas también vigilan la watchlist del usuario
    # (dedupe preservando el orden; scan() ya evita duplicar contra el universo)
    extra = list(dict.fromkeys(
        [str(s).strip().upper() for s in [*extra, *db.get_watchlist_symbols(user_id)] if str(s).strip()]
    ))

    # Escanear solo los símbolos del universo + extras (no hacer full scan siempre)
    data = scan(extra)
    results = data.get("results", [])

    with db.db_conn() as conn:
        seen_ids = {
            r["id"] for r in conn.execute(
                "SELECT id FROM smart_alerts WHERE user_id = ?", (user_id,)
            ).fetchall()
        }

    triggered = []
    for r in results:
        if r.get("growth_score", 0) >= min_growth and r.get("entry_score", 0) >= min_entry:
            alert_id = _make_id(user_id, r["symbol"])
            if alert_id in seen_ids:
                continue  # ya notificado hoy

            entry = {
                "id": alert_id,
                "symbol": r["symbol"],
                "name": r.get("name", ""),
                "entry_score": r["entry_score"],
                "growth_score": r["growth_score"],
                "risk_score": r["risk_score"],
                "risk_label": r.get("risk_label", ""),
                "growth_label": r.get("growth_label", ""),
                "current_price": r.get("current_price"),
                "drop_52w_pct": r.get("drop_52w_pct"),
                "drop_30d_pct": r.get("drop_30d_pct"),
                "rsi": r.get("rsi"),
                "analyst_upside_pct": r.get("analyst_upside_pct"),
                "analyst_target": r.get("analyst_target"),
                "revenue_growth_pct": r.get("revenue_growth_pct"),
                "sector_label": r.get("sector_label", ""),
                "detected_at": datetime.now().isoformat(),
                "seen": False,
            }
            triggered.append(entry)

    if triggered:
        with db.db_conn() as conn:
            for entry in triggered:
                _insert_entry(conn, user_id, entry)

    return {
        "triggered": triggered,
        "scanned": data.get("scanned", 0),
        "config": cfg,
        "checked_at": datetime.now().isoformat(),
    }


def get_unseen(user_id: int) -> list:
    """Retorna alertas no vistas todavía de un usuario."""
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM smart_alerts WHERE user_id = ? AND seen = 0", (user_id,)
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def mark_seen(user_id: int, alert_ids: list):
    """Marca alertas de un usuario como vistas."""
    if not alert_ids:
        return
    placeholders = ",".join("?" for _ in alert_ids)
    with db.db_conn() as conn:
        conn.execute(
            f"UPDATE smart_alerts SET seen = 1 WHERE user_id = ? AND id IN ({placeholders})",
            [user_id, *alert_ids],
        )


def get_history(user_id: int, limit: int = 50) -> list:
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM smart_alerts WHERE user_id = ? ORDER BY detected_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [_row_to_entry(r) for r in rows]
