"""
Abstracción del proveedor de datos de mercado.

Contrato (MarketProvider):
  - get_ticker_info(symbol) -> dict | None
        Info cruda del proveedor. None si no hay datos válidos (símbolo
        inexistente, respuesta vacía o sin precio real).
  - get_quote(symbol) -> dict
        Cotización normalizada. Si no hay datos válidos retorna
        {"error": ..., "symbol": ...} — el resto del código distingue por
        la presencia de la key "error".
  - get_history(symbol, period) -> dict
        Historial normalizado (dates/close/open/high/low/volume) o
        {"error": ...}.

Los valores inválidos del proveedor (0/None/vacíos) se traducen SIEMPRE a
None / {"error": ...} acá adentro: los consumidores nunca ven datos en cero.

El cache TTL NO vive acá: es responsabilidad de la fachada (market_data.py),
así es agnóstico del provider activo.

Selección: get_provider() lee MARKET_PROVIDER de config (default "yfinance",
único implementado hoy). Para agregar otro proveedor: implementar la interfaz
y registrarlo en _PROVIDERS.
"""
import logging

import yfinance as yf

logger = logging.getLogger("market_provider")


class MarketProvider:
    """Interfaz del proveedor de datos de mercado."""

    name = "base"

    def get_ticker_info(self, symbol: str) -> dict | None:
        raise NotImplementedError

    def get_quote(self, symbol: str) -> dict:
        raise NotImplementedError

    def get_history(self, symbol: str, period: str = "1y") -> dict:
        raise NotImplementedError


class YFinanceProvider(MarketProvider):
    """Implementación sobre yfinance (gratuito, sin API key)."""

    name = "yfinance"

    def get_ticker_info(self, symbol: str) -> dict | None:
        """Info cruda de yfinance. Retorna None si falla, viene vacía o no
        trae un precio real (0/None): nunca info inválida."""
        symbol = symbol.upper()
        try:
            info = yf.Ticker(symbol).info
        except Exception:
            return None

        price = (info or {}).get("currentPrice") or (info or {}).get("regularMarketPrice")
        if not info or not price:
            return None
        return info

    def get_quote(self, symbol: str) -> dict:
        """Precio actual + métricas básicas, o {"error": ..., "symbol": ...}."""
        symbol = symbol.upper()
        info = self.get_ticker_info(symbol)
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

    def get_history(self, symbol: str, period: str = "1y") -> dict:
        """Historial de precios normalizado, o {"error": ...}.
        Periods: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max"""
        symbol = symbol.upper()
        try:
            hist = yf.Ticker(symbol).history(period=period)

            if hist.empty:
                return {"error": f"No hay datos históricos para {symbol}"}

            return {
                "symbol": symbol,
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


# ── Registro y selección del provider activo ─────────────────────────────────

_PROVIDERS = {
    "yfinance": YFinanceProvider,
}

_active_provider: MarketProvider | None = None


def get_provider() -> MarketProvider:
    """Retorna el provider activo (singleton) según MARKET_PROVIDER de config.
    Si el nombre configurado no existe, cae a yfinance con un warning."""
    global _active_provider
    if _active_provider is not None:
        return _active_provider

    try:
        import config
        name = (getattr(config, "MARKET_PROVIDER", "yfinance") or "yfinance").lower()
    except Exception:
        name = "yfinance"

    cls = _PROVIDERS.get(name)
    if cls is None:
        logger.warning("MARKET_PROVIDER=%r no implementado, usando yfinance", name)
        cls = YFinanceProvider

    _active_provider = cls()
    return _active_provider
