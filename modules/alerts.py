"""
Módulo de alertas de precio.
Las alertas se persisten en SQLite (data/portfolio.db, tabla `alerts`).
El JSON legacy (data/alerts.json) se migra automáticamente en db.init_db().
"""
from datetime import datetime

from modules import db


def _row_to_alert(row) -> dict:
    """Convierte una fila de la tabla `alerts` al dict que espera el frontend."""
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "alert_type": row["alert_type"],
        "condition": row["condition"],
        "target_price": row["target_price"],
        "pct_change": row["pct_change"],
        "reference_price": row["reference_price"],
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "triggered": bool(row["triggered"]),
        "triggered_at": row["triggered_at"],
        "triggered_price": row["triggered_price"],
    }


def get_alerts() -> list:
    with db.db_conn() as conn:
        rows = conn.execute("SELECT * FROM alerts ORDER BY id ASC").fetchall()
    return [_row_to_alert(r) for r in rows]


def create_alert(symbol: str, condition: str, target_price: float, note: str = "",
                 alert_type: str = "price", pct_change: float = None,
                 reference_price: float = None) -> dict:
    """
    Crea una nueva alerta.
    alert_type:
      - "price"    : dispara cuando precio cruza target_price (condition: above/below)
      - "pct_drop" : dispara cuando precio baja pct_change% desde reference_price
    """
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
    with db.db_conn() as conn:
        conn.execute(
            """INSERT INTO alerts
               (id, symbol, alert_type, condition, target_price, pct_change,
                reference_price, note, triggered, triggered_at, triggered_price, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?)""",
            (
                alert["id"], alert["symbol"], alert["alert_type"], alert["condition"],
                alert["target_price"], alert["pct_change"], alert["reference_price"],
                alert["note"], alert["created_at"],
            ),
        )
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
    with db.db_conn() as conn:
        cur = conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    return cur.rowcount > 0


def check_alerts(current_prices: dict) -> list:
    """
    Verifica cuáles alertas se dispararon dado un dict {symbol: price}.
    Retorna lista de alertas disparadas.
    """
    triggered = []
    with db.db_conn() as conn:
        rows = conn.execute("SELECT * FROM alerts WHERE triggered = 0 ORDER BY id ASC").fetchall()
        for row in rows:
            price = current_prices.get(row["symbol"])
            if price is None:
                continue

            fired = False
            if row["condition"] == "above" and price >= row["target_price"]:
                fired = True
            elif row["condition"] == "below" and price <= row["target_price"]:
                fired = True

            if fired:
                now = datetime.now().isoformat()
                conn.execute(
                    "UPDATE alerts SET triggered = 1, triggered_at = ?, triggered_price = ? WHERE id = ?",
                    (now, price, row["id"]),
                )
                alert = _row_to_alert(row)
                alert["triggered"] = True
                alert["triggered_at"] = now
                alert["triggered_price"] = price
                triggered.append(alert)

    return triggered
