"""
Radar de Oportunidades — escanea activos y los rankea en 3 dimensiones:
  - entry_score  (0-100): qué tan buena es la entrada técnica AHORA (dip, RSI, momentum)
  - growth_score (0-100): potencial de crecimiento de la empresa
  - risk_score   (0-100): nivel de riesgo (100 = muy arriesgado)
"""
import yfinance as yf
import pandas as pd


UNIVERSE = {
    "Tech / IA": ["NVDA", "MSFT", "GOOGL", "META", "AMD", "PLTR", "CRM", "AAPL", "TSLA", "AMZN"],
    "Defensa":   ["LMT", "RTX", "NOC", "GD", "BA", "HII", "LDOS", "CACI"],
    "Salud":     ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ISRG", "PFE"],
    "Finanzas":  ["JPM", "GS", "MS", "V", "MA", "AXP", "BRK-B", "BAC"],
    "Energía":   ["XOM", "CVX", "COP", "SLB", "EOG", "OXY"],
    "Consumo":   ["COST", "WMT", "HD", "MCD", "SBUX", "NKE"],
}

ALL_SYMBOLS = list({s for syms in UNIVERSE.values() for s in syms})


def _calc_entry_score(info: dict, prices: pd.Series) -> tuple[int, dict]:
    """
    Oportunidad de entrada técnica (dip buying).
    Retorna (score, métricas).
    """
    score = 0.0
    m = {}

    current = info.get("currentPrice") or info.get("regularMarketPrice") or 0

    # Caída desde 52w high (35 pts)
    high_52w = info.get("fiftyTwoWeekHigh")
    if high_52w and current and high_52w > 0:
        drop = (high_52w - current) / high_52w * 100
        m["drop_52w_pct"] = round(drop, 1)
        if drop < 5:       s = 0
        elif drop < 15:    s = drop * 1.0
        elif drop < 35:    s = 15 + (drop - 15) * 1.8
        elif drop < 55:    s = 51 + (drop - 35) * 0.5
        else:              s = max(0, 61 - (drop - 55) * 1.5)
        score += min(35, s * 0.35)
    else:
        m["drop_52w_pct"] = None

    # Caída 30d (25 pts)
    if len(prices) >= 22:
        drop_30 = (prices.iloc[-22] - prices.iloc[-1]) / prices.iloc[-22] * 100
        m["drop_30d_pct"] = round(drop_30, 1)
        if drop_30 > 0:
            score += min(25, drop_30 * 0.8)
    else:
        m["drop_30d_pct"] = None

    # RSI (25 pts)
    if len(prices) >= 15:
        delta = prices.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi_s = 100 - (100 / (1 + rs))
        rsi = rsi_s.iloc[-1]
        if not pd.isna(rsi):
            m["rsi"] = round(rsi, 1)
            if rsi < 30:   score += 25
            elif rsi < 40: score += 20
            elif rsi < 50: score += 10
            elif rsi < 60: score += 3
        else:
            m["rsi"] = None
    else:
        m["rsi"] = None

    # P/E forward mejora (15 pts)
    pe = info.get("trailingPE")
    fpe = info.get("forwardPE")
    m["pe"] = round(pe, 1) if pe else None
    m["forward_pe"] = round(fpe, 1) if fpe else None
    if pe and fpe and pe > 0 and fpe > 0:
        imp = (pe - fpe) / pe * 100
        if imp > 20:   score += 15
        elif imp > 10: score += 10
        elif imp > 0:  score += 5
    elif pe and pe > 0:
        if pe < 15:    score += 15
        elif pe < 25:  score += 8

    return min(100, round(score)), m


def _calc_growth_score(info: dict) -> tuple[int, dict]:
    """
    Potencial de crecimiento de la empresa.
    100 = altísimo potencial de crecimiento.
    """
    score = 0.0
    m = {}

    # Revenue growth YoY (25 pts)
    rev_growth = info.get("revenueGrowth")  # decimal, ej: 0.23 = 23%
    if rev_growth is not None:
        m["revenue_growth_pct"] = round(rev_growth * 100, 1)
        if rev_growth > 0.50:   score += 25
        elif rev_growth > 0.25: score += 20
        elif rev_growth > 0.10: score += 13
        elif rev_growth > 0:    score += 6
        else:                   score += 0  # decrecimiento
    else:
        m["revenue_growth_pct"] = None

    # EPS growth (20 pts)
    eps_growth = info.get("earningsGrowth")
    if eps_growth is not None:
        m["eps_growth_pct"] = round(eps_growth * 100, 1)
        if eps_growth > 0.50:   score += 20
        elif eps_growth > 0.20: score += 15
        elif eps_growth > 0.05: score += 8
        elif eps_growth > 0:    score += 3
    else:
        m["eps_growth_pct"] = None

    # Precio objetivo analistas vs actual (25 pts)
    target = info.get("targetMeanPrice")
    current = info.get("currentPrice") or info.get("regularMarketPrice")
    if target and current and current > 0:
        upside = (target - current) / current * 100
        m["analyst_upside_pct"] = round(upside, 1)
        m["analyst_target"] = round(target, 2)
        if upside > 40:    score += 25
        elif upside > 20:  score += 18
        elif upside > 10:  score += 10
        elif upside > 0:   score += 4
    else:
        m["analyst_upside_pct"] = None
        m["analyst_target"] = None

    # ROE (15 pts) — rentabilidad sobre capital
    roe = info.get("returnOnEquity")
    if roe is not None:
        m["roe_pct"] = round(roe * 100, 1)
        if roe > 0.30:   score += 15
        elif roe > 0.15: score += 10
        elif roe > 0:    score += 5
    else:
        m["roe_pct"] = None

    # Margen bruto (15 pts)
    gross_margin = info.get("grossMargins")
    if gross_margin is not None:
        m["gross_margin_pct"] = round(gross_margin * 100, 1)
        if gross_margin > 0.60:   score += 15
        elif gross_margin > 0.40: score += 10
        elif gross_margin > 0.20: score += 5
    else:
        m["gross_margin_pct"] = None

    return min(100, round(score)), m


def _calc_risk_score(info: dict, prices: pd.Series) -> tuple[int, dict]:
    """
    Nivel de riesgo. 100 = extremadamente riesgoso.
    Alto riesgo no es malo — es información para decidir posición.
    """
    score = 0.0
    m = {}

    # Beta (25 pts) — volatilidad vs mercado
    beta = info.get("beta")
    if beta is not None:
        m["beta"] = round(beta, 2)
        if beta > 2.5:     score += 25
        elif beta > 1.8:   score += 20
        elif beta > 1.3:   score += 13
        elif beta > 1.0:   score += 7
        elif beta > 0.7:   score += 3
    else:
        m["beta"] = None
        score += 10  # sin datos = asumimos riesgo moderado-alto

    # Deuda/Equity (20 pts)
    de = info.get("debtToEquity")
    if de is not None:
        m["debt_equity"] = round(de, 1)
        if de > 200:      score += 20
        elif de > 100:    score += 14
        elif de > 50:     score += 8
        elif de > 20:     score += 4
    else:
        m["debt_equity"] = None
        score += 5

    # Márgenes operativos negativos (20 pts)
    op_margin = info.get("operatingMargins")
    if op_margin is not None:
        m["operating_margin_pct"] = round(op_margin * 100, 1)
        if op_margin < -0.20:  score += 20
        elif op_margin < -0.10: score += 15
        elif op_margin < 0:     score += 10
        elif op_margin < 0.05:  score += 4
    else:
        m["operating_margin_pct"] = None
        score += 5

    # Market cap (15 pts) — small cap = más riesgo
    mcap = info.get("marketCap") or 0
    m["market_cap"] = mcap
    if mcap < 300_000_000:        score += 15   # micro cap
    elif mcap < 2_000_000_000:    score += 12   # small cap
    elif mcap < 10_000_000_000:   score += 7    # mid cap
    elif mcap < 50_000_000_000:   score += 3    # large cap
    # mega cap = 0

    # Volatilidad histórica 90d (20 pts)
    if len(prices) >= 63:
        vol_90d = prices.tail(63).pct_change().std() * (252 ** 0.5) * 100
        m["volatility_90d"] = round(vol_90d, 1)
        if vol_90d > 80:    score += 20
        elif vol_90d > 50:  score += 15
        elif vol_90d > 35:  score += 10
        elif vol_90d > 20:  score += 5
    else:
        m["volatility_90d"] = None

    return min(100, round(score)), m


def _risk_label(score: int) -> str:
    if score >= 75: return "MUY ALTO"
    if score >= 55: return "ALTO"
    if score >= 35: return "MODERADO"
    return "BAJO"

def _growth_label(score: int) -> str:
    if score >= 70: return "ALTO"
    if score >= 45: return "MODERADO"
    if score >= 25: return "LIMITADO"
    return "BAJO"


def scan(extra_symbols: list = None) -> dict:
    symbols_to_scan = list(ALL_SYMBOLS)
    if extra_symbols:
        for s in extra_symbols:
            s = s.upper().strip()
            if s and s not in symbols_to_scan:
                symbols_to_scan.append(s)

    results = []
    errors = []

    for symbol in symbols_to_scan:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            hist = ticker.history(period="1y")

            current = info.get("currentPrice") or info.get("regularMarketPrice")
            if not info or not current:
                errors.append(symbol)
                continue

            prices = pd.Series(hist["Close"].tolist()) if not hist.empty else pd.Series([])

            entry_score, entry_m = _calc_entry_score(info, prices)
            growth_score, growth_m = _calc_growth_score(info)
            risk_score, risk_m = _calc_risk_score(info, prices)

            # Sector del universo curado
            sector_label = info.get("sector") or "Otro"
            for sec, syms in UNIVERSE.items():
                if symbol in syms:
                    sector_label = sec
                    break
            if extra_symbols and symbol in [s.upper() for s in extra_symbols]:
                if symbol not in ALL_SYMBOLS:
                    sector_label = info.get("sector") or "Watchlist"

            result = {
                "symbol": symbol,
                "name": info.get("longName") or info.get("shortName", ""),
                "sector_label": sector_label,
                "current_price": current,
                "change_pct_today": info.get("regularMarketChangePercent", 0),
                "52w_high": info.get("fiftyTwoWeekHigh"),
                "52w_low": info.get("fiftyTwoWeekLow"),
                "market_cap": info.get("marketCap"),
                # Scores
                "entry_score": entry_score,
                "growth_score": growth_score,
                "risk_score": risk_score,
                "risk_label": _risk_label(risk_score),
                "growth_label": _growth_label(growth_score),
                # Métricas detalladas
                **entry_m, **growth_m, **risk_m,
            }
            results.append(result)

        except Exception:
            errors.append(symbol)

    # Ordenar por entry_score por defecto
    results.sort(key=lambda x: x.get("entry_score", 0), reverse=True)

    by_sector = {}
    for r in results:
        sec = r["sector_label"]
        by_sector.setdefault(sec, []).append(r)

    return {
        "results": results,
        "by_sector": by_sector,
        "top_10": results[:10],
        # Top por perfil
        "top_growth": sorted(results, key=lambda x: x.get("growth_score", 0), reverse=True)[:10],
        "top_riskreward": sorted(
            results,
            key=lambda x: x.get("entry_score", 0) * 0.4 + x.get("growth_score", 0) * 0.6,
            reverse=True
        )[:10],
        "errors": errors,
        "scanned": len(results),
    }
