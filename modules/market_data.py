"""
Fachada de datos de mercado.

La obtención de datos vive en modules/market_provider.py (provider activo
según MARKET_PROVIDER en config, default yfinance). Esta fachada mantiene la
API pública histórica (get_quote / get_history / get_ticker_info /
compare_assets / enrich_positions) para que ningún consumidor cambie sus
imports, y agrega un cache TTL en memoria agnóstico del provider:

- quotes e info de tickers: TTL 120 s
- historiales de precios: TTL 600 s, key símbolo+período

Los resultados fallidos (None o dicts con "error") NO se cachean, así el
próximo request reintenta contra el provider.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from modules.market_provider import get_provider

QUOTE_TTL = 120       # segundos (quotes + info de tickers)
HISTORY_TTL = 600     # segundos
MAX_WORKERS = 8       # hilos para enriquecer posiciones en paralelo

_cache_lock = threading.Lock()
_info_cache: dict = {}     # symbol -> (timestamp, info cruda del provider)
_quote_cache: dict = {}    # symbol -> (timestamp, dict de get_quote)
_history_cache: dict = {}  # (symbol, period) -> (timestamp, dict de get_history)


def clear_cache():
    """Vacía todos los caches en memoria (útil para tests)."""
    with _cache_lock:
        _info_cache.clear()
        _quote_cache.clear()
        _history_cache.clear()


def _cache_get(cache: dict, key, ttl: float):
    """Lee del cache si la entrada existe y no expiró. El lock solo protege
    el acceso al dict: el fetch al provider ocurre fuera del lock para no
    serializar las llamadas en paralelo."""
    with _cache_lock:
        item = cache.get(key)
    if item is not None and (time.time() - item[0]) < ttl:
        return item[1]
    return None


def _cache_set(cache: dict, key, value):
    with _cache_lock:
        cache[key] = (time.time(), value)


# ── API pública (misma firma y shapes de siempre) ────────────────────────────

def get_ticker_info(symbol: str):
    """Info cruda del provider para un símbolo, cacheada QUOTE_TTL segundos.
    Retorna None si el provider no trae datos válidos (no se cachea)."""
    symbol = symbol.upper()
    cached = _cache_get(_info_cache, symbol, QUOTE_TTL)
    if cached is not None:
        return cached

    info = get_provider().get_ticker_info(symbol)
    if info is None:
        return None

    _cache_set(_info_cache, symbol, info)
    return info


def get_quote(symbol: str) -> dict:
    """Precio actual + métricas básicas de un activo (cacheado 120 s).
    Si no hay datos válidos retorna {"error": ..., "symbol": ...} — el resto
    del código ya distingue por la presencia de la key "error"."""
    symbol = symbol.upper()
    cached = _cache_get(_quote_cache, symbol, QUOTE_TTL)
    if cached is not None:
        return cached

    quote = get_provider().get_quote(symbol)
    if "error" not in quote:
        _cache_set(_quote_cache, symbol, quote)
    return quote


def get_history(symbol: str, period: str = "1y") -> dict:
    """Historial de precios (cacheado 600 s por símbolo+período).
    Periods: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
    Los errores no se cachean."""
    symbol = symbol.upper()
    key = (symbol, period)
    cached = _cache_get(_history_cache, key, HISTORY_TTL)
    if cached is not None:
        return cached

    result = get_provider().get_history(symbol, period)
    if "error" not in result:
        _cache_set(_history_cache, key, result)
    return result


# ── Lógica de negocio agnóstica del provider ─────────────────────────────────

# Períodos aceptados por compare_assets (el endpoint valida contra esta tupla)
VALID_COMPARE_PERIODS = ("1mo", "3mo", "6mo", "1y", "2y", "5y", "max")


def compare_assets(symbols: list[str], period: str = "1y") -> dict:
    """Compara múltiples activos sobre el período pedido: precios normalizados
    base 100, serie de drawdown, métricas (retorno, volatilidad anualizada,
    sharpe aproximado, max drawdown, beta vs SPY) y matriz de correlación de
    retornos diarios. Los símbolos sin datos se reportan en "errors" sin
    romper el resto de la comparación."""
    if period not in VALID_COMPARE_PERIODS:
        raise ValueError(
            f"Período inválido: '{period}'. Válidos: {', '.join(VALID_COMPARE_PERIODS)}"
        )

    comparison_data = {
        "symbols": symbols,
        "period": period,
        "quotes": {},
        "history": {},
        "metrics": {},
        "correlation": {},
        "errors": {},
    }

    # Historia de SPY una sola vez por request (benchmark para beta)
    spy_hist = get_history("SPY", period)
    spy_returns = None
    if "error" not in spy_hist and spy_hist.get("close"):
        spy_closes = pd.Series(spy_hist["close"], index=spy_hist["dates"], dtype=float)
        spy_returns = spy_closes.pct_change().dropna()

    daily_returns = {}  # symbol -> Series de retornos diarios (index = fecha)

    for symbol in symbols:
        quote = get_quote(symbol)
        if "error" not in quote:
            comparison_data["quotes"][symbol] = quote

        hist = get_history(symbol, period)
        if "error" in hist or not hist.get("close"):
            comparison_data["errors"][symbol] = hist.get(
                "error", "Sin datos de historial para el período"
            )
            continue

        closes = pd.Series(hist["close"], index=hist["dates"], dtype=float)

        # Normalizar a base 100
        base = closes.iloc[0]
        normalized = [round((p / base) * 100, 2) for p in hist["close"]]

        # Drawdown: % de caída desde el máximo acumulado hasta cada fecha (≤ 0)
        drawdown_series = ((closes / closes.cummax()) - 1) * 100
        comparison_data["history"][symbol] = {
            "dates": hist["dates"],
            "normalized": normalized,
            "close": hist["close"],
            "drawdown": [round(d, 2) for d in drawdown_series.tolist()],
        }

        # Métricas sobre el período pedido
        ret = ((closes.iloc[-1] - base) / base) * 100
        returns = closes.pct_change().dropna()
        vol = returns.std() * (252 ** 0.5) * 100  # volatilidad anualizada

        # Beta vs SPY: cov(retornos, retornos SPY) / var(SPY), fechas alineadas
        beta = None
        if spy_returns is not None and len(returns) >= 2:
            aligned = pd.concat([returns, spy_returns], axis=1, join="inner").dropna()
            if len(aligned) >= 2:
                spy_var = aligned.iloc[:, 1].var()
                if spy_var and spy_var > 0:
                    beta = round(aligned.iloc[:, 0].cov(aligned.iloc[:, 1]) / spy_var, 2)

        comparison_data["metrics"][symbol] = {
            "return_pct": round(ret, 2),
            "volatility_pct": round(vol, 2),
            "sharpe_approx": round(ret / vol, 2) if vol > 0 else 0,
            "max_drawdown_pct": round(drawdown_series.min(), 2),
            "beta_vs_spy": beta,
            "current_price": hist["close"][-1],
            "pe_ratio": quote.get("pe_ratio"),
            "market_cap": quote.get("market_cap"),
            "sector": quote.get("sector"),
        }
        daily_returns[symbol] = returns

    # Matriz de correlación de Pearson (retornos diarios, fechas comunes)
    if daily_returns:
        corr = pd.DataFrame(daily_returns).corr()
        comparison_data["correlation"] = {
            a: {
                b: (round(float(corr.loc[a, b]), 2) if pd.notna(corr.loc[a, b]) else None)
                for b in corr.columns
            }
            for a in corr.index
        }

    return comparison_data


def _enrich_one(pos: dict) -> dict:
    """Enriquece una posición individual con datos de mercado actuales."""
    symbol = pos.get("symbol", "")
    if not symbol:
        return pos

    quote = get_quote(symbol)
    pos_enriched = {**pos}

    if "error" not in quote:
        pos_enriched["current_price"] = quote.get("price", pos.get("mark_price", 0))
        pos_enriched["pe_ratio"] = quote.get("pe_ratio")
        pos_enriched["sector"] = quote.get("sector")
        pos_enriched["name"] = quote.get("name", symbol)
        pos_enriched["change_pct_today"] = quote.get("change_pct", 0)
    else:
        pos_enriched["current_price"] = pos.get("mark_price", 0)

    # Recalcular P&L con precio actual
    if pos_enriched.get("current_price") and pos_enriched.get("avg_cost"):
        cost_basis = pos_enriched["avg_cost"] * pos_enriched["quantity"]
        current_val = pos_enriched["current_price"] * pos_enriched["quantity"]
        pos_enriched["position_value"] = round(current_val, 2)
        pos_enriched["unrealized_pnl"] = round(current_val - cost_basis, 2)
        pos_enriched["unrealized_pnl_pct"] = round(
            ((current_val - cost_basis) / cost_basis * 100) if cost_basis != 0 else 0, 2
        )

    return pos_enriched


def enrich_positions(positions: list[dict]) -> list[dict]:
    """Agrega datos de mercado actuales a las posiciones del portfolio.
    Las llamadas al provider se hacen en paralelo (hasta MAX_WORKERS hilos);
    executor.map preserva el orden original de las posiciones."""
    if not positions:
        return []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        return list(executor.map(_enrich_one, positions))
