"""
Backend principal - Flask API para la plataforma de inversiones.
Correr con: python app.py
Luego abrir: http://localhost:5000
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS
import threading

import config
from modules.ibkr import fetch_flex_report, parse_csv_upload
from modules.market_data import get_quote, get_history, compare_assets, enrich_positions
from modules.ai_analysis import analyze_asset, chat_analysis
from modules.ai_provider import AIProvider
from modules.alerts import get_alerts, create_alert, create_pct_alert, delete_alert, check_alerts
from modules.radar import scan as radar_scan
from modules.smart_alerts import (
    check_opportunities, get_unseen, mark_seen,
    get_history as smart_history, get_config as smart_get_config,
    update_config as smart_update_config
)
from modules.scheduler import init_scheduler, pop_pending_triggered
from modules.track_record import get_track_record
import asyncio
import time
from modules import db
from modules.news.collector import collect_all as news_collect
from modules.news.enricher import enrich_news
from modules.news import cache as news_cache

app = Flask(__name__, static_folder="static")
CORS(app)

# Cache en memoria para el portfolio (se limpia al reiniciar)
_portfolio_cache = {}

# Hidratar el cache desde la DB: un reinicio no obliga a esperar a IBKR
_stored_portfolio = db.load_portfolio_cache()
if _stored_portfolio:
    _portfolio_cache["data"] = _stored_portfolio


def _persist_portfolio(data: dict):
    """Guarda el portfolio en la DB y hace upsert del snapshot del día."""
    try:
        db.save_portfolio_cache(data)
        db.upsert_snapshot(data)
    except Exception as e:
        print(f"⚠️  Error persistiendo portfolio en la DB: {e}")


# ── Instrumentación de uso ───────────────────────────────────────────────────
# Registra cada request a /api/* en la tabla `events` (ts, endpoint, method,
# status, duration_ms). Se excluye el frontend ("/" y estáticos). Los endpoints
# de polling de alto volumen se registran con sampling 1/10 (determinístico,
# contador por endpoint) para no llenar la tabla de ruido: sus counts en
# /api/stats/usage representan ~10% del tráfico real.

_SAMPLED_ENDPOINTS = {"/api/alerts/check", "/api/smart-alerts/unseen"}
_SAMPLE_RATE = 10
_sample_lock = threading.Lock()
_sample_counters = {}


@app.before_request
def _usage_start_timer():
    g._usage_start = time.perf_counter()


@app.after_request
def _usage_log_request(response):
    """Registra el request en la DB. Best effort: jamás rompe la respuesta."""
    try:
        path = request.path
        if not path.startswith("/api/"):
            return response

        if path in _SAMPLED_ENDPOINTS:
            with _sample_lock:
                _sample_counters[path] = _sample_counters.get(path, 0) + 1
                n = _sample_counters[path]
            if n % _SAMPLE_RATE != 1:  # registra 1 de cada 10 (el 1º, 11º, ...)
                return response

        start = getattr(g, "_usage_start", None)
        duration_ms = (time.perf_counter() - start) * 1000 if start else 0.0
        # Usar la regla de la ruta (ej: /api/quote/<symbol>) para agregar
        # por endpoint y no por cada símbolo pedido
        endpoint = request.url_rule.rule if request.url_rule else path
        db.log_event(endpoint, request.method, response.status_code, duration_ms)
    except Exception:
        pass  # la instrumentación nunca debe romper un request
    return response


@app.route("/api/stats/usage")
def usage_stats():
    """Estadísticas de uso de los endpoints: count, latencia promedio,
    errores 5xx por endpoint y serie diaria de requests."""
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    return jsonify(db.get_usage_stats(days))


# ── Servir el frontend ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── Portfolio / IBKR ─────────────────────────────────────────────────────────

@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    """Obtiene portfolio desde IBKR Flex Query (o caché)."""
    force_refresh = request.args.get("refresh", "false") == "true"

    if not force_refresh and _portfolio_cache.get("data"):
        return jsonify(_portfolio_cache["data"])

    data = fetch_flex_report(config.IBKR_FLEX_TOKEN, config.IBKR_FLEX_QUERY_ID)

    if "error" not in data and data.get("positions"):
        # Enriquecer con datos de mercado actuales
        data["positions"] = enrich_positions(data["positions"])
        # Recalcular summary con precios actuales
        total_val = sum(p.get("position_value", 0) for p in data["positions"])
        total_pnl = sum(p.get("unrealized_pnl", 0) for p in data["positions"])
        cost_basis = total_val - total_pnl
        data["summary"]["total_value"] = round(total_val, 2)
        data["summary"]["total_unrealized_pnl"] = round(total_pnl, 2)
        data["summary"]["total_pnl_pct"] = round((total_pnl / cost_basis * 100) if cost_basis != 0 else 0, 2)
        _portfolio_cache["data"] = data
        _persist_portfolio(data)

    return jsonify(data)


@app.route("/api/portfolio/upload", methods=["POST"])
def upload_csv():
    """Carga portfolio desde CSV exportado de IB."""
    if "file" not in request.files:
        return jsonify({"error": "No se recibió archivo"}), 400

    file = request.files["file"]
    content = file.read().decode("utf-8", errors="replace")
    data = parse_csv_upload(content)

    if "error" not in data and data.get("positions"):
        data["positions"] = enrich_positions(data["positions"])
        _portfolio_cache["data"] = data
        _persist_portfolio(data)

    return jsonify(data)


@app.route("/api/portfolio/manual", methods=["POST"])
def set_manual_portfolio():
    """Permite ingresar posiciones manualmente (sin IBKR)."""
    body = request.get_json()
    positions = body.get("positions", [])

    if not positions:
        return jsonify({"error": "No se recibieron posiciones"}), 400

    enriched = enrich_positions(positions)
    total_val = sum(p.get("position_value", 0) for p in enriched)
    total_pnl = sum(p.get("unrealized_pnl", 0) for p in enriched)
    cost_basis = total_val - total_pnl

    data = {
        "positions": enriched,
        "summary": {
            "total_value": round(total_val, 2),
            "total_unrealized_pnl": round(total_pnl, 2),
            "total_pnl_pct": round((total_pnl / cost_basis * 100) if cost_basis != 0 else 0, 2),
            "num_positions": len(enriched),
        },
        "source": "manual"
    }
    _portfolio_cache["data"] = data
    _persist_portfolio(data)
    return jsonify(data)


@app.route("/api/portfolio/history", methods=["GET"])
def portfolio_history():
    """Historial de snapshots diarios del portfolio (para gráficos de evolución)."""
    try:
        days = int(request.args.get("days", 90))
    except ValueError:
        days = 90
    snapshots = db.get_snapshots(days)
    return jsonify({"snapshots": snapshots, "count": len(snapshots)})


# ── Market Data ──────────────────────────────────────────────────────────────

@app.route("/api/quote/<symbol>")
def quote(symbol):
    return jsonify(get_quote(symbol.upper()))


@app.route("/api/history/<symbol>")
def history(symbol):
    period = request.args.get("period", "1y")
    return jsonify(get_history(symbol.upper(), period))


@app.route("/api/compare")
def compare():
    symbols_str = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "Indicá al menos un símbolo"}), 400
    return jsonify(compare_assets(symbols))


# ── AI Analysis ──────────────────────────────────────────────────────────────

@app.route("/api/analyze/<symbol>")
def analyze(symbol):
    portfolio = _portfolio_cache.get("data")
    result = analyze_asset(symbol.upper(), config, portfolio)
    return jsonify(result)


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json()
    question = body.get("question", "").strip()
    if not question:
        return jsonify({"error": "Pregunta vacía"}), 400

    portfolio = _portfolio_cache.get("data")
    answer = chat_analysis(question, config, portfolio)
    return jsonify({"answer": answer})


# ── AI Status ───────────────────────────────────────────────────────────────

@app.route("/api/ai/status")
def ai_status():
    provider = AIProvider(
        provider=config.AI_PROVIDER,
        groq_api_key=config.GROQ_API_KEY,
        groq_model_analysis=config.GROQ_MODEL_ANALYSIS,
        groq_model_fast=config.GROQ_MODEL_FAST,
        anthropic_api_key=config.ANTHROPIC_API_KEY,
        anthropic_model=config.CLAUDE_MODEL,
    )
    return jsonify(provider.status())


# ── Alerts ───────────────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
def list_alerts():
    return jsonify(get_alerts())


@app.route("/api/alerts", methods=["POST"])
def add_alert():
    body = request.get_json()
    symbol = body.get("symbol", "").upper()
    alert_type = body.get("alert_type", "price")
    note = body.get("note", "")

    if not symbol:
        return jsonify({"error": "Símbolo requerido"}), 400

    if alert_type == "pct_drop":
        pct = body.get("pct_change")
        reference_price = body.get("reference_price")
        reference_type = body.get("reference_type", "current")
        if not pct or not reference_price:
            return jsonify({"error": "pct_change y reference_price requeridos"}), 400
        alert = create_pct_alert(symbol, float(pct), float(reference_price), reference_type, note)
    else:
        condition = body.get("condition")
        target_price = body.get("target_price")
        if condition not in ("above", "below") or target_price is None:
            return jsonify({"error": "Parámetros inválidos"}), 400
        alert = create_alert(symbol, condition, float(target_price), note)

    return jsonify(alert), 201


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def remove_alert(alert_id):
    success = delete_alert(alert_id)
    return jsonify({"success": success})


@app.route("/api/alerts/check", methods=["GET"])
def check():
    """Verifica alertas contra precios actuales de las posiciones en caché.
    Además entrega lo que el scheduler haya disparado desde el último poll."""
    # Alertas disparadas por el scheduler server-side, pendientes de notificar
    pending = pop_pending_triggered()

    alerts = get_alerts()
    active = [a for a in alerts if not a["triggered"]]
    if not active:
        return jsonify(pending)

    # Obtener precios actuales de los símbolos con alertas activas
    symbols = list({a["symbol"] for a in active})
    prices = {}
    for sym in symbols:
        q = get_quote(sym)
        if "error" not in q:
            prices[sym] = q.get("price", 0)

    triggered = check_alerts(prices)
    return jsonify(pending + triggered)


# ── Radar de Oportunidades ───────────────────────────────────────────────────

@app.route("/api/radar")
def radar():
    extra = request.args.get("extra", "")
    extra_symbols = [s.strip() for s in extra.split(",") if s.strip()] if extra else []
    data = radar_scan(extra_symbols)
    return jsonify(data)


# ── Smart Alerts ─────────────────────────────────────────────────────────────

@app.route("/api/smart-alerts/check")
def smart_check():
    """Corre el escaneo y retorna oportunidades nuevas."""
    data = check_opportunities()
    return jsonify(data)

@app.route("/api/smart-alerts/unseen")
def smart_unseen():
    """Alertas pendientes de ver (para polling ligero)."""
    return jsonify(get_unseen())

@app.route("/api/smart-alerts/seen", methods=["POST"])
def smart_mark_seen():
    body = request.get_json()
    mark_seen(body.get("ids", []))
    return jsonify({"ok": True})

@app.route("/api/smart-alerts/history")
def smart_hist():
    return jsonify(smart_history())

@app.route("/api/smart-alerts/track-record")
def smart_track_record():
    """Performance de cada smart alert desde su detección vs SPY (cacheado 15 min)."""
    force = request.args.get("refresh", "false") == "true"
    return jsonify(get_track_record(force_refresh=force))

@app.route("/api/smart-alerts/config", methods=["GET"])
def smart_cfg_get():
    return jsonify(smart_get_config())

@app.route("/api/smart-alerts/config", methods=["POST"])
def smart_cfg_set():
    body = request.get_json()
    cfg = smart_update_config(body)
    return jsonify(cfg)


# ── Noticias ─────────────────────────────────────────────────────────────────

def _portfolio_symbols() -> set:
    """Símbolos en el portfolio en caché, para resaltar noticias relevantes."""
    data = _portfolio_cache.get("data") or {}
    return {p.get("symbol", "").upper() for p in data.get("positions", []) if p.get("symbol")}


@app.route("/api/news")
def get_news():
    """
    Recolecta noticias de todas las fuentes, las rankea y les agrega contexto de Haiku.
    Parámetros:
      ?enrich=true   → genera contexto con Haiku (requiere API key)
      ?enrich=false  → solo títulos y links, sin IA (default)
      ?max=15        → máximo de noticias por sección
      ?refresh=true  → ignora el cache y vuelve a recolectar
    """
    do_enrich = request.args.get("enrich", "false") == "true"
    max_items = int(request.args.get("max", 80))
    force_refresh = request.args.get("refresh", "false") == "true"

    # Cache: la versión sin IA dura poco (20 min); la enriquecida dura más (6 h)
    # porque cuesta dinero regenerarla.
    cache_key = f"enrich={do_enrich}|max={max_items}"
    ttl = 6 * 3600 if do_enrich else 20 * 60

    port_syms = _portfolio_symbols()

    if not force_refresh:
        cached, age = news_cache.get(cache_key, ttl)
        if cached is not None:
            _mark_portfolio(cached, port_syms)
            cached["_meta"] = {"cached": True, "age_seconds": age, "enriched": do_enrich}
            return jsonify(cached)

    # collect_all es async, lo corremos desde Flask con asyncio.run
    try:
        data = asyncio.run(news_collect(max_per_section=max_items))
    except RuntimeError:
        # Si ya hay un event loop corriendo (en algunos entornos), crear uno nuevo
        loop = asyncio.new_event_loop()
        data = loop.run_until_complete(news_collect(max_per_section=max_items))
        loop.close()

    # Enriquecer con Haiku si se pide. Solo las más relevantes de cada sección
    # para mantener el costo y el tamaño de respuesta acotados; enrich_news muta
    # los NewsItem in-place, así que el resto de la lista queda intacto.
    if do_enrich:
        for section in data:
            enrich_news(data[section][:12], config)

    # Serializar a dict para JSON
    result = {}
    for section, items in data.items():
        result[section] = [
            {
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "published": item.published.strftime("%d/%m %H:%M"),
                "relevance_score": item.relevance_score,
                "symbols": item.symbols_mentioned,
                "summary": item.summary,
                "context": item.context,
                "section": item.section,
            }
            for item in items
        ]

    news_cache.set(cache_key, result)
    _mark_portfolio(result, port_syms)
    result["_meta"] = {"cached": False, "age_seconds": 0, "enriched": do_enrich}
    return jsonify(result)


def _mark_portfolio(result: dict, port_syms: set) -> None:
    """Marca in_portfolio=True en noticias que mencionan algún símbolo del portfolio."""
    for section, items in result.items():
        if section.startswith("_"):
            continue
        for it in items:
            syms = {s.upper() for s in it.get("symbols", [])}
            it["in_portfolio"] = bool(syms & port_syms)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"🚀 Plataforma de inversiones corriendo en http://localhost:{config.APP_PORT}")
    print(f"   IBKR Flex Query: {'✅ configurado' if config.IBKR_FLEX_TOKEN != 'TU_TOKEN_FLEX_QUERY_AQUI' else '⚠️  no configurado'}")
    groq_ok = config.GROQ_API_KEY != "TU_GROQ_API_KEY_AQUI"
    anthropic_ok = config.ANTHROPIC_API_KEY != "TU_ANTHROPIC_API_KEY_AQUI"
    print(f"   AI Provider: {config.AI_PROVIDER}")
    print(f"   Groq (gratis): {'✅ configurado' if groq_ok else '⚠️  no configurado'}")
    print(f"   Anthropic (pago): {'✅ configurado' if anthropic_ok else '⚠️  no configurado'}")

    # Iniciar el scheduler evitando el doble arranque del reloader de Flask:
    # - con debug/reloader activo, solo el proceso hijo tiene WERKZEUG_RUN_MAIN=true
    # - sin reloader (DEBUG_MODE=false), se inicia directo
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not config.DEBUG_MODE:
        init_scheduler()

    app.run(debug=config.DEBUG_MODE, port=config.APP_PORT)
