"""
Cache disque des affiches Jellyfin.

Sans lui, chaque ouverture de la page /bibliotheque redemande à Jellyfin
plusieurs centaines d'affiches. Ici, une affiche déjà récupérée est relue
depuis le disque (POSTER_CACHE_DIR, dans le volume de données : elle survit aux
redémarrages et aux reconstructions d'image) et ne génère AUCUNE requête
vers Jellyfin.

Clé : identifiant de l'item + largeur demandée + "tag" d'image.
Le tag est la version de l'affiche renvoyée par Jellyfin (ImageTags.Primary) : il
change quand l'affiche change. Une entrée AVEC tag est donc valable indéfiniment
et se renouvelle d'elle-même, sans durée de vie à régler. Une entrée SANS tag
(usage direct de l'API) expire après POSTER_CACHE_TTL_DAYS.
"""
import logging
import os
import re
import tempfile
import time
from typing import Optional, Tuple

from app.config import POSTER_CACHE_DIR, POSTER_CACHE_TTL_DAYS

logger = logging.getLogger("image_cache")

# Un tag Jellyfin est un hash hexadécimal ; on n'accepte que des caractères
# sûrs, puisqu'il entre dans un nom de fichier.
TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_BYTES = 10 * 1024 * 1024  # garde-fou : on ne met pas sur disque une "image" de plusieurs Mo

_EXT_BY_TYPE = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
_TYPE_BY_EXT = {v: k for k, v in _EXT_BY_TYPE.items()}


def enabled() -> bool:
    return bool(POSTER_CACHE_DIR)


def _norm_id(item_id: str) -> Optional[str]:
    n = item_id.replace("-", "").lower()
    return n if _ID_RE.match(n) else None


def _base(item_id: str, width: int, tag: Optional[str]) -> Optional[str]:
    nid = _norm_id(item_id)
    if nid is None:
        return None
    return f"{nid}_{int(width)}_{tag or 'notag'}"


def _files_for(base: str) -> list:
    if not os.path.isdir(POSTER_CACHE_DIR):
        return []
    return [f for f in os.listdir(POSTER_CACHE_DIR) if f.rsplit(".", 1)[0] == base]


def get(item_id: str, width: int, tag: Optional[str] = None) -> Optional[Tuple[bytes, str]]:
    """Retourne (octets, content_type) si l'affiche est en cache et valide, sinon None."""
    if not enabled():
        return None
    base = _base(item_id, width, tag)
    if base is None:
        return None
    for name in _files_for(base):
        path = os.path.join(POSTER_CACHE_DIR, name)
        try:
            if not tag and POSTER_CACHE_TTL_DAYS > 0:
                age_jours = (time.time() - os.path.getmtime(path)) / 86400
                if age_jours > POSTER_CACHE_TTL_DAYS:
                    return None
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if not data:
            return None
        ext = name.rsplit(".", 1)[-1]
        return data, _TYPE_BY_EXT.get(ext, "image/jpeg")
    return None


def put(item_id: str, width: int, tag: Optional[str], content: bytes, content_type: str) -> bool:
    """
    Enregistre une affiche. Écriture atomique (fichier temporaire puis
    renommage) : un lecteur ne voit jamais un fichier à moitié écrit. Les
    anciennes versions de la même affiche (autre tag) sont supprimées.
    """
    if not enabled() or not content or len(content) > _MAX_BYTES:
        return False
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not ctype.startswith("image/"):
        return False
    base = _base(item_id, width, tag)
    if base is None:
        return False
    ext = _EXT_BY_TYPE.get(ctype, "jpg")
    try:
        os.makedirs(POSTER_CACHE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=POSTER_CACHE_DIR, prefix=".tmp_", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp, os.path.join(POSTER_CACHE_DIR, f"{base}.{ext}"))
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        # Même item + même largeur, autre tag (ou sans tag) : version périmée.
        # Préfixe construit directement : un tag peut contenir "_", on ne peut
        # donc pas le déduire en coupant `base` au dernier "_".
        prefix = f"{_norm_id(item_id)}_{int(width)}_"
        for name in os.listdir(POSTER_CACHE_DIR):
            stem = name.rsplit(".", 1)[0]
            if stem.startswith(prefix) and stem != base and not name.startswith(".tmp_"):
                try:
                    os.unlink(os.path.join(POSTER_CACHE_DIR, name))
                except OSError:
                    pass
        return True
    except OSError as e:
        logger.warning("Impossible d'écrire l'affiche en cache (%s) : elle sera redemandée à Jellyfin.", e)
        return False


def clear() -> int:
    """Supprime toutes les affiches en cache. Retourne le nombre de fichiers supprimés."""
    if not enabled() or not os.path.isdir(POSTER_CACHE_DIR):
        return 0
    n = 0
    for name in os.listdir(POSTER_CACHE_DIR):
        try:
            os.unlink(os.path.join(POSTER_CACHE_DIR, name))
            n += 1
        except OSError:
            pass
    logger.info("Cache d'affiches vidé (%d fichier(s) supprimé(s)).", n)
    return n


def stats() -> dict:
    """Nombre de fichiers et taille totale (octets) du cache."""
    if not enabled() or not os.path.isdir(POSTER_CACHE_DIR):
        return {"fichiers": 0, "octets": 0}
    fichiers = [f for f in os.listdir(POSTER_CACHE_DIR) if not f.startswith(".tmp_")]
    octets = 0
    for f in fichiers:
        try:
            octets += os.path.getsize(os.path.join(POSTER_CACHE_DIR, f))
        except OSError:
            pass
    return {"fichiers": len(fichiers), "octets": octets}
