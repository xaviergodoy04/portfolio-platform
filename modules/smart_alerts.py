"""
Smart Alerts — detecta automáticamente activos con alto potencial
que alcanzan un buen punto de entrada técnico.
"""
import json
import os
from datetime import datetime
from modules.radar import scan, UNIVERSE, ALL_SYMBOLS

SMART_ALERTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "smart_alerts.json"
)

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
    cfg_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "smart_config.json")
    if os.path.exists(cfg_file):
        with open(cfg_file) as f:
            stored = json.load(f)
            return {**DEFAULT_CONFIG, **stored}
    return DEFAULT_CONFIG.copy()


def _save_config(cfg: dict):
    cfg_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "smart_config.json")
    os.makedirs(os.path.dirname(cfg_file), exist_ok=True)
    with open(cfg_file, "w") as f:
        json.dump(cfg, f, indent=2)


def get_config() -> dict:
    return _load_config()


def update_config(updates: dict) -> dict:
    cfg = _load_config()
    cfg.update(updates)
    _save_config(cfg)
    return cfg


def _load_history() -> list:
    if not os.path.exists(SMART_ALERTS_FILE):
        return []
    with open(SMART_ALERTS_FILE) as f:
        return json.load(f)


def _save_history(history: list):
    os.makedirs(os.path.dirname(SMART_ALERTS_FILE), exist_ok=True)
    with open(SMART_ALERTS_FILE, "w") as f:
        json.dump(history, f, indent=2)


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

    history = _load_history()
    seen_ids = {h["id"] for h in history}

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
            history.append(entry)

    if triggered:
        _save_history(history)

    return {
        "triggered": triggered,
        "scanned": data.get("scanned", 0),
        "config": cfg,
        "checked_at": datetime.now().isoformat(),
    }


def get_unseen() -> list:
    """Retorna alertas no vistas todavía."""
    history = _load_history()
    return [h for h in history if not h.get("seen")]


def mark_seen(alert_ids: list):
    """Marca alertas como vistas."""
    history = _load_history()
    for h in history:
        if h["id"] in alert_ids:
            h["seen"] = True
    _save_history(history)


def get_history(limit: int = 50) -> list:
    history = _load_history()
    return sorted(history, key=lambda x: x.get("detected_at", ""), reverse=True)[:limit]
