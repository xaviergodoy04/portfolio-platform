"""
News Collector
==============
Recolecta noticias de RSS y Reddit sin usar LLM.
Tagea cada noticia como 'IA' o 'MERCADOS' según la fuente.
Rankea por relevancia (0-10) usando palabras clave y fuente.
"""

import ssl
import certifi
import feedparser
import httpx
import asyncio
import re
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from modules.news.models import NewsItem

# Fix SSL en macOS: usar los certificados de certifi
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())


# ── Secciones ─────────────────────────────────────────────────────────────────
# Orden en que se muestran / iteran. Cada noticia termina en exactamente una.
SECTIONS = ["TECH_IA", "IA_TECNICA", "STARTUPS", "VOCES", "MERCADOS"]


# ── Fuentes ───────────────────────────────────────────────────────────────────
# Cada fuente declara su sección "natural". Las fuentes especializadas
# (MERCADOS / IA_TECNICA / STARTUPS / VOCES) se quedan en su carril; las de
# TECH_IA (tech/IA general) se rutean por contenido en classify_section().
# Tuplas: (nombre, url, sección_por_defecto)

RSS_SOURCES = [
    # ── TECH_IA — IA / tecnología general (catch-all, se rutea por contenido) ──
    ("TechCrunch AI",       "https://techcrunch.com/category/artificial-intelligence/feed/", "TECH_IA"),
    ("The Verge AI",        "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml", "TECH_IA"),
    ("Ars Technica",        "https://feeds.arstechnica.com/arstechnica/technology-lab", "TECH_IA"),
    ("VentureBeat AI",      "https://venturebeat.com/category/ai/feed/", "TECH_IA"),
    ("VentureBeat",         "https://venturebeat.com/feed/", "TECH_IA"),
    ("Wired AI",            "https://www.wired.com/feed/tag/artificial-intelligence/latest/rss", "TECH_IA"),
    ("MIT Tech Review",     "https://www.technologyreview.com/feed/", "TECH_IA"),

    # ── IA_TECNICA — research, modelos, sistemas ──
    ("ArXiv cs.AI",         "http://export.arxiv.org/rss/cs.AI", "IA_TECNICA"),
    ("ArXiv cs.LG",         "http://export.arxiv.org/rss/cs.LG", "IA_TECNICA"),
    ("ArXiv cs.CL",         "http://export.arxiv.org/rss/cs.CL", "IA_TECNICA"),
    ("Hugging Face",        "https://huggingface.co/blog/feed.xml", "IA_TECNICA"),
    ("Google AI",           "https://blog.google/technology/ai/rss/", "IA_TECNICA"),
    ("OpenAI Blog",         "https://openai.com/blog/rss.xml", "IA_TECNICA"),

    # ── STARTUPS — venture, funding, founders ──
    ("TechCrunch Startups", "https://techcrunch.com/category/startups/feed/", "STARTUPS"),
    ("TechCrunch Venture",  "https://techcrunch.com/category/venture/feed/", "STARTUPS"),
    ("Crunchbase News",     "https://news.crunchbase.com/feed/", "STARTUPS"),

    # ── VOCES — referentes de la IA (blogs / newsletters) ──
    ("Simon Willison",      "https://simonwillison.net/atom/everything/", "VOCES"),
    ("Lilian Weng",         "https://lilianweng.github.io/index.xml", "VOCES"),
    ("Ethan Mollick",       "https://www.oneusefulthing.org/feed", "VOCES"),
    ("Sebastian Raschka",   "https://magazine.sebastianraschka.com/feed", "VOCES"),
    ("Import AI",           "https://importai.substack.com/feed", "VOCES"),
    ("HN · IA",             "https://hnrss.org/newest?q=AI+OR+LLM+OR+OpenAI+OR+Anthropic&points=80", "VOCES"),

    # ── MERCADOS ──
    ("Reuters Business",    "https://feeds.reuters.com/reuters/businessNews", "MERCADOS"),
    ("Reuters Markets",     "https://feeds.reuters.com/reuters/financialNews", "MERCADOS"),
    ("CNBC Top News",       "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "MERCADOS"),
    ("CNBC Finance",        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", "MERCADOS"),
    ("MarketWatch",         "https://feeds.content.dowjones.io/public/rss/mw_topstories", "MERCADOS"),
    ("Yahoo Finance",       "https://finance.yahoo.com/news/rssindex", "MERCADOS"),
    ("Federal Reserve",     "https://www.federalreserve.gov/feeds/press_all.xml", "MERCADOS"),
    ("Investing.com",       "https://www.investing.com/rss/news.rss", "MERCADOS"),
]

REDDIT_SOURCES = [
    ("Reddit/artificial",      "https://www.reddit.com/r/artificial/hot.json?limit=25", "TECH_IA"),
    ("Reddit/MachineLearning", "https://www.reddit.com/r/MachineLearning/hot.json?limit=25", "IA_TECNICA"),
    ("Reddit/LocalLLaMA",      "https://www.reddit.com/r/LocalLLaMA/hot.json?limit=25", "IA_TECNICA"),
    ("Reddit/startups",        "https://www.reddit.com/r/startups/hot.json?limit=20", "STARTUPS"),
    ("Reddit/investing",       "https://www.reddit.com/r/investing/hot.json?limit=20", "MERCADOS"),
    ("Reddit/stocks",          "https://www.reddit.com/r/stocks/hot.json?limit=20", "MERCADOS"),
    ("Reddit/economics",       "https://www.reddit.com/r/economics/hot.json?limit=15", "MERCADOS"),
]


# ── Keywords de relevancia ────────────────────────────────────────────────────

HIGH_IMPACT_IA = {
    "gpt", "llm", "large language model", "artificial intelligence", "deep learning",
    "neural network", "openai", "anthropic", "gemini", "claude", "mistral",
    "nvidia", "semiconductor", "chip", "gpu", "ai regulation", "agi",
    "foundation model", "multimodal", "inference", "training", "model release",
    "spacex", "starlink", "acquisition", "billion", "funding",
}

HIGH_IMPACT_MERCADOS = {
    "federal reserve", "fed rate", "interest rate", "rate cut", "rate hike",
    "jerome powell", "fomc", "inflation", "cpi", "pce",
    "recession", "gdp", "unemployment", "jobs report", "non-farm payroll",
    "yield curve", "treasury", "earnings beat", "earnings miss",
    "merger", "acquisition", "bankruptcy", "ipo", "tariff", "sanctions",
}

MEDIUM_IMPACT = {
    "market", "stock", "shares", "nasdaq", "s&p", "dow", "rally",
    "selloff", "correction", "analyst", "upgrade", "downgrade", "price target",
    "revenue", "profit", "guidance", "quarterly", "sector",
}

# Señales de contenido para rutear noticias de TECH_IA (general) a una sección
# más específica. Se buscan como palabra completa (ver _has_kw).
RESEARCH_KW = {
    "model release", "open source model", "open-source model", "open weights",
    "benchmark", "fine-tune", "fine-tuning", "fine tuned", "parameters",
    "paper", "arxiv", "weights", "architecture", "training run", "pretraining",
    "state-of-the-art", "sota", "inference", "multimodal", "context window",
    "transformer", "diffusion", "rlhf", "quantization", "embedding",
    "checkpoint", "dataset", "reinforcement learning", "reasoning model",
    "large language model", "llm", "neural network", "agentic",
}

STARTUP_KW = {
    "raises", "raised", "funding round", "seed round", "series a", "series b",
    "series c", "valuation", "y combinator", "startup", "founders", "founder",
    "venture capital", "pre-seed", "term sheet", "angel round", "spinout",
    "fundraise", "vc firm", "raising",
}


def _has_kw(text: str, kws) -> bool:
    """True si alguna keyword aparece como palabra completa en el texto (lowercase)."""
    for kw in kws:
        if re.search(r'(?<![a-z])' + re.escape(kw) + r'(?![a-z])', text):
            return True
    return False


def classify_section(title: str, summary: str, source_section: str) -> str:
    """
    Rutea una noticia a exactamente una sección.
    Las fuentes especializadas se quedan en su carril; las de TECH_IA (general)
    se promueven a una sección más específica según su contenido.
    Prioridad para el catch-all: MERCADOS > IA_TECNICA > STARTUPS > TECH_IA.
    """
    if source_section in ("VOCES", "MERCADOS", "IA_TECNICA", "STARTUPS"):
        return source_section

    text = (title + " " + summary).lower()
    if _has_kw(text, HIGH_IMPACT_MERCADOS):
        return "MERCADOS"
    if _has_kw(text, RESEARCH_KW):
        return "IA_TECNICA"
    if _has_kw(text, STARTUP_KW):
        return "STARTUPS"
    return "TECH_IA"

UNIVERSE_SYMBOLS = [
    "NVDA", "MSFT", "GOOGL", "META", "AMD", "PLTR", "CRM", "AAPL", "TSLA", "AMZN",
    "LMT", "RTX", "NOC", "GD", "BA", "LLY", "UNH", "JNJ", "ABBV", "MRK",
    "JPM", "GS", "V", "MA", "XOM", "CVX", "COP",
]

# Nombres / alias de empresa → símbolo. Los títulos casi nunca usan el ticker
# (dicen "Nvidia", no "NVDA"), así que mapeamos los nombres comunes para no
# perder menciones reales. Se buscan como palabra completa (word boundary).
SYMBOL_ALIASES = {
    "NVDA": ["nvidia"],
    "MSFT": ["microsoft"],
    "GOOGL": ["google", "alphabet", "deepmind", "waymo"],
    "META": ["meta platforms", "facebook", "instagram", "whatsapp"],
    "AMD": ["amd", "advanced micro devices"],
    "PLTR": ["palantir"],
    "CRM": ["salesforce"],
    "AAPL": ["apple"],
    "TSLA": ["tesla"],
    "AMZN": ["amazon"],
    "LMT": ["lockheed", "lockheed martin"],
    "RTX": ["raytheon", "rtx"],
    "NOC": ["northrop", "northrop grumman"],
    "GD": ["general dynamics"],
    "BA": ["boeing"],
    "LLY": ["eli lilly"],
    "UNH": ["unitedhealth", "united health"],
    "JNJ": ["johnson & johnson"],
    "ABBV": ["abbvie"],
    "MRK": ["merck"],
    "JPM": ["jpmorgan", "jp morgan"],
    "GS": ["goldman sachs"],
    "V": ["visa"],
    "MA": ["mastercard"],
    "XOM": ["exxon", "exxonmobil"],
    "CVX": ["chevron"],
    "COP": ["conocophillips"],
}


def _detect_symbols(title: str, summary: str) -> list[str]:
    """
    Detecta tickers mencionados sin falsos positivos.
      - Ticker: como palabra suelta y en mayúsculas ("NVDA", "$V"), case-sensitive.
        Así "V" no matchea "government" ni "GS" matchea "things".
      - Alias: nombre de empresa como palabra completa, case-insensitive.
    """
    raw = title + " " + summary
    low = raw.lower()
    found = []
    for sym in UNIVERSE_SYMBOLS:
        # Ticker exacto en mayúsculas, opcionalmente con $, rodeado de no-letras
        ticker_re = r'(?<![A-Za-z])\$?' + re.escape(sym) + r'(?![A-Za-z0-9])'
        hit = re.search(ticker_re, raw) is not None
        # Alias de empresa como palabra completa
        if not hit:
            for alias in SYMBOL_ALIASES.get(sym, []):
                if re.search(r'(?<![a-z])' + re.escape(alias) + r'(?![a-z])', low):
                    hit = True
                    break
        if hit:
            found.append(sym)
    return found


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(date_str) -> datetime:
    if isinstance(date_str, datetime):
        return date_str
    if hasattr(date_str, 'tm_year'):
        try:
            return datetime(*date_str[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _score(title: str, summary: str, section: str, source_weight: float) -> tuple[float, list[str]]:
    """Score 0-10: base por fuente + keywords + símbolos mencionados."""
    text = (title + " " + summary).lower()
    score = source_weight * 3

    high_kw = HIGH_IMPACT_MERCADOS if section == "MERCADOS" else HIGH_IMPACT_IA
    for kw in high_kw:
        if kw in text:
            score += 2
            break

    for kw in MEDIUM_IMPACT:
        if kw in text:
            score += 1
            break

    symbols_found = _detect_symbols(title, summary)
    score += 0.5 * len(symbols_found)

    return min(10.0, round(score, 1)), symbols_found


def _deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    unique = []
    for item in items:
        is_dup = False
        for existing in unique:
            ratio = SequenceMatcher(None, item.title.lower(), existing.title.lower()).ratio()
            if ratio > 0.75:
                if item.relevance_score > existing.relevance_score:
                    unique.remove(existing)
                    unique.append(item)
                is_dup = True
                break
        if not is_dup:
            unique.append(item)
    return unique


# ── Recolectores ──────────────────────────────────────────────────────────────

# Fuentes que postean poco (blogs/research): se les da una ventana más amplia
# para que la sección no quede vacía.
SLOW_SECTIONS = ("VOCES", "IA_TECNICA")


def _collect_rss(sources: list, max_age_hours: int) -> list[NewsItem]:
    """Parsea todos los feeds RSS. Cada item conserva la sección de su fuente."""
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    fed_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    slow_cutoff = datetime.now(timezone.utc) - timedelta(days=5)

    for source_name, url, section in sources:
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                continue

            is_tier1 = any(t in source_name for t in ["Reuters", "CNBC", "Federal"])
            is_fed = "Federal" in source_name
            # Fuentes autoritativas / curadas pesan más para no perderlas en el filtro
            curated = is_tier1 or section in SLOW_SECTIONS
            source_weight = 1.0 if curated else 0.8

            if is_fed:
                age_cutoff = fed_cutoff
            elif section in SLOW_SECTIONS:
                age_cutoff = slow_cutoff
            else:
                age_cutoff = cutoff

            for entry in feed.entries[:20]:
                pub_date = _parse_date(entry.get("published_parsed") or entry.get("updated_parsed"))
                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=timezone.utc)

                if pub_date < age_cutoff:
                    continue

                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                url_entry = entry.get("link", "")

                if not title or not url_entry:
                    continue

                if is_fed:
                    score, symbols = 9.5, []
                else:
                    score, symbols = _score(title, summary, section, source_weight)

                items.append(NewsItem(
                    title=title,
                    summary=summary[:400],
                    url=url_entry,
                    source=source_name,
                    published=pub_date,
                    relevance_score=score,
                    symbols_mentioned=symbols,
                    section=section,
                ))

        except Exception as e:
            print(f"  ⚠️  {source_name}: {e}")

    return items


async def _collect_reddit(sources: list, max_age_hours: int) -> list[NewsItem]:
    """Recolecta posts de Reddit. Cada item conserva la sección de su subreddit."""
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    headers = {"User-Agent": "InvestmentPlatform/1.0"}

    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        for source_name, url, section in sources:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue

                posts = resp.json().get("data", {}).get("children", [])
                for post in posts:
                    p = post.get("data", {})
                    if p.get("score", 0) < 100:  # Solo posts con buen engagement
                        continue

                    created = datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)
                    if created < cutoff:
                        continue

                    title = p.get("title", "").strip()
                    summary = p.get("selftext", "")[:300]
                    url_post = f"https://reddit.com{p.get('permalink', '')}"

                    score, symbols = _score(title, summary, section, source_weight=0.6)

                    items.append(NewsItem(
                        title=title,
                        summary=summary,
                        url=url_post,
                        source=source_name,
                        published=created,
                        relevance_score=score,
                        symbols_mentioned=symbols,
                        section=section,
                    ))

            except Exception as e:
                print(f"  ⚠️  {source_name}: {e}")

    return items


# ── Función principal ─────────────────────────────────────────────────────────

async def collect_all(
    max_per_section: int = 80,
    min_score: float = 2.0,
    max_age_hours: int = 36,
) -> dict:
    """
    Recolecta de todas las fuentes a un pool único, deduplica globalmente (una
    noticia no puede aparecer en dos secciones), rutea cada noticia a UNA sección
    y ordena cada sección por relevancia.

    Retorna: { "TECH_IA": [...], "IA_TECNICA": [...], "STARTUPS": [...],
               "VOCES": [...], "MERCADOS": [...] }
    """
    print("📡 Recolectando noticias...")

    rss_items, reddit_items = await asyncio.gather(
        asyncio.to_thread(_collect_rss, RSS_SOURCES, max_age_hours),
        _collect_reddit(REDDIT_SOURCES, max_age_hours),
    )

    pool = rss_items + reddit_items
    # Dedup global: garantiza que la misma noticia no quede en 2 secciones
    unique = _deduplicate(pool)

    results = {s: [] for s in SECTIONS}
    for item in unique:
        if item.relevance_score < min_score:
            continue
        item.section = classify_section(item.title, item.summary, item.section)
        results.setdefault(item.section, []).append(item)

    for section in results:
        results[section].sort(key=lambda x: (x.relevance_score, x.published), reverse=True)
        results[section] = results[section][:max_per_section]
        print(f"  → {section}: {len(results[section])} noticias")

    # Orden estable de secciones
    return {s: results.get(s, []) for s in SECTIONS}


# ── Test standalone ───────────────────────────────────────────────────────────
# Correr: python -m modules.news.collector

if __name__ == "__main__":
    async def test():
        data = await collect_all()
        # Chequeo de dedup global entre secciones
        seen = {}
        dupes = 0
        for section, items in data.items():
            for it in items:
                key = it.title.lower().strip()
                if key in seen:
                    dupes += 1
                    print(f"  ⚠️  DUP entre [{seen[key]}] y [{section}]: {it.title[:60]}")
                seen[key] = section

        for section, items in data.items():
            print(f"\n{'='*55}")
            print(f"  {section} — {len(items)} noticias")
            print('='*55)
            for i, item in enumerate(items[:12], 1):
                syms = f" [{', '.join(item.symbols_mentioned)}]" if item.symbols_mentioned else ""
                print(f"{i}. [{item.relevance_score}] {item.title}{syms}")
                print(f"   {item.source} · {item.published.strftime('%d/%m %H:%M')}")
        print(f"\n{'='*55}\nDuplicados exactos entre secciones: {dupes}")

    asyncio.run(test())
