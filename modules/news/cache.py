"""
News Cache
==========
Cachea el resultado serializado del endpoint de noticias en un JSON para no
re-fetchear RSS/Reddit (y, sobre todo, no re-llamar a Haiku) en cada visita.

El cache distingue entre la versión sin IA y la enriquecida con IA, porque
enriquecer cuesta dinero y conviene conservarla más tiempo.
"""

import json
import os
import time

CACHE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "news_cache.json")
CACHE_FILE = os.path.abspath(CACHE_FILE)


def _read() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write(store: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
    except Exception as e:
        print(f"  ⚠️  No se pudo escribir cache de noticias: {e}")


def get(key: str, ttl_seconds: int):
    """Retorna (data, age_seconds) si hay cache fresco, o (None, None)."""
    store = _read()
    entry = store.get(key)
    if not entry:
        return None, None
    age = time.time() - entry.get("ts", 0)
    if age > ttl_seconds:
        return None, None
    return entry.get("data"), int(age)


def set(key: str, data) -> None:
    store = _read()
    store[key] = {"ts": time.time(), "data": data}
    _write(store)
