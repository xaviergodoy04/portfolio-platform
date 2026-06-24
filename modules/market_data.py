"""
Módulo de datos de mercado usando yfinance (gratuito).
"""
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta


def get_quote(symbol: str) -> dict:
    """Precio actual + métricas básicas de un activo."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info

        return {
            "symbol": symbol.upper(),
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
    except Exception as e:
        return {"error": f"Error obteniendo datos de {symbol}: {str(e)}", "symbol": symbol}


def get_history(symbol: str, period: str = "1y") -> dict:
    """
    Historial de precios.
    Periods: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)

        if hist.empty:
            return {"error": f"No hay datos históricos para {symbol}"}

        return {
            "symbol": symbol.upper(),
            "period": period,
            "dates": hist.index.strftime("%Y-%m-%d").tolist(),
            "close": hist["Close"].round(2).tolist(),
            "open": hist["Open"].round(2).tolist(),
            "high": hist["High"].round(2).tolist(),
            "low": hist["Low"].round(2).tolist(),
            "volume": hist["Volume"].tolist(),
        }
    except Exception as e:
        return {"error": f"Error obteniendo historial de {symbol}: {str(e)}"}


def compare_assets(symbols: list[str]) -> dict:
    """Compara múltiples activos: precios normalizados + métricas clave."""
    results = {}
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


def enrich_positions(positions: list[dict]) -> list[dict]:
    """Agrega datos de mercado actuales a las posiciones del portfolio."""
    enriched = []
    for pos in positions:
        symbol = pos.get("symbol", "")
        if not symbol:
            enriched.append(pos)
            continue

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

        enriched.append(pos_enriched)

    return enriched
