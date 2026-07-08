"""
Versión del build que está corriendo.

Motivación (Fase 2.5): el bug "VOCES no funciona" fue un server viejo con
estado mezclado y costó horas darse cuenta. Con el git hash + branch visibles
en la UI, "¿qué versión está corriendo?" se responde de un vistazo.

El hash/branch se resuelven UNA vez al importar (subprocess git): la versión
de un proceso no cambia mientras vive, y así /api/version no paga un
subprocess por request. Si git no está disponible (o no es un repo), los
campos quedan en None y la UI muestra "desconocida".
"""
import os
import subprocess
from datetime import datetime

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STARTED_AT = datetime.now().isoformat(timespec="seconds")


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", _REPO_DIR, *args],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


GIT_HASH = _git("rev-parse", "--short", "HEAD")
GIT_BRANCH = _git("rev-parse", "--abbrev-ref", "HEAD")
GIT_DATE = _git("log", "-1", "--format=%cs")  # fecha del último commit (YYYY-MM-DD)
GIT_DIRTY = bool(_git("status", "--porcelain"))


def get_version() -> dict:
    """Payload de /api/version. `label` es el string listo para mostrar."""
    if GIT_HASH:
        label = f"{GIT_BRANCH or '?'} @ {GIT_HASH}{'*' if GIT_DIRTY else ''}"
    else:
        label = "versión desconocida"
    return {
        "git_hash": GIT_HASH,
        "git_branch": GIT_BRANCH,
        "git_commit_date": GIT_DATE,
        "dirty": GIT_DIRTY,
        "label": label,
        "started_at": STARTED_AT,
    }
