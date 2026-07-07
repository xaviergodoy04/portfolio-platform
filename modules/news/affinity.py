"""
Perfil de afinidad personal de noticias.

Aprende de tu feedback (likes = señal fuerte, leídas = señal débil) qué
fuentes, símbolos y palabras clave te interesan, y calcula un bonus de
relevancia para personalizar el ranking del feed sin tocar el collector
ni el cache base.

- Pesos: like = 3, leída = 1, con decaimiento exponencial (half-life 30 días)
  sobre la fecha en que se dio el feedback: lo que te gustaba hace 3 meses
  pesa mucho menos que lo de esta semana.
- Cada dimensión (fuente, símbolo, keyword) se normaliza por su máximo, así
  el bonus es comparable aunque haya poco o mucho feedback acumulado.
- affinity_bonus() está capeado a +2.0 para que la afinidad reordene dentro
  de una sección pero nunca tape una noticia objetivamente importante.
- El perfil se cachea en memoria 10 minutos y se rebuild-ea lazy; los
  endpoints de feedback lo invalidan para que un like nuevo impacte enseguida.
"""
import re
import threading
import time
from datetime import datetime

from modules import db

LIKE_WEIGHT = 3.0
READ_WEIGHT = 1.0
HALF_LIFE_DAYS = 30.0
FEEDBACK_WINDOW_DAYS = 90
PROFILE_TTL = 10 * 60   # segundos
MAX_BONUS = 2.0

# Stopwords básicas ES/EN: tokens frecuentes sin señal temática en títulos
_STOPWORDS = {
    # español
    "para", "como", "este", "esta", "estos", "estas", "desde", "hasta",
    "entre", "sobre", "según", "segun", "tras", "cuando", "donde", "porque",
    "pero", "más", "menos", "todo", "toda", "todos", "todas", "otro", "otra",
    "años", "año", "dice", "hace", "tiene", "sería", "será", "puede",
    "nueva", "nuevo", "contra", "ante", "cada", "solo", "sólo", "ella",
    "ellos", "nosotros", "ustedes", "quien", "quién", "cual", "cuál",
    # inglés
    "this", "that", "these", "those", "with", "from", "into", "over",
    "after", "before", "about", "against", "between", "during", "under",
    "what", "when", "where", "which", "while", "will", "would", "could",
    "should", "have", "been", "being", "does", "just", "than", "then",
    "them", "they", "their", "there", "here", "more", "most", "some",
    "such", "only", "also", "very", "your", "make", "makes", "made",
    "says", "said", "still", "amid", "year", "years", "week", "month",
    "news", "report", "reports", "today", "back", "down", "cómo", "post",
}

_token_re = re.compile(r"[a-z0-9áéíóúüñ]+")

_profile_lock = threading.Lock()
_profile_cache = {"ts": 0.0, "profile": None}


def _title_tokens(title: str) -> set:
    """Tokens significativos del título: lowercase, sin stopwords, len > 3."""
    words = _token_re.findall((title or "").lower())
    return {w for w in words if len(w) > 3 and w not in _STOPWORDS}


def _decay(ts_iso: str, now: datetime) -> float:
    """Factor de decaimiento exponencial según la antigüedad del feedback."""
    if not ts_iso:
        return 0.0
    try:
        then = datetime.fromisoformat(ts_iso)
    except (ValueError, TypeError):
        return 0.0
    age_days = max((now - then).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def build_profile() -> dict:
    """
    Construye el perfil desde el feedback de los últimos 90 días.
    Retorna {sources, symbols, keywords}, cada uno un dict {clave: afinidad}
    normalizado a [0, 1] (dividido por el máximo de su dimensión).
    """
    rows = db.get_feedback_rows(FEEDBACK_WINDOW_DAYS)
    now = datetime.now()
    sources, symbols, keywords = {}, {}, {}

    for r in rows:
        weight = 0.0
        if r.get("liked"):
            weight += LIKE_WEIGHT * _decay(r.get("liked_at"), now)
        if r.get("read"):
            weight += READ_WEIGHT * _decay(r.get("read_at"), now)
        if weight <= 0:
            continue

        src = (r.get("source") or "").strip().lower()
        if src:
            sources[src] = sources.get(src, 0.0) + weight
        for s in r.get("symbols") or []:
            s = (s or "").strip().upper()
            if s:
                symbols[s] = symbols.get(s, 0.0) + weight
        for tok in _title_tokens(r.get("title")):
            keywords[tok] = keywords.get(tok, 0.0) + weight

    def _normalize(d: dict) -> dict:
        top = max(d.values(), default=0.0)
        return {k: v / top for k, v in d.items()} if top > 0 else {}

    return {
        "sources": _normalize(sources),
        "symbols": _normalize(symbols),
        "keywords": _normalize(keywords),
    }


def _get_profile() -> dict:
    """Perfil cacheado en memoria (TTL 10 min), rebuild lazy y thread-safe."""
    with _profile_lock:
        if (
            _profile_cache["profile"] is None
            or time.time() - _profile_cache["ts"] > PROFILE_TTL
        ):
            _profile_cache["profile"] = build_profile()
            _profile_cache["ts"] = time.time()
        return _profile_cache["profile"]


def invalidate_profile() -> None:
    """Fuerza el rebuild en el próximo uso (se llama al registrar feedback)."""
    with _profile_lock:
        _profile_cache["profile"] = None
        _profile_cache["ts"] = 0.0


def affinity_bonus(item: dict) -> float:
    """
    Bonus de afinidad para un item serializado del feed (dict con source,
    symbols y title). Suma la afinidad normalizada de la fuente (0-1), la del
    mejor símbolo mencionado (0-1) y la de las keywords del título (capeada a
    1.0), con tope total de +2.0.
    """
    profile = _get_profile()
    if not (profile["sources"] or profile["symbols"] or profile["keywords"]):
        return 0.0

    bonus = profile["sources"].get((item.get("source") or "").strip().lower(), 0.0)

    syms = [(s or "").strip().upper() for s in item.get("symbols") or []]
    if syms:
        bonus += max(profile["symbols"].get(s, 0.0) for s in syms)

    tokens = _title_tokens(item.get("title"))
    if tokens:
        kw_score = sum(profile["keywords"].get(t, 0.0) for t in tokens)
        bonus += min(kw_score, 1.0)

    return round(min(bonus, MAX_BONUS), 2)
