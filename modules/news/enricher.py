"""
News Enricher — genera contexto para noticias usando AIProvider.
Usa el tier "fast" (modelo ligero) para mantener velocidad y bajo costo.
"""

import json
from modules.news.models import NewsItem
from modules.ai_provider import AIProvider


PROMPT_TEMPLATE = """Eres un analista financiero y tecnológico.
Para cada noticia de la lista, genera un JSON con dos campos:
- "que_paso": 1 oración concisa del hecho (qué empresa/institución hizo qué)
- "implicacion": 1-2 oraciones de qué significa esto para inversores o para el sector

Responde SOLO con un array JSON válido, sin texto adicional, sin markdown, sin bloques de código.
El array debe tener exactamente {n} elementos en el mismo orden que la lista.

Noticias:
{noticias}"""


def enrich_news(items: list[NewsItem], cfg) -> list[NewsItem]:
    """
    Enriquece una lista de noticias con contexto de IA.
    Retorna la misma lista con el campo `context` completado.
    """
    if not items:
        return items

    try:
        provider = AIProvider(
            provider=cfg.AI_PROVIDER,
            groq_api_key=cfg.GROQ_API_KEY,
            groq_model_analysis=cfg.GROQ_MODEL_ANALYSIS,
            groq_model_fast=cfg.GROQ_MODEL_FAST,
            anthropic_api_key=cfg.ANTHROPIC_API_KEY,
            anthropic_model=cfg.CLAUDE_MODEL,
        )
    except Exception:
        return items

    noticias_texto = "\n".join(
        f"{i+1}. [{item.source}] {item.title}"
        + (f"\nResumen: {item.summary[:200]}" if item.summary else "")
        for i, item in enumerate(items)
    )

    prompt = PROMPT_TEMPLATE.format(n=len(items), noticias=noticias_texto)

    try:
        raw = provider.generate(prompt=prompt, max_tokens=1500, tier="fast")
        raw = raw.strip()

        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        if not raw.startswith("["):
            start = raw.find("[")
            if start != -1:
                raw = raw[start:]

        contexts = json.loads(raw)

        for i, item in enumerate(items):
            if i < len(contexts):
                c = contexts[i]
                que_paso = c.get("que_paso", "")
                implicacion = c.get("implicacion", "")
                item.context = f"{que_paso} {implicacion}".strip()

    except Exception as e:
        print(f"  ⚠️  Enricher error: {e} — noticias sin contexto")

    return items
