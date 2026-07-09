"""
Track record de smart alerts — mide cómo evolucionó cada oportunidad
detectada desde su detección, comparada contra SPY en el mismo período.

- Precio actual por alerta vía get_quote (cacheado en market_data).
- SPY se baja UNA sola vez (get_history) y se reutiliza para todas las
  alertas: el retorno de SPY se calcula entre el cierre en la fecha de
  detección y el último cierre disponible.
- El resultado completo se cachea 15 minutos en memoria para no golpear
  yfinance en cada visita al dashboard.
"""
import threading
import time
from bisect import bisect_left
from datetime import datetime

from modules import db
from modules.market_data import get_quote, get_history

TRACK_RECORD_TTL = 15 * 60  # 15 minutos

_cache_lock = threading.Lock()
_cached = {}  # {user_id: (timestamp, result)}


def clear_cache():
    """Vacía el cache del track record (útil para tests)."""
    global _cached
    with _cache_lock:
        _cached = {}


def _empty_summary(total: int = 0) -> dict:
    return {
        "total": total,
        "win_rate_vs_spy": None,
        "avg_return": None,
        "avg_alpha": None,
        "best": None,
        "worst": None,
    }


def _load_alerts(user_id: int) -> list:
    """Todas las smart alerts del historial de un usuario, más recientes primero."""
    with db.db_conn() as conn:
        rows = conn.execute(
            """SELECT id, symbol, detected_at, price_at_detection,
                      entry_score, growth_score, risk_score
               FROM smart_alerts WHERE user_id = ? ORDER BY detected_at DESC""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_dt(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _spy_period_for(oldest_days: float) -> str:
    """Período de historial de SPY suficiente para cubrir la alerta más vieja."""
    if oldest_days <= 80:
        return "3mo"
    if oldest_days <= 170:
        return "6mo"
    if oldest_days <= 350:
        return "1y"
    if oldest_days <= 715:
        return "2y"
    if oldest_days <= 1790:
        return "5y"
    return "max"


def _spy_close_at(dates: list, closes: list, date_str: str):
    """Cierre de SPY en la fecha dada (o el primer día hábil siguiente).
    Si la fecha es posterior al último dato, usa el último cierre."""
    idx = bisect_left(dates, date_str)
    if idx >= len(dates):
        idx = len(dates) - 1
    return closes[idx]


def _build_track_record(user_id: int) -> dict:
    now = datetime.now()
    raw_alerts = _load_alerts(user_id)

    if not raw_alerts:
        return {
            "alerts": [],
            "summary": _empty_summary(0),
            "generated_at": now.isoformat(),
        }

    # SPY una sola vez, con período suficiente para la alerta más vieja
    detected_dts = [_parse_dt(a.get("detected_at")) for a in raw_alerts]
    oldest_days = max(
        ((now - dt).days for dt in detected_dts if dt is not None), default=0
    )
    spy = get_history("SPY", _spy_period_for(oldest_days))
    spy_ok = "error" not in spy and bool(spy.get("close"))
    spy_dates = spy.get("dates", []) if spy_ok else []
    spy_closes = spy.get("close", []) if spy_ok else []
    spy_last = spy_closes[-1] if spy_ok else None

    alerts = []
    for row, detected_dt in zip(raw_alerts, detected_dts):
        symbol = row["symbol"]
        price_then = row.get("price_at_detection")
        days = (now - detected_dt).days if detected_dt else None

        # Precio actual (cacheado 120 s en market_data)
        quote = get_quote(symbol)
        current_price = quote.get("price") if "error" not in quote else None

        return_pct = None
        if current_price and price_then:
            return_pct = round((current_price - price_then) / price_then * 100, 2)

        spy_return_pct = None
        if spy_ok and detected_dt is not None:
            spy_then = _spy_close_at(spy_dates, spy_closes, detected_dt.strftime("%Y-%m-%d"))
            if spy_then:
                spy_return_pct = round((spy_last - spy_then) / spy_then * 100, 2)

        alpha = None
        if return_pct is not None and spy_return_pct is not None:
            alpha = round(return_pct - spy_return_pct, 2)

        alerts.append({
            "id": row["id"],
            "symbol": symbol,
            "detected_at": row.get("detected_at"),
            "days_elapsed": days,
            "price_at_detection": price_then,
            "current_price": current_price,
            "return_pct": return_pct,
            "spy_return_pct": spy_return_pct,
            "alpha": alpha,
            "entry_score": row.get("entry_score"),
            "growth_score": row.get("growth_score"),
            "risk_score": row.get("risk_score"),
        })

    # ── Agregados ────────────────────────────────────────────────────────────
    summary = _empty_summary(len(alerts))

    with_return = [a for a in alerts if a["return_pct"] is not None]
    if with_return:
        summary["avg_return"] = round(
            sum(a["return_pct"] for a in with_return) / len(with_return), 2
        )
        best = max(with_return, key=lambda a: a["return_pct"])
        worst = min(with_return, key=lambda a: a["return_pct"])
        summary["best"] = {
            "symbol": best["symbol"], "detected_at": best["detected_at"],
            "return_pct": best["return_pct"], "alpha": best["alpha"],
        }
        summary["worst"] = {
            "symbol": worst["symbol"], "detected_at": worst["detected_at"],
            "return_pct": worst["return_pct"], "alpha": worst["alpha"],
        }

    with_alpha = [a for a in alerts if a["alpha"] is not None]
    if with_alpha:
        summary["avg_alpha"] = round(
            sum(a["alpha"] for a in with_alpha) / len(with_alpha), 2
        )

    # Win rate vs SPY: solo alertas con al menos 1 día de antigüedad
    # (las detectadas hoy todavía no tienen recorrido para medirse)
    mature = [
        a for a in with_alpha
        if a["days_elapsed"] is not None and a["days_elapsed"] >= 1
    ]
    if mature:
        wins = sum(1 for a in mature if a["alpha"] > 0)
        summary["win_rate_vs_spy"] = round(wins / len(mature) * 100, 1)

    return {
        "alerts": alerts,
        "summary": summary,
        "generated_at": now.isoformat(),
    }


def get_track_record(user_id: int, force_refresh: bool = False) -> dict:
    """Track record de un usuario, cacheado TRACK_RECORD_TTL segundos en memoria."""
    global _cached
    with _cache_lock:
        cached = _cached.get(user_id)
        if not force_refresh and cached is not None and (time.time() - cached[0]) < TRACK_RECORD_TTL:
            return cached[1]

    result = _build_track_record(user_id)

    with _cache_lock:
        _cached[user_id] = (time.time(), result)
    return result
