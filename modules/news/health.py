"""
Health check de fuentes de noticias.

Recorre RSS_SOURCES con feedparser y persiste el estado de cada fuente en la
tabla feed_health (db.py). Una fuente está "ok" si el feed parsea y trae al
menos una entry; si no, queda "down" y conserva la fecha del último ok para
saber desde cuándo está caída.

Se usa desde:
  - el job semanal del scheduler (lunes 08:00)
  - GET /api/news/health con ?refresh=true (botón "Chequear ahora")
"""

import socket
from datetime import datetime

import ssl
import certifi
import feedparser

from modules import db
from modules.news.collector import RSS_SOURCES

# Mismo fix SSL que el collector (macOS + certifi)
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

FEED_TIMEOUT_SECONDS = 15


def check_all_feeds() -> list[dict]:
    """
    Chequea todas las fuentes RSS y persiste el resultado en feed_health.
    Tolerante a excepciones: una fuente rota jamás corta el chequeo.
    Retorna la lista de estados ya persistida (misma forma que db.get_feed_health()).
    """
    now = datetime.now().isoformat(timespec="seconds")
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FEED_TIMEOUT_SECONDS)
    try:
        for source_name, url, section in RSS_SOURCES:
            try:
                feed = feedparser.parse(url)
                entries = len(feed.entries)
            except Exception:
                entries = 0
            status = "ok" if entries > 0 else "down"
            db.upsert_feed_health(
                source_name=source_name,
                url=url,
                section=section,
                status=status,
                entries=entries,
                checked_at=now,
            )
    finally:
        socket.setdefaulttimeout(old_timeout)

    # Limpiar fuentes que ya no están en RSS_SOURCES (renombradas / eliminadas)
    db.prune_feed_health([name for name, _, _ in RSS_SOURCES])

    return db.get_feed_health()
