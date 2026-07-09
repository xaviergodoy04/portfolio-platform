"""
Backend principal - Flask API para la plataforma de inversiones.
Correr con: python app.py
Luego abrir: http://localhost:5000
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, g, jsonify, request, send_from_directory, session
from flask_cors import CORS
import threading
import secrets

import config
from modules.ibkr import fetch_flex_report, parse_csv_upload
from modules.market_data import (
    VALID_COMPARE_PERIODS,
    get_quote,
    get_history,
    compare_assets,
    enrich_positions,
)
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
from modules.version import get_version
from modules.health import get_health
from modules import auth
import asyncio
import time
from modules import db
from modules.news.collector import collect_all as news_collect, SECTIONS as NEWS_SECTIONS
from modules.news.enricher import enrich_items
from modules.news import cache as news_cache
from modules.news import health as news_health
from modules.news.affinity import affinity_bonus, invalidate_profile

app = Flask(__name__, static_folder="static")
CORS(app, supports_credentials=True)

# Firma la cookie de sesión. Sin SECRET_KEY en .env, se genera una al azar en
# memoria: la app funciona pero las sesiones no sobreviven un restart.
app.secret_key = config.SECRET_KEY or secrets.token_hex(32)
if not config.SECRET_KEY:
    print("⚠️  SECRET_KEY no configurada — las sesiones no sobrevivirán un restart. Fijala en .env.")

# Cache en memoria del portfolio de cada usuario (se limpia al reiniciar):
# {user_id: {...datos del portfolio...}}
_portfolio_cache = {}


def _persist_portfolio(user_id: int, data: dict):
    """Guarda el portfolio de un usuario en la DB y hace upsert del snapshot del día."""
    try:
        db.save_portfolio_cache(user_id, data)
        db.upsert_snapshot(user_id, data)
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
        db.log_event(endpoint, request.method, response.status_code, duration_ms,
                     user_id=auth.current_user_id())
    except Exception:
        pass  # la instrumentación nunca debe romper un request
    return response


# ── Autenticación (login/logout públicos; el resto de rutas privadas usa
# @auth.login_required) ──────────────────────────────────────────────────────

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    user = auth.login(username, password)
    if not user:
        return jsonify({"error": "Usuario o contraseña incorrectos"}), 401
    return jsonify(user)


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    auth.logout()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def auth_me():
    user_id = auth.current_user_id()
    if user_id is None:
        return jsonify({"error": "No hay sesión"}), 401
    user = db.get_user(user_id)
    if not user:
        auth.logout()  # sesión de un usuario que ya no existe
        return jsonify({"error": "No hay sesión"}), 401
    return jsonify({
        "id": user["id"], "username": user["username"], "display_name": user["display_name"],
        "ibkr_configured": bool(user.get("ibkr_flex_token")),
    })


@app.route("/api/account/ibkr", methods=["POST"])
@auth.login_required
def account_set_ibkr():
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    query_id = (body.get("query_id") or "").strip()
    if not token and not query_id:
        return jsonify({"error": "Pegá al menos el token o el query ID"}), 400
    user_id = auth.current_user_id()
    db.set_ibkr_credentials(user_id, token, query_id)
    # Estado efectivo tras el update parcial (campo vacío no pisa lo guardado)
    user = db.get_user(user_id)
    configured = bool(user.get("ibkr_flex_token")) and bool(user.get("ibkr_flex_query_id"))
    warning = None
    if not configured:
        missing = "query ID" if user.get("ibkr_flex_token") else "token"
        warning = f"Falta el {missing} — la conexión queda incompleta hasta cargarlo."
    return jsonify({"ok": True, "ibkr_configured": configured, "warning": warning})


@app.route("/api/stats/usage")
def usage_stats():
    """Estadísticas de uso de los endpoints: count, latencia promedio,
    errores 5xx por endpoint y serie diaria de requests."""
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        days = 30
    return jsonify(db.get_usage_stats(days))


@app.route("/api/version")
def version():
    """Versión del build corriendo (git hash + branch) y hora de arranque.
    Para saber de un vistazo si el server es el código que creés que es."""
    return jsonify(get_version())


@app.route("/api/health")
def system_health():
    """Semáforo de las dependencias críticas: mercado, IA, feeds y scheduler."""
    return jsonify(get_health())


# ── Servir el frontend ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/sw.js")
def service_worker():
    """El service worker se sirve desde la raíz para que su scope cubra
    toda la app (desde /static/ solo controlaría /static/*)."""
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


@app.route("/api/mobile-info")
def mobile_info():
    """IP LAN del server para armar la URL que se abre desde el celular."""
    import socket
    lan_ip = None
    try:
        # Truco estándar: un socket UDP "conectado" a una IP externa revela
        # qué IP local usaría el sistema para salir — no manda ningún paquete
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    lan_enabled = config.APP_HOST in ("0.0.0.0", "::")
    return jsonify({
        "lan_enabled": lan_enabled,
        "lan_url": f"http://{lan_ip}:{config.APP_PORT}" if (lan_ip and lan_enabled) else None,
        "host": config.APP_HOST,
        "port": config.APP_PORT,
    })


# ── Portfolio / IBKR (privado — requiere cuenta) ─────────────────────────────

def _get_cached_portfolio(user_id: int):
    """Portfolio de un usuario: memoria primero, si no hidrata desde la DB
    (un reinicio no obliga a esperar a IBKR)."""
    if user_id not in _portfolio_cache:
        stored = db.load_portfolio_cache(user_id)
        if stored:
            _portfolio_cache[user_id] = stored
    return _portfolio_cache.get(user_id)


@app.route("/api/portfolio", methods=["GET"])
@auth.login_required
def get_portfolio():
    """Obtiene portfolio desde IBKR Flex Query propio del usuario (o caché)."""
    user_id = auth.current_user_id()
    force_refresh = request.args.get("refresh", "false") == "true"

    cached = _get_cached_portfolio(user_id)
    if not force_refresh and cached:
        return jsonify(cached)

    user = db.get_user(user_id)
    token, query_id = user.get("ibkr_flex_token"), user.get("ibkr_flex_query_id")
    if not token or not query_id:
        return jsonify({"error": "No tenés IBKR conectado — cargá tu token en Ajustes, "
                                  "subí un CSV o cargá tus posiciones a mano."})

    data = fetch_flex_report(token, query_id)

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
        _portfolio_cache[user_id] = data
        _persist_portfolio(user_id, data)

    return jsonify(data)


@app.route("/api/portfolio/upload", methods=["POST"])
@auth.login_required
def upload_csv():
    """Carga portfolio desde CSV exportado de IB."""
    if "file" not in request.files:
        return jsonify({"error": "No se recibió archivo"}), 400

    user_id = auth.current_user_id()
    file = request.files["file"]
    content = file.read().decode("utf-8", errors="replace")
    data = parse_csv_upload(content)

    if "error" not in data and data.get("positions"):
        data["positions"] = enrich_positions(data["positions"])
        _portfolio_cache[user_id] = data
        _persist_portfolio(user_id, data)

    return jsonify(data)


@app.route("/api/portfolio/manual", methods=["POST"])
@auth.login_required
def set_manual_portfolio():
    """Permite ingresar posiciones manualmente (sin IBKR)."""
    user_id = auth.current_user_id()
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
    _portfolio_cache[user_id] = data
    _persist_portfolio(user_id, data)
    return jsonify(data)


@app.route("/api/portfolio/history", methods=["GET"])
@auth.login_required
def portfolio_history():
    """Historial de snapshots diarios del portfolio (para gráficos de evolución)."""
    try:
        days = int(request.args.get("days", 90))
    except ValueError:
        days = 90
    snapshots = db.get_snapshots(auth.current_user_id(), days)
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
    period = request.args.get("period", "1y")
    symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "Indicá al menos un símbolo"}), 400
    if period not in VALID_COMPARE_PERIODS:
        return jsonify({
            "error": f"Período inválido: '{period}'. Válidos: {', '.join(VALID_COMPARE_PERIODS)}"
        }), 400
    return jsonify(compare_assets(symbols, period))


# ── AI Analysis (público — sin cuenta no hay contexto de portfolio) ──────────

@app.route("/api/analyze/<symbol>")
def analyze(symbol):
    user_id = auth.current_user_id()
    portfolio = _get_cached_portfolio(user_id) if user_id else None
    result = analyze_asset(symbol.upper(), config, portfolio)
    return jsonify(result)


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json()
    question = body.get("question", "").strip()
    if not question:
        return jsonify({"error": "Pregunta vacía"}), 400

    # Historial de conversación (opcional, retro-compatible): se limita a los
    # últimos 20 mensajes para acotar el tamaño del prompt.
    history = body.get("history")
    if isinstance(history, list):
        history = history[-20:]
    else:
        history = None

    # Símbolo que el usuario está mirando en la UI (opcional)
    context_symbol = body.get("context_symbol") or None
    if context_symbol:
        context_symbol = str(context_symbol).strip().upper()[:12]

    user_id = auth.current_user_id()
    portfolio = _get_cached_portfolio(user_id) if user_id else None
    answer = chat_analysis(question, config, portfolio,
                           history=history, context_symbol=context_symbol)
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


# ── Alerts (privado — requiere cuenta) ───────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
@auth.login_required
def list_alerts():
    return jsonify(get_alerts(auth.current_user_id()))


@app.route("/api/alerts", methods=["POST"])
@auth.login_required
def add_alert():
    user_id = auth.current_user_id()
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
        alert = create_pct_alert(user_id, symbol, float(pct), float(reference_price), reference_type, note)
    else:
        condition = body.get("condition")
        target_price = body.get("target_price")
        if condition not in ("above", "below") or target_price is None:
            return jsonify({"error": "Parámetros inválidos"}), 400
        alert = create_alert(user_id, symbol, condition, float(target_price), note)

    return jsonify(alert), 201


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
@auth.login_required
def remove_alert(alert_id):
    success = delete_alert(auth.current_user_id(), alert_id)
    return jsonify({"success": success})


@app.route("/api/alerts/check", methods=["GET"])
@auth.login_required
def check():
    """Verifica alertas contra precios actuales de las posiciones en caché.
    Además entrega lo que el scheduler haya disparado desde el último poll."""
    user_id = auth.current_user_id()
    # Alertas disparadas por el scheduler server-side, pendientes de notificar
    pending = pop_pending_triggered(user_id)

    alerts = get_alerts(user_id)
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

    triggered = check_alerts(user_id, prices)
    return jsonify(pending + triggered)


# ── Watchlist (privado — requiere cuenta) ────────────────────────────────────

@app.route("/api/watchlist", methods=["GET"])
@auth.login_required
def watchlist_list():
    """Watchlist con quote fresco por símbolo (get_quote cachea 120 s)."""
    items = db.get_watchlist(auth.current_user_id())
    for it in items:
        q = get_quote(it["symbol"])
        if "error" in q:
            it["error"] = q["error"]
        else:
            it["price"] = q.get("price")
            it["change_pct"] = q.get("change_pct")
            it["name"] = q.get("name", "")
    return jsonify(items)


@app.route("/api/watchlist", methods=["POST"])
@auth.login_required
def watchlist_add():
    body = request.get_json(silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    note = (body.get("note") or "").strip()
    if not symbol:
        return jsonify({"error": "Símbolo requerido"}), 400

    # Validar que el ticker exista antes de guardarlo
    q = get_quote(symbol)
    if "error" in q:
        return jsonify({"error": f"No hay datos para '{symbol}' — verificá el ticker"}), 400

    entry = db.add_to_watchlist(auth.current_user_id(), symbol, note)
    return jsonify(entry), 201


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
@auth.login_required
def watchlist_remove(symbol):
    success = db.remove_from_watchlist(auth.current_user_id(), symbol.strip().upper())
    return jsonify({"success": success})


# ── Radar de Oportunidades (público — la watchlist propia solo se suma con cuenta) ──

@app.route("/api/radar")
def radar():
    extra = request.args.get("extra", "")
    extra_symbols = [s.strip() for s in extra.split(",") if s.strip()] if extra else []
    # Con cuenta, la watchlist propia se vigila siempre: se suma a los extras
    # sin duplicar (radar_scan ya evita duplicar contra el UNIVERSE). Sin
    # cuenta, el radar escanea solo el universo compartido + los extras que
    # el pedido pase explícitamente.
    user_id = auth.current_user_id()
    if user_id:
        for s in db.get_watchlist_symbols(user_id):
            if s not in extra_symbols:
                extra_symbols.append(s)
    data = radar_scan(extra_symbols)
    return jsonify(data)


# ── Smart Alerts (privado — requiere cuenta) ─────────────────────────────────

@app.route("/api/smart-alerts/check")
@auth.login_required
def smart_check():
    """Corre el escaneo y retorna oportunidades nuevas."""
    data = check_opportunities(auth.current_user_id())
    return jsonify(data)

@app.route("/api/smart-alerts/unseen")
@auth.login_required
def smart_unseen():
    """Alertas pendientes de ver (para polling ligero)."""
    return jsonify(get_unseen(auth.current_user_id()))

@app.route("/api/smart-alerts/seen", methods=["POST"])
@auth.login_required
def smart_mark_seen():
    body = request.get_json()
    mark_seen(auth.current_user_id(), body.get("ids", []))
    return jsonify({"ok": True})

@app.route("/api/smart-alerts/history")
@auth.login_required
def smart_hist():
    return jsonify(smart_history(auth.current_user_id()))

@app.route("/api/smart-alerts/track-record")
@auth.login_required
def smart_track_record():
    """Performance de cada smart alert desde su detección vs SPY (cacheado 15 min)."""
    force = request.args.get("refresh", "false") == "true"
    return jsonify(get_track_record(auth.current_user_id(), force_refresh=force))

@app.route("/api/smart-alerts/config", methods=["GET"])
@auth.login_required
def smart_cfg_get():
    return jsonify(smart_get_config(auth.current_user_id()))

@app.route("/api/smart-alerts/config", methods=["POST"])
@auth.login_required
def smart_cfg_set():
    body = request.get_json()
    cfg = smart_update_config(auth.current_user_id(), body)
    return jsonify(cfg)


# ── Noticias (público — la personalización solo aplica con cuenta) ──────────

def _portfolio_symbols(user_id) -> set:
    """Símbolos en el portfolio en caché de un usuario, para resaltar noticias
    relevantes. Sin usuario, conjunto vacío (nada que resaltar)."""
    if not user_id:
        return set()
    data = _get_cached_portfolio(user_id) or {}
    return {p.get("symbol", "").upper() for p in data.get("positions", []) if p.get("symbol")}


# Cache de noticias en dos niveles:
#   - Pool base (sin IA): key "base|max={n}", TTL corto — recolectar es gratis.
#   - Contexto IA por sección: key "enriched|{SECCION}" con un mapa {url: context},
#     TTL largo porque regenerarlo cuesta dinero. Al servir, el contexto se
#     mergea sobre el pool base sin re-recolectar ni re-enriquecer lo demás.
NEWS_BASE_TTL = 20 * 60
NEWS_ENRICH_TTL = 6 * 3600
NEWS_ENRICH_TOP_N = 12  # cuántas noticias por sección se enriquecen (costo acotado)


def _collect_news_serialized(max_items: int) -> dict:
    """Recolecta todas las fuentes y serializa a dicts listos para JSON/cache."""
    # collect_all es async, lo corremos desde Flask con asyncio.run
    try:
        data = asyncio.run(news_collect(max_per_section=max_items))
    except RuntimeError:
        # Si ya hay un event loop corriendo (en algunos entornos), crear uno nuevo
        loop = asyncio.new_event_loop()
        data = loop.run_until_complete(news_collect(max_per_section=max_items))
        loop.close()

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
    return result


@app.route("/api/news")
def get_news():
    """
    Recolecta noticias de todas las fuentes, las rankea y opcionalmente les
    agrega contexto de IA.
    Parámetros:
      ?enrich=true        → genera contexto con IA (requiere API key)
      ?enrich=false       → solo títulos y links, sin IA (default)
      ?section=MERCADOS   → con enrich=true, enriquece SOLO esa sección.
                            Sin section, enriquece todas (comportamiento legacy).
      ?max=15             → máximo de noticias por sección
      ?refresh=true       → ignora el cache: re-recolecta y re-enriquece
    """
    do_enrich = request.args.get("enrich", "false") == "true"
    max_items = int(request.args.get("max", 80))
    force_refresh = request.args.get("refresh", "false") == "true"
    section = (request.args.get("section") or "").strip().upper() or None

    if section and section not in NEWS_SECTIONS:
        return jsonify({"error": f"Sección inválida: {section}. Válidas: {NEWS_SECTIONS}"}), 400

    # 1) Pool base: cache fresco si lo hay, si no re-recolectar
    base_key = f"base|max={max_items}"
    result, age = (None, None)
    if not force_refresh:
        result, age = news_cache.get(base_key, NEWS_BASE_TTL)
    from_cache = result is not None
    if result is None:
        result = _collect_news_serialized(max_items)
        news_cache.set(base_key, result)  # se cachea limpio, sin contexto ni marcas
        age = 0

    # 2) Contexto IA por sección: usa el cache por sección o enriquece las top N
    enriched_sections = []
    if do_enrich:
        targets = [section] if section else NEWS_SECTIONS
        for sec in targets:
            items = result.get(sec) or []
            ctx_map = None
            if not force_refresh:
                ctx_map, _ = news_cache.get(f"enriched|{sec}", NEWS_ENRICH_TTL)
            if not ctx_map:
                top = items[:NEWS_ENRICH_TOP_N]
                enrich_items(top, config)
                ctx_map = {it["url"]: it["context"] for it in top if it.get("context")}
                # Solo cachear si hubo resultado: un fallo del LLM no debe
                # quedar cacheado 6 horas
                if ctx_map:
                    news_cache.set(f"enriched|{sec}", ctx_map)
            for it in items:
                ctx = ctx_map.get(it["url"])
                if ctx:
                    it["context"] = ctx
            if ctx_map:
                enriched_sections.append(sec)

    # Personalización (marcas de portfolio/watchlist, feedback y afinidad):
    # solo con cuenta. Sin sesión se sirve el pool con el score base, igual
    # para cualquiera — nada del gusto de un usuario se le impone a otro.
    user_id = auth.current_user_id()
    port_syms = _portfolio_symbols(user_id)
    watch_syms = {s.upper() for s in db.get_watchlist_symbols(user_id)} if user_id else set()
    _mark_symbols(result, port_syms, watch_syms)
    _apply_feedback_and_affinity(result, user_id)
    result["_meta"] = {
        "cached": from_cache,
        "age_seconds": age or 0,
        "enriched": do_enrich,
        "enriched_sections": enriched_sections,
        "personalized": user_id is not None,
    }
    return jsonify(result)


@app.route("/api/news/health")
def news_feed_health():
    """
    Estado de salud de las fuentes RSS de noticias (tabla feed_health).
    Si nunca se corrió el chequeo (o se pide ?refresh=true), lo corre ahora.
    """
    force = request.args.get("refresh", "false") == "true"
    rows = db.get_feed_health()
    if force or not rows:
        rows = news_health.check_all_feeds()
    return jsonify({"sources": rows, "count": len(rows)})


def _mark_symbols(result: dict, port_syms: set, watch_syms: set) -> None:
    """Marca in_portfolio / in_watchlist en noticias que mencionan símbolos
    del portfolio o de la watchlist."""
    for section, items in result.items():
        if section.startswith("_"):
            continue
        for it in items:
            syms = {s.upper() for s in it.get("symbols", [])}
            it["in_portfolio"] = bool(syms & port_syms)
            it["in_watchlist"] = bool(syms & watch_syms)


def _apply_feedback_and_affinity(result: dict, user_id) -> None:
    """
    Mergea el feedback guardado de un usuario (liked/read, lookup O(1) por
    url_hash) y calcula relevance_score_personal = relevance_score + affinity_bonus.
    El score original NO se pisa: el personal es un campo adicional y cada
    sección se reordena por él. El pool cacheado queda intacto (se cachea
    limpio antes de este paso). Sin usuario (anónimo), no hay feedback propio:
    el score personal queda igual al base y no se aplica bonus de afinidad.
    """
    fb_map = db.get_news_feedback_map(user_id) if user_id else {}
    for section, items in result.items():
        if section.startswith("_"):
            continue
        for it in items:
            fb = fb_map.get(db.news_url_hash(it.get("url") or ""))
            it["liked"] = bool(fb and fb.get("liked"))
            it["read"] = bool(fb and fb.get("read"))
            bonus = affinity_bonus(it, user_id) if user_id else 0.0
            it["relevance_score_personal"] = round((it.get("relevance_score") or 0) + bonus, 2)
        items.sort(key=lambda x: x["relevance_score_personal"], reverse=True)


@app.route("/api/news/feedback", methods=["POST"])
@auth.login_required
def news_feedback_set():
    """
    Registra feedback del usuario sobre una noticia (upsert parcial):
    body {url, title, section, source, symbols, liked?: bool, read?: bool}.
    Solo pisa los flags presentes en el body. Retorna el estado resultante.
    """
    user_id = auth.current_user_id()
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Falta 'url'"}), 400
    liked, read = body.get("liked"), body.get("read")
    if liked is None and read is None:
        return jsonify({"error": "Mandá al menos 'liked' o 'read'"}), 400

    state = db.set_news_feedback(
        user_id,
        {
            "url": url,
            "title": body.get("title") or "",
            "section": body.get("section") or "",
            "source": body.get("source") or "",
            "symbols": body.get("symbols") or [],
        },
        liked=bool(liked) if liked is not None else None,
        read=bool(read) if read is not None else None,
    )
    # El próximo /api/news reconstruye el perfil de este usuario con el feedback incluido
    invalidate_profile(user_id)
    return jsonify(state)


@app.route("/api/news/feedback/stats")
@auth.login_required
def news_feedback_stats():
    """Agregados del feedback de un usuario en los últimos 90 días para el
    tablero: por sección (shown_proxy = likes + reads), top fuentes y símbolos likeados."""
    rows = db.get_feedback_rows(auth.current_user_id(), 90)
    by_section, src_likes, sym_likes = {}, {}, {}
    for r in rows:
        sec = r.get("section") or "SIN_SECCION"
        agg = by_section.setdefault(sec, {"shown_proxy": 0, "likes": 0, "reads": 0})
        if r.get("liked"):
            agg["likes"] += 1
            src = r.get("source") or "?"
            src_likes[src] = src_likes.get(src, 0) + 1
            for s in r.get("symbols") or []:
                s = (s or "").strip().upper()
                if s:
                    sym_likes[s] = sym_likes.get(s, 0) + 1
        if r.get("read"):
            agg["reads"] += 1
        agg["shown_proxy"] = agg["likes"] + agg["reads"]

    top = lambda d, key: [
        {key: k, "likes": v}
        for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:10]
    ]
    return jsonify({
        "days": 90,
        "total_items": len(rows),
        "by_section": by_section,
        "top_sources_liked": top(src_likes, "source"),
        "top_symbols_liked": top(sym_likes, "symbol"),
    })


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"🚀 Plataforma de inversiones corriendo en http://localhost:{config.APP_PORT}")
    print(f"   Versión: {get_version()['label']}")
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

    if config.APP_HOST in ("0.0.0.0", "::"):
        print("   📱 Accesible desde la red local (celular): ver Ajustes → Usar en el celular")
    app.run(debug=config.DEBUG_MODE, host=config.APP_HOST, port=config.APP_PORT)
