"""
Módulo de datos de mercado usando yfinance (gratuito).

Incluye un cache en memoria thread-safe (dict + lock):
- info de tickers (base de get_quote): TTL 120 s
- historiales de precios: TTL 600 s, key símbolo+período
Los resultados fallidos (excepción, respuesta vacía o sin precio real) NO se
cachean, así el próximo request reintenta contra yfinance.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf
import pandas as pd

QUOTE_TTL = 120       # segundos
HISTORY_TTL = 600     # segundos
MAX_WORKERS = 8       # hilos para enriquecer posiciones en paralelo

_cache_lock = threading.Lock()
_info_cache: dict = {}     # symbol -> (timestamp, info crudo de yfinance)
_history_cache: dict = {}  # (symbol, period) -> (timestamp, dict de get_history)


def clear_cache():
    """Vacía todos los caches en memoria (útil para tests)."""
    with _cache_lock:
        _info_cache.clear()
        _history_cache.clear()


def _cache_get(cache: dict, key, ttl: float):
    """Lee del cache si la entrada existe y no expiró. El lock solo protege
    el acceso al dict: el fetch a yfinance ocurre fuera del lock para no
    serializar las llamadas en paralelo."""
    with _cache_lock:
        item = cache.get(key)
    if item is not None and (time.time() - item[0]) < ttl:
        return item[1]
    return None


def _cache_set(cache: dict, key, value):
    with _cache_lock:
        cache[key] = (time.time(), value)


def get_ticker_info(symbol: str):
    """
    Info cruda de yfinance para un símbolo, cacheada QUOTE_TTL segundos.
    Retorna None si yfinance falla, devuelve vacío o no trae un precio real
    (0/None): esos casos NO se cachean para que el próximo intento reintente.
    """
    symbol = symbol.upper()
    cached = _cache_get(_info_cache, symbol, QUOTE_TTL)
    if cached is not None:
        return cached

    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None

    price = (info or {}).get("currentPrice") or (info or {}).get("regularMarketPrice")
    if not info or not price:
        # Vacío o sin precio: no es un dato de mercado válido → no cachear
        return None

    _cache_set(_info_cache, symbol, info)
    return info


def get_quote(symbol: str) -> dict:
    """Precio actual + métricas básicas de un activo (cacheado 120 s).
    Si no hay datos válidos retorna {"error": ..., "symbol": ...} — el resto
    del código ya distingue por la presencia de la key "error"."""
    symbol = symbol.upper()
    info = get_ticker_info(symbol)
    if info is None:
        return {
            "error": f"Error obteniendo datos de {symbol}: sin datos o sin precio válido en yfinance",
            "symbol": symbol,
        }

    return {
        "symbol": symbol,
        "name": info.get("longName", info.get("shortName", symbol)),
        "price": info.get("currentPrice", info.get("regularMarketPrice", 0)),
        "change_pct": info.get("regularMarketChangePercent", 0),
        "volume": info.get("regularMarketVolume", 0),
        "market_cap": info.get("marketCap", 0),
        "pe_ratio": info.get("trailingPE", None),
        "forward_pe": info.get("forwardPE", None),
        "eps": info.get("trailingEps", None),
        "dividend_yield": info.get("dividendYield", None),
        "52w_high": info.get("fiftyTwoWeekHigh", None),
        "52w_low": info.get("fiftyTwoWeekLow", None),
        "avg_volume": info.get("averageVolume", None),
        "sector": info.get("sector", None),
        "industry": info.get("industry", None),
        "currency": info.get("currency", "USD"),
        "exchange": info.get("exchange", None),
    }


def get_history(symbol: str, period: str = "1y") -> dict:
    """
    Historial de precios (cacheado 600 s por símbolo+período).
    Periods: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
    Los errores no se cachean.
    """
    symbol = symbol.upper()
    key = (symbol, period)
    cached = _cache_get(_history_cache, key, HISTORY_TTL)
    if cached is not None:
        return cached

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)

        if hist.empty:
            return {"error": f"No hay datos históricos para {symbol}"}

        result = {
            "symbol": symbol,
            "period": period,
            "dates": hist.index.strftime("%Y-%m-%d").tolist(),
            "close": hist["Close"].round(2).tolist(),
            "open": hist["Open"].round(2).tolist(),
            "high": hist["High"].round(2).tolist(),
            "low": hist["Low"].round(2).tolist(),
            "volume": hist["Volume"].tolist(),
        }
        _cache_set(_history_cache, key, result)
        return result
    except Exception as e:
        return {"error": f"Error obteniendo historial de {symbol}: {str(e)}"}


def compare_assets(symbols: list[str]) -> dict:
    """Compara múltiples activos: precios normalizados + métricas clave."""
    comparison_data = {"symbols": symbols, "quotes": {}, "history": {}, "metrics": {}}

    for symbol in symbols:
        quote = get_quote(symbol)
        if "error" not in quote:
            comparison_data["quotes"][symbol] = quote

        hist = get_history(symbol, "1y")
        if "error" not in hist and hist.get("close"):
            # Normalizar a base 100
            base = hist["close"][0]
            normalized = [round((p / base) * 100, 2) for p in hist["close"]]
            comparison_data["history"][symbol] = {
                "dates": hist["dates"],
                "normalized": normalized,
                "close": hist["close"]
            }

            # Calcular retorno del período
            ret = ((hist["close"][-1] - hist["close"][0]) / hist["close"][0]) * 100
            vol = pd.Series(hist["close"]).pct_change().std() * (252 ** 0.5) * 100  # volatilidad anualizada

            comparison_data["metrics"][symbol] = {
                "return_1y": round(ret, 2),
                "volatility_1y": round(vol, 2),
                "sharpe_approx": round(ret / vol, 2) if vol > 0 else 0,
                "current_price": hist["close"][-1],
                "pe_ratio": quote.get("pe_ratio"),
                "market_cap": quote.get("market_cap"),
                "sector": quote.get("sector"),
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
    Las llamadas a yfinance se hacen en paralelo (hasta MAX_WORKERS hilos);
    executor.map preserva el orden original de las posiciones."""
    if not positions:
        return []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        return list(executor.map(_enrich_one, positions))
