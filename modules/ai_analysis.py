"""
Módulo de análisis con IA — usa AIProvider para soportar múltiples backends.
"""
import json
from modules.market_data import get_quote, get_history
from modules.ai_provider import AIProvider
import pandas as pd


def _build_provider(cfg) -> AIProvider:
    return AIProvider(
        provider=cfg.AI_PROVIDER,
        groq_api_key=cfg.GROQ_API_KEY,
        groq_model_analysis=cfg.GROQ_MODEL_ANALYSIS,
        groq_model_fast=cfg.GROQ_MODEL_FAST,
        anthropic_api_key=cfg.ANTHROPIC_API_KEY,
        anthropic_model=cfg.CLAUDE_MODEL,
    )


def analyze_asset(symbol: str, cfg, portfolio_context: dict = None) -> dict:
    """
    Análisis completo de un activo con IA.
    Incluye análisis fundamental, técnico básico y recomendación.
    """
    try:
        provider = _build_provider(cfg)
    except Exception as e:
        return {"error": f"No hay proveedor de IA configurado: {e}"}

    # Recopilar datos del activo
    quote = get_quote(symbol)
    hist_1y = get_history(symbol, "1y")

    if "error" in quote:
        return {"error": f"No se pudieron obtener datos para {symbol}"}

    # Calcular métricas técnicas básicas
    technical = {}
    if "error" not in hist_1y and hist_1y.get("close"):
        prices = pd.Series(hist_1y["close"])
        technical["sma_50"] = round(prices.tail(50).mean(), 2)
        technical["sma_200"] = round(prices.mean(), 2)
        technical["trend"] = "ALCISTA" if prices.iloc[-1] > prices.mean() else "BAJISTA"
        technical["return_1y"] = round(((prices.iloc[-1] - prices.iloc[0]) / prices.iloc[0]) * 100, 2)
        technical["volatility"] = round(prices.pct_change().std() * (252 ** 0.5) * 100, 2)

        # RSI simple
        delta = prices.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        technical["rsi_14"] = round(rsi.iloc[-1], 1) if not pd.isna(rsi.iloc[-1]) else None

    # Construir prompt con todos los datos
    data_summary = f"""
ACTIVO: {symbol} — {quote.get('name', symbol)}
Sector: {quote.get('sector', 'N/A')} | Industria: {quote.get('industry', 'N/A')}

PRECIO Y MERCADO:
- Precio actual: ${quote.get('price', 'N/A')}
- Cambio hoy: {quote.get('change_pct', 0):.2f}%
- P/E Ratio (trailing): {quote.get('pe_ratio', 'N/A')}
- P/E Ratio (forward): {quote.get('forward_pe', 'N/A')}
- EPS: {quote.get('eps', 'N/A')}
- Market Cap: ${quote.get('market_cap', 0):,.0f}
- Dividend Yield: {(quote.get('dividend_yield') or 0) * 100:.2f}%
- 52w High: ${quote.get('52w_high', 'N/A')} | 52w Low: ${quote.get('52w_low', 'N/A')}
- Volumen promedio: {quote.get('avg_volume', 'N/A'):,}

MÉTRICAS TÉCNICAS (1 año):
- SMA 50: ${technical.get('sma_50', 'N/A')}
- SMA 200: ${technical.get('sma_200', 'N/A')}
- Tendencia: {technical.get('trend', 'N/A')}
- Retorno 1 año: {technical.get('return_1y', 'N/A')}%
- Volatilidad anualizada: {technical.get('volatility', 'N/A')}%
- RSI (14): {technical.get('rsi_14', 'N/A')}
"""

    if portfolio_context and portfolio_context.get("positions"):
        portfolio_symbols = [p["symbol"] for p in portfolio_context["positions"]]
        data_summary += f"\nPORTFOLIO ACTUAL: {', '.join(portfolio_symbols)}"
        data_summary += f"\nValor total portfolio: ${portfolio_context.get('summary', {}).get('total_value', 0):,.2f}"

    prompt = f"""Eres un analista financiero senior. Analizá el siguiente activo y dá una recomendación de inversión clara y fundamentada.

{data_summary}

Por favor respondé con un análisis estructurado en JSON con exactamente este formato:
{{
  "symbol": "{symbol}",
  "recommendation": "COMPRAR" | "MANTENER" | "VENDER" | "ESPERAR",
  "conviction": "ALTA" | "MEDIA" | "BAJA",
  "target_price_range": {{"min": número, "max": número}},
  "time_horizon": "corto plazo (< 3 meses)" | "mediano plazo (3-12 meses)" | "largo plazo (> 1 año)",
  "summary": "resumen ejecutivo en 2-3 oraciones",
  "strengths": ["fortaleza 1", "fortaleza 2", "fortaleza 3"],
  "risks": ["riesgo 1", "riesgo 2", "riesgo 3"],
  "technical_signal": "descripción de la señal técnica",
  "fundamental_signal": "descripción del análisis fundamental",
  "portfolio_fit": "cómo encaja con el portfolio actual (si hay datos)"
}}

IMPORTANTE: Respondé SOLO con el JSON válido, sin texto adicional, sin markdown, sin bloques de código."""

    try:
        response_text, used_provider = provider.generate_with_fallback(
            prompt=prompt,
            max_tokens=1024,
            tier="analysis",
        )

        response_text = response_text.strip()
        # Limpiar posibles ```json ``` del response
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        # A veces el modelo agrega texto antes del JSON
        if not response_text.startswith("{"):
            start = response_text.find("{")
            if start != -1:
                response_text = response_text[start:]

        analysis = json.loads(response_text)
        analysis["data"] = {"quote": quote, "technical": technical}
        analysis["ai_provider"] = used_provider
        return analysis

    except json.JSONDecodeError:
        return {
            "symbol": symbol,
            "recommendation": "ERROR",
            "summary": "No se pudo parsear la respuesta de IA",
            "raw_response": response_text if 'response_text' in locals() else ""
        }
    except Exception as e:
        return {"error": f"Error en análisis IA: {str(e)}"}


def chat_analysis(question: str, cfg, portfolio_data: dict = None,
                  history: list = None, context_symbol: str = None) -> str:
    """
    Chat libre sobre inversiones con contexto del portfolio.

    Parámetros opcionales (retro-compatibles):
      history        — mensajes previos de la conversación
                       [{"role": "user"|"assistant", "content": str}, ...]
      context_symbol — símbolo que el usuario está mirando en la UI, se agrega
                       al prompt como contexto de pantalla.
    """
    try:
        provider = _build_provider(cfg)
    except Exception as e:
        return f"Error: No hay proveedor de IA configurado: {e}"

    system_prompt = """Eres un asistente de inversiones experto. Respondés preguntas sobre mercados,
estrategias de inversión, análisis de activos y gestión de portfolio.
Sos directo y claro. Siempre aclarás que no sos un asesor financiero registrado y que
las decisiones de inversión son responsabilidad del usuario."""

    user_message = question
    if context_symbol:
        user_message = (
            f"(Contexto: el usuario está mirando el activo {context_symbol} "
            f"en la plataforma en este momento.)\n\n{user_message}"
        )
    if portfolio_data and portfolio_data.get("positions"):
        portfolio_summary = json.dumps({
            "positions": [
                {
                    "symbol": p["symbol"],
                    "value": p.get("position_value", 0),
                    "pnl_pct": p.get("unrealized_pnl_pct", 0)
                }
                for p in portfolio_data["positions"]
            ],
            "total_value": portfolio_data.get("summary", {}).get("total_value", 0)
        }, indent=2)
        user_message = f"Mi portfolio actual:\n{portfolio_summary}\n\nPregunta: {user_message}"

    try:
        response, _ = provider.generate_with_fallback(
            prompt=user_message,
            system_prompt=system_prompt,
            max_tokens=1024,
            tier="analysis",
            history=history,
        )
        return response
    except Exception as e:
        return f"Error en análisis IA: {str(e)}"
