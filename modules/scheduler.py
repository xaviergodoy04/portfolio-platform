"""
Scheduler server-side (APScheduler BackgroundScheduler).

Jobs:
- Alertas de precio: cada 5 minutos. Lo disparado queda en un buffer que
  /api/alerts/check drena en el próximo poll del frontend (así no se pierde
  ninguna alerta aunque la detecte el server y no el navegador).
- Smart alerts: wrapper cada 60s que respeta `enabled` e `interval_minutes`
  de la config (leída de la DB en cada pasada, así los cambios aplican al toque).
- Snapshot diario del portfolio: todos los días a las 18:30 hora local, y una
  corrida inmediata al arrancar si todavía no hay snapshot de hoy.

Todos los jobs capturan sus excepciones y las loggean: jamás tumban el server.
"""
import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from modules import db

logger = logging.getLogger("scheduler")

_scheduler = None

# Buffer de alertas de precio disparadas por el scheduler, pendientes de
# entregar al frontend en el próximo /api/alerts/check.
_pending_lock = threading.Lock()
_pending_triggered = []

# Estado del job de smart alerts
_last_smart_run = None
_last_smart_result = None


# ── Job: alertas de precio ───────────────────────────────────────────────────

def check_price_alerts_job():
    """Verifica las alertas de precio activas contra cotizaciones actuales."""
    try:
        from modules.alerts import get_alerts, check_alerts
        from modules.market_data import get_quote

        alerts = get_alerts()
        symbols = {a["symbol"] for a in alerts if not a["triggered"]}
        if not symbols:
            return

        prices = {}
        for sym in symbols:
            q = get_quote(sym)
            if "error" not in q and q.get("price"):
                prices[sym] = q["price"]

        triggered = check_alerts(prices)
        if triggered:
            with _pending_lock:
                _pending_triggered.extend(triggered)
            logger.info("Alertas de precio disparadas: %s", [a["symbol"] for a in triggered])
    except Exception:
        logger.exception("Error en job de alertas de precio")


def pop_pending_triggered() -> list:
    """Drena el buffer de alertas disparadas por el scheduler (para /api/alerts/check)."""
    with _pending_lock:
        out = list(_pending_triggered)
        _pending_triggered.clear()
    return out


# ── Job: smart alerts ────────────────────────────────────────────────────────

def check_smart_alerts_job():
    """Corre el escaneo de smart alerts respetando enabled + interval_minutes."""
    global _last_smart_run, _last_smart_result
    try:
        from modules.smart_alerts import get_config, check_opportunities

        cfg = get_config()
        if not cfg.get("enabled"):
            return

        interval = max(1, int(cfg.get("interval_minutes", 30) or 30))
        now = datetime.now()
        if _last_smart_run and (now - _last_smart_run).total_seconds() < interval * 60:
            return  # todavía no pasó el intervalo configurado

        _last_smart_run = now
        result = check_opportunities()
        _last_smart_result = result
        if result.get("triggered"):
            logger.info("Smart alerts nuevas: %s", [t["symbol"] for t in result["triggered"]])
    except Exception:
        logger.exception("Error en job de smart alerts")


def get_last_smart_result():
    """Último resultado del escaneo de smart alerts hecho por el scheduler (o None)."""
    return _last_smart_result


# ── Job: snapshot diario del portfolio ───────────────────────────────────────

def daily_snapshot_job():
    """
    Snapshot diario usando el último portfolio disponible en la DB.
    Intenta refrescar precios con yfinance; si falla, usa los últimos
    valores conocidos. Nunca crashea.
    """
    try:
        data = db.load_portfolio_cache()
        if not data or not data.get("positions"):
            logger.info("Snapshot diario: no hay portfolio en la DB, se omite")
            return

        try:
            from modules.market_data import enrich_positions
            positions = enrich_positions(data["positions"])
            total_val = sum(p.get("position_value", 0) for p in positions)
            total_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
            cost_basis = total_val - total_pnl
            summary = {**(data.get("summary") or {})}
            summary.update({
                "total_value": round(total_val, 2),
                "total_unrealized_pnl": round(total_pnl, 2),
                "total_pnl_pct": round((total_pnl / cost_basis * 100) if cost_basis != 0 else 0, 2),
                "num_positions": len(positions),
            })
            data = {**data, "positions": positions, "summary": summary}
            db.save_portfolio_cache(data)
        except Exception:
            logger.warning("Snapshot diario: no se pudieron refrescar precios (yfinance); "
                           "se usan los últimos valores conocidos")

        db.upsert_snapshot(data)
        logger.info("Snapshot diario guardado (%s)", datetime.now().strftime("%Y-%m-%d"))
    except Exception:
        logger.exception("Error en job de snapshot diario")


# ── Inicialización ───────────────────────────────────────────────────────────

def init_scheduler():
    """Arranca el BackgroundScheduler con los tres jobs. Idempotente."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(daemon=True)

    # Alertas de precio: cada 5 minutos
    sched.add_job(check_price_alerts_job, "interval", minutes=5,
                  id="price_alerts", max_instances=1, coalesce=True)

    # Smart alerts: wrapper cada 60s que respeta interval_minutes de la config
    sched.add_job(check_smart_alerts_job, "interval", seconds=60,
                  id="smart_alerts", max_instances=1, coalesce=True)

    # Snapshot diario a las 18:30 hora local
    sched.add_job(daily_snapshot_job, CronTrigger(hour=18, minute=30),
                  id="daily_snapshot", max_instances=1, coalesce=True)

    sched.start()
    _scheduler = sched

    # Si todavía no hay snapshot de hoy, correr uno ahora (en background)
    try:
        if not db.has_snapshot_today():
            sched.add_job(daily_snapshot_job, id="snapshot_startup")
    except Exception:
        logger.exception("No se pudo encolar el snapshot inicial")

    logger.info("Scheduler iniciado con jobs: %s", [j.id for j in sched.get_jobs()])
    print(f"⏰ Scheduler activo: {[j.id for j in sched.get_jobs()]}")
    return sched
