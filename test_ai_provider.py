"""
Test rápido del AI Provider con Groq.
Uso: python test_ai_provider.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import config
from modules.ai_provider import AIProvider


def test_status():
    print("=" * 60)
    print("TEST 1: Estado del proveedor")
    print("=" * 60)
    provider = AIProvider(
        provider=config.AI_PROVIDER,
        groq_api_key=config.GROQ_API_KEY,
        groq_model_analysis=config.GROQ_MODEL_ANALYSIS,
        groq_model_fast=config.GROQ_MODEL_FAST,
        anthropic_api_key=config.ANTHROPIC_API_KEY,
        anthropic_model=config.CLAUDE_MODEL,
    )
    status = provider.status()
    print(f"  Proveedor activo: {status['active_provider']}")
    print(f"  Groq configurado: {status['groq']['configured']}")
    if status['groq'].get('connected') is not None:
        print(f"  Groq conectado: {status['groq']['connected']}")
    print(f"  Anthropic configurado: {status['anthropic']['configured']}")
    return status['active_provider'] != 'none'


def test_generation():
    print("\n" + "=" * 60)
    print("TEST 2: Generación de texto (pregunta simple)")
    print("=" * 60)
    provider = AIProvider(
        provider=config.AI_PROVIDER,
        groq_api_key=config.GROQ_API_KEY,
        groq_model_analysis=config.GROQ_MODEL_ANALYSIS,
        groq_model_fast=config.GROQ_MODEL_FAST,
        anthropic_api_key=config.ANTHROPIC_API_KEY,
        anthropic_model=config.CLAUDE_MODEL,
    )
    response, used = provider.generate_with_fallback(
        prompt="En una oración, ¿qué es el RSI en análisis técnico?",
        max_tokens=200,
        tier="fast",
    )
    print(f"  Proveedor usado: {used}")
    print(f"  Respuesta: {response[:300]}")
    return True


def test_json_analysis():
    print("\n" + "=" * 60)
    print("TEST 3: Generación de JSON (formato de análisis)")
    print("=" * 60)
    provider = AIProvider(
        provider=config.AI_PROVIDER,
        groq_api_key=config.GROQ_API_KEY,
        groq_model_analysis=config.GROQ_MODEL_ANALYSIS,
        groq_model_fast=config.GROQ_MODEL_FAST,
        anthropic_api_key=config.ANTHROPIC_API_KEY,
        anthropic_model=config.CLAUDE_MODEL,
    )
    import json
    prompt = """Analizá brevemente NVDA y respondé SOLO con JSON válido:
{
  "symbol": "NVDA",
  "recommendation": "COMPRAR" | "MANTENER" | "VENDER",
  "summary": "resumen en 1 oración",
  "strengths": ["fortaleza 1"],
  "risks": ["riesgo 1"]
}
IMPORTANTE: Solo JSON, sin texto adicional."""

    response, used = provider.generate_with_fallback(
        prompt=prompt,
        max_tokens=500,
        tier="analysis",
    )
    print(f"  Proveedor usado: {used}")

    raw = response.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    if not raw.startswith("{"):
        start = raw.find("{")
        if start != -1:
            raw = raw[start:]

    try:
        parsed = json.loads(raw)
        print(f"  JSON válido: ✅")
        print(f"  Recomendación: {parsed.get('recommendation')}")
        print(f"  Resumen: {parsed.get('summary')}")
        return True
    except json.JSONDecodeError as e:
        print(f"  JSON válido: ❌ ({e})")
        print(f"  Raw: {raw[:300]}")
        return False


if __name__ == "__main__":
    print("\n--- Testing AI Provider ---\n")

    if not test_status():
        print("\n❌ No hay proveedor configurado. Editá config.py con tu GROQ_API_KEY.")
        print("   Obtené una gratis en: https://console.groq.com/keys")
        sys.exit(1)

    try:
        test_generation()
    except Exception as e:
        print(f"  ❌ Error: {e}")

    try:
        ok = test_json_analysis()
    except Exception as e:
        print(f"  ❌ Error: {e}")
        ok = False

    print("\n" + "=" * 60)
    if ok:
        print("✅ AI Provider funcionando correctamente")
    else:
        print("⚠️  Hay problemas — revisá los errores arriba")
    print("=" * 60)
