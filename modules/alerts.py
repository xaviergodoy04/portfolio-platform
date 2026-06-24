"""
Módulo de alertas de precio.
Las alertas se persisten en data/alerts.json
"""
import json
import os
from datetime import datetime


ALERTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "alerts.json")


def _load_alerts() -> list:
    if not os.path.exists(ALERTS_FILE):
        return []
    with open(ALERTS_FILE, "r") as f:
        return json.load(f)


def _save_alerts(alerts: list):
    os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)


def get_alerts() -> list:
    return _load_alerts()


def create_alert(symbol: str, condition: str, target_price: float, note: str = "",
                 alert_type: str = "price", pct_change: float = None,
                 reference_price: float = None) -> dict:
    """
    Crea una nueva alerta.
    alert_type:
      - "price"    : dispara cuando precio cruza target_price (condition: above/below)
      - "pct_drop" : dispara cuando precio baja pct_change% desde reference_price
    """
    alerts = _load_alerts()
    alert = {
        "id": int(datetime.now().timestamp() * 1000),
        "symbol": symbol.upper(),
        "alert_type": alert_type,
        "condition": condition,          # "above" | "below"
        "target_price": target_price,    # precio absoluto de disparo
        "pct_change": pct_change,        # % configurado (solo pct_drop)
        "reference_price": reference_price,  # precio base del % (solo pct_drop)
        "note": note,
        "created_at": datetime.now().isoformat(),
        "triggered": False,
        "triggered_at": None,
        "triggered_price": None,
    }
    alerts.append(alert)
    _save_alerts(alerts)
    return alert


def create_pct_alert(symbol: str, pct: float, reference_price: float,
                     reference_type: str = "current", note: str = "") -> dict:
    """
    Crea una alerta de caída porcentual.
    reference_type: "current" (desde precio actual) | "cost" (desde costo promedio)
    """
    target = round(reference_price * (1 - pct / 100), 2)
    label = f"Baja {pct}% desde {'precio actual' if reference_type == 'current' else 'costo promedio'} (ref: ${reference_price:.2f} → objetivo: ${target:.2f})"
    note_full = f"{label}" + (f" — {note}" if note else "")
    return create_alert(
        symbol=symbol,
        condition="below",
        target_price=target,
        note=note_full,
        alert_type="pct_drop",
        pct_change=pct,
        reference_price=reference_price,
    )


def delete_alert(alert_id: int) -> bool:
    alerts = _load_alerts()
    initial_len = len(alerts)
    alerts = [a for a in alerts if a["id"] != alert_id]
    _save_alerts(alerts)
    return len(alerts) < initial_len


def check_alerts(current_prices: dict) -> list:
    """
    Verifica cuáles alertas se dispararon dado un dict {symbol: price}.
    Retorna lista de alertas disparadas.
    """
    alerts = _load_alerts()
    triggered = []

    for alert in alerts:
        if alert["triggered"]:
            continue

        symbol = alert["symbol"]
        price = current_prices.get(symbol)
        if price is None:
            continue

        fired = False
        if alert["condition"] == "above" and price >= alert["target_price"]:
            fired = True
        elif alert["condition"] == "below" and price <= alert["target_price"]:
            fired = True

        if fired:
            alert["triggered"] = True
            alert["triggered_at"] = datetime.now().isoformat()
            alert["triggered_price"] = price
            triggered.append(alert)

    _save_alerts(alerts)
    return triggered
