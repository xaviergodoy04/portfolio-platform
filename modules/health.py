"""
Panel único de salud: las 3 dependencias externas críticas en un solo lugar.

Extiende el patrón del health check de feeds (que se pagó solo detectando
The Verge/Wired caídas) a los otros dos proveedores externos:

  - market: el proveedor de datos de mercado (yfinance hoy) — se pide una
    quote real de SPY y se mide la latencia. Si viene con "error", está caído.
  - ai: el proveedor de IA (Groq/Anthropic) — AIProvider.status() ya hace un
    ping real a Groq cuando está configurado.
  - news_feeds: resumen de la tabla feed_health (el detalle por fuente sigue
    viviendo en /api/news/health).

Cada check es tolerante a fallos y reporta status "ok" | "degraded" | "down"
más un detalle humano. El campo `overall` agrega: down si hay algo down,
degraded si hay algo degraded, ok si todo ok.

Nota de costo: /api/health con ?refresh=false usa el cache de quotes de
market_data (TTL 120 s) — es barato llamarlo al entrar a Ajustes.
"""
import time
from datetime import datetime

import config
from modules import db

_STATUS_RANK = {"ok": 0, "degraded": 1, "down": 2}


def _check_market() -> dict:
    """Pide una quote real de SPY al proveedor activo y mide la latencia."""
    from modules.market_data import get_quote

    t0 = time.perf_counter()
    try:
        q = get_quote("SPY")
        latency_ms = round((time.perf_counter() - t0) * 1000)
        if "error" in q or not q.get("price"):
            return {
                "status": "down",
                "detail": q.get("error", "SPY sin precio válido"),
                "latency_ms": latency_ms,
            }
        return {
            "status": "ok",
            "detail": f"SPY ${q['price']} · {latency_ms} ms",
            "latency_ms": latency_ms,
        }
    except Exception as e:
        return {"status": "down", "detail": f"Excepción: {e}", "latency_ms": None}


def _check_ai() -> dict:
    """Estado del proveedor de IA usando AIProvider.status() (ping real a Groq)."""
    from modules.ai_provider import AIProvider

    try:
        provider = AIProvider(
            provider=config.AI_PROVIDER,
            groq_api_key=config.GROQ_API_KEY,
            groq_model_analysis=config.GROQ_MODEL_ANALYSIS,
            groq_model_fast=config.GROQ_MODEL_FAST,
            anthropic_api_key=config.ANTHROPIC_API_KEY,
            anthropic_model=config.CLAUDE_MODEL,
        )
        st = provider.status()
    except Exception as e:
        return {"status": "down", "detail": f"Excepción: {e}"}

    active = st.get("active_provider", "none")
    if active == "none":
        return {"status": "down", "detail": "Sin proveedor configurado (ni Groq ni Anthropic)"}

    groq = st.get("groq", {})
    anthropic_ok = st.get("anthropic", {}).get("configured", False)
    if active == "groq" and groq.get("connected") is False:
        # Groq configurado pero sin conexión: si hay fallback a Anthropic es
        # degradado, si no hay fallback está caído.
        if anthropic_ok and config.AI_PROVIDER == "auto":
            return {"status": "degraded",
                    "detail": "Groq sin conexión — operando con fallback a Anthropic"}
        return {"status": "down", "detail": "Groq configurado pero sin conexión"}

    detail = f"activo: {active}"
    if active == "groq" and groq.get("connected"):
        detail += " · conectado ✓"
        if anthropic_ok:
            detail += " · fallback Anthropic listo"
    return {"status": "ok", "detail": detail}


def _check_news_feeds() -> dict:
    """Resumen de la tabla feed_health (poblada por el job semanal de los lunes)."""
    try:
        rows = db.get_feed_health()
    except Exception as e:
        return {"status": "down", "detail": f"Excepción: {e}"}

    if not rows:
        return {"status": "degraded",
                "detail": "Sin datos todavía — el chequeo corre los lunes 08:00 (o desde la tarjeta de fuentes)"}

    down = [r["source_name"] for r in rows if r["status"] != "ok"]
    checked = rows[0].get("last_checked") or ""
    suffix = f" · último chequeo {checked[:10]}" if checked else ""
    if not down:
        return {"status": "ok", "detail": f"{len(rows)} fuentes OK{suffix}"}
    # Fuentes caídas degradan (hay redundancia por sección); todas caídas = down
    status = "down" if len(down) == len(rows) else "degraded"
    return {"status": status,
            "detail": f"{len(down)}/{len(rows)} caídas: {', '.join(down[:4])}{'…' if len(down) > 4 else ''}{suffix}"}


def _check_scheduler() -> dict:
    """Jobs internos: confirma que el scheduler esté vivo (clave bajo launchd)."""
    from modules import scheduler as sched_mod

    sched = sched_mod._scheduler
    if sched is None or not sched.running:
        return {"status": "down",
                "detail": "Scheduler no iniciado — snapshots/backups/alertas no van a correr"}
    jobs = sched.get_jobs()
    return {"status": "ok", "detail": f"{len(jobs)} jobs activos: {', '.join(j.id for j in jobs)}"}


def get_health() -> dict:
    """Corre los 4 checks y agrega el estado general."""
    checks = {
        "market": {"name": f"Datos de mercado ({config.MARKET_PROVIDER})", **_check_market()},
        "ai": {"name": f"Proveedor IA ({config.AI_PROVIDER})", **_check_ai()},
        "news_feeds": {"name": "Fuentes de noticias", **_check_news_feeds()},
        "scheduler": {"name": "Scheduler interno", **_check_scheduler()},
    }
    worst = max((c["status"] for c in checks.values()),
                key=lambda s: _STATUS_RANK.get(s, 2))
    return {
        "overall": worst,
        "checks": checks,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
