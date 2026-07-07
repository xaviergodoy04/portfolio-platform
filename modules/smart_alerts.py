"""
Smart Alerts — detecta automáticamente activos con alto potencial
que alcanzan un buen punto de entrada técnico.
Persistencia en SQLite (tablas `smart_alerts` y `smart_config`).
Los JSON legacy se migran automáticamente en db.init_db().
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


def _load_config() -> dict:
    with db.db_conn() as conn:
        rows = conn.execute("SELECT key, value_json FROM smart_config").fetchall()
    stored = {}
    for r in rows:
        try:
            stored[r["key"]] = json.loads(r["value_json"])
        except Exception:
            continue
    return {**DEFAULT_CONFIG, **stored}


def _save_config(cfg: dict):
    with db.db_conn() as conn:
        for k, v in cfg.items():
            conn.execute(
                """INSERT INTO smart_config (key, value_json) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json""",
                (k, json.dumps(v)),
            )


def get_config() -> dict:
    return _load_config()


def update_config(updates: dict) -> dict:
    cfg = _load_config()
    cfg.update(updates)
    _save_config(cfg)
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


def _insert_entry(conn, entry: dict):
    conn.execute(
        """INSERT OR REPLACE INTO smart_alerts
           (id, symbol, detected_at, price_at_detection, entry_score,
            growth_score, risk_score, seen, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry["id"], entry["symbol"], entry.get("detected_at"),
            entry.get("current_price"), entry.get("entry_score"),
            entry.get("growth_score"), entry.get("risk_score"),
            1 if entry.get("seen") else 0, json.dumps(entry),
        ),
    )


def _make_id(symbol: str) -> str:
    """ID único por símbolo + día, para no notificar el mismo activo más de una vez por día."""
    return f"{symbol}_{datetime.now().strftime('%Y-%m-%d')}"


def check_opportunities() -> dict:
    """
    Escanea el mercado y retorna activos que cumplen los umbrales.
    Solo notifica activos que no fueron notificados hoy.
    """
    cfg = _load_config()

    if not cfg.get("enabled"):
        return {"triggered": [], "scanned": 0, "config": cfg}

    min_growth = cfg.get("min_growth_score", 55)
    min_entry  = cfg.get("min_entry_score", 55)
    extra      = cfg.get("extra_symbols", [])

    # Escanear solo los símbolos del universo + extras (no hacer full scan siempre)
    data = scan(extra)
    results = data.get("results", [])

    with db.db_conn() as conn:
        seen_ids = {r["id"] for r in conn.execute("SELECT id FROM smart_alerts").fetchall()}

    triggered = []
    for r in results:
        if r.get("growth_score", 0) >= min_growth and r.get("entry_score", 0) >= min_entry:
            alert_id = _make_id(r["symbol"])
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
                _insert_entry(conn, entry)

    return {
        "triggered": triggered,
        "scanned": data.get("scanned", 0),
        "config": cfg,
        "checked_at": datetime.now().isoformat(),
    }


def get_unseen() -> list:
    """Retorna alertas no vistas todavía."""
    with db.db_conn() as conn:
        rows = conn.execute("SELECT * FROM smart_alerts WHERE seen = 0").fetchall()
    return [_row_to_entry(r) for r in rows]


def mark_seen(alert_ids: list):
    """Marca alertas como vistas."""
    if not alert_ids:
        return
    placeholders = ",".join("?" for _ in alert_ids)
    with db.db_conn() as conn:
        conn.execute(
            f"UPDATE smart_alerts SET seen = 1 WHERE id IN ({placeholders})",
            list(alert_ids),
        )


def get_history(limit: int = 50) -> list:
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM smart_alerts ORDER BY detected_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_entry(r) for r in rows]
