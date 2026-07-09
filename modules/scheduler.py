"""
Scheduler server-side (APScheduler BackgroundScheduler).

Jobs (todos iteran db.get_all_user_ids() — cada usuario con su propio
portfolio, alertas y config):
- Alertas de precio: cada 5 minutos. Junta los símbolos de TODAS las alertas
  activas de TODOS los usuarios en un solo batch de quotes (evita pedir el
  mismo precio varias veces), y evalúa las alertas de cada usuario contra
  ese batch. Lo disparado queda en un buffer por usuario que
  /api/alerts/check drena en el próximo poll del frontend (así no se pierde
  ninguna alerta aunque la detecte el server y no el navegador).
- Smart alerts: wrapper cada 60s que corre por usuario respetando su propio
  `enabled` e `interval_minutes` (leídos de la DB en cada pasada, así los
  cambios aplican al toque).
- Snapshot diario del portfolio: todos los días a las 18:30 hora local (por
  usuario, salteando a quien ya tenga el de hoy), y una corrida inmediata al
  arrancar.
- Backup diario de la DB: todos los días a las 18:45 (después del snapshot).
  Es un solo archivo SQLite: un backup cubre a todos los usuarios.
- Health check de fuentes de noticias: todos los lunes a las 08:00. Persiste
  el estado de cada feed RSS en la tabla feed_health (compartida, visible en
  Ajustes) — no depende de usuario.

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

# Buffer de alertas de precio disparadas por el scheduler, por usuario,
# pendientes de entregar al frontend en el próximo /api/alerts/check.
_pending_lock = threading.Lock()
_pending_triggered = {}  # {user_id: [alertas]}

# Estado del job de smart alerts, por usuario
_last_smart_run = {}      # {user_id: datetime}
_last_smart_result = {}   # {user_id: dict}


# ── Job: alertas de precio ───────────────────────────────────────────────────

def check_price_alerts_job():
    """Verifica las alertas de precio activas de todos los usuarios contra
    cotizaciones actuales (un solo batch de quotes para todos)."""
    try:
        from modules.alerts import get_alerts, check_alerts
        from modules.market_data import get_quote

        user_ids = db.get_all_user_ids()
        alerts_by_user = {uid: get_alerts(uid) for uid in user_ids}
        symbols = {
            a["symbol"]
            for alerts in alerts_by_user.values()
            for a in alerts if not a["triggered"]
        }
        if not symbols:
            return

        prices = {}
        for sym in symbols:
            q = get_quote(sym)
            if "error" not in q and q.get("price"):
                prices[sym] = q["price"]
        if not prices:
            return

        for uid in user_ids:
            if not any(not a["triggered"] and a["symbol"] in prices for a in alerts_by_user[uid]):
                continue
            triggered = check_alerts(uid, prices)
            if triggered:
                with _pending_lock:
                    _pending_triggered.setdefault(uid, []).extend(triggered)
                logger.info("Alertas de precio disparadas (user %s): %s",
                            uid, [a["symbol"] for a in triggered])
    except Exception:
        logger.exception("Error en job de alertas de precio")


def pop_pending_triggered(user_id: int) -> list:
    """Drena el buffer de alertas disparadas por el scheduler para un usuario
    (para /api/alerts/check)."""
    with _pending_lock:
        out = _pending_triggered.pop(user_id, [])
    return out


# ── Job: smart alerts ────────────────────────────────────────────────────────

def check_smart_alerts_job():
    """Corre el escaneo de smart alerts por usuario, respetando su propio
    enabled + interval_minutes."""
    try:
        from modules.smart_alerts import get_config, check_opportunities

        now = datetime.now()
        for uid in db.get_all_user_ids():
            try:
                cfg = get_config(uid)
                if not cfg.get("enabled"):
                    continue

                interval = max(1, int(cfg.get("interval_minutes", 30) or 30))
                last_run = _last_smart_run.get(uid)
                if last_run and (now - last_run).total_seconds() < interval * 60:
                    continue  # todavía no pasó el intervalo configurado

                _last_smart_run[uid] = now
                result = check_opportunities(uid)
                _last_smart_result[uid] = result
                if result.get("triggered"):
                    logger.info("Smart alerts nuevas (user %s): %s",
                                uid, [t["symbol"] for t in result["triggered"]])
            except Exception:
                logger.exception("Error en smart alerts del usuario %s", uid)
    except Exception:
        logger.exception("Error en job de smart alerts")


def get_last_smart_result(user_id: int):
    """Último resultado del escaneo de smart alerts de un usuario (o None)."""
    return _last_smart_result.get(user_id)


# ── Job: snapshot diario del portfolio ───────────────────────────────────────

def daily_snapshot_job():
    """
    Snapshot diario por usuario, usando el último portfolio disponible en la
    DB. Intenta refrescar precios con yfinance; si falla, usa los últimos
    valores conocidos. Salta a quien ya tenga el snapshot de hoy o no tenga
    portfolio cargado. Nunca crashea.
    """
    for uid in db.get_all_user_ids():
        try:
            if db.has_snapshot_today(uid):
                continue

            data = db.load_portfolio_cache(uid)
            if not data or not data.get("positions"):
                continue

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
                db.save_portfolio_cache(uid, data)
            except Exception:
                logger.warning("Snapshot diario (user %s): no se pudieron refrescar precios "
                               "(yfinance); se usan los últimos valores conocidos", uid)

            db.upsert_snapshot(uid, data)
            logger.info("Snapshot diario guardado (user %s, %s)",
                        uid, datetime.now().strftime("%Y-%m-%d"))
        except Exception:
            logger.exception("Error en job de snapshot diario del usuario %s", uid)


# ── Job: backup diario de la DB ──────────────────────────────────────────────

def daily_backup_job():
    """Backup diario de la DB a data/backups/ con rotación. Nunca crashea."""
    try:
        path = db.backup_db()
        if path:
            logger.info("Backup diario de la DB guardado en %s", path)
        else:
            logger.info("Backup diario: no existe la DB todavía, se omite")
    except Exception:
        logger.exception("Error en job de backup diario de la DB")


# ── Job: health check de fuentes de noticias ─────────────────────────────────

def feed_health_job():
    """Chequea todas las fuentes RSS de noticias y persiste su estado. Nunca crashea."""
    try:
        from modules.news.health import check_all_feeds
        results = check_all_feeds()
        down = [r["source_name"] for r in results if r["status"] != "ok"]
        if down:
            logger.warning("Health check de feeds: %d fuentes caídas: %s", len(down), down)
        else:
            logger.info("Health check de feeds: %d fuentes OK", len(results))
    except Exception:
        logger.exception("Error en job de health check de feeds")


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

    # Backup diario de la DB a las 18:45 (después del snapshot)
    sched.add_job(daily_backup_job, CronTrigger(hour=18, minute=45),
                  id="daily_backup", max_instances=1, coalesce=True)

    # Health check semanal de fuentes de noticias: lunes 08:00
    sched.add_job(feed_health_job, CronTrigger(day_of_week="mon", hour=8, minute=0),
                  id="feed_health", max_instances=1, coalesce=True)

    sched.start()
    _scheduler = sched

    # Correr un snapshot ahora (en background): el job ya salta por usuario a
    # quien ya tenga el de hoy, así que no hace falta chequear antes de encolar.
    try:
        sched.add_job(daily_snapshot_job, id="snapshot_startup")
    except Exception:
        logger.exception("No se pudo encolar el snapshot inicial")

    # Si todavía no hay backup de hoy, correr uno ahora (en background)
    try:
        if not db.has_backup_today():
            sched.add_job(daily_backup_job, id="backup_startup")
    except Exception:
        logger.exception("No se pudo encolar el backup inicial")

    logger.info("Scheduler iniciado con jobs: %s", [j.id for j in sched.get_jobs()])
    print(f"⏰ Scheduler activo: {[j.id for j in sched.get_jobs()]}")
    return sched
