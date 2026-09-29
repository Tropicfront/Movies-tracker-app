"""
Cache disque des résultats AlloCiné (note presse/spectateurs par titre).

Objectif : la synchro Jellyfin interroge AlloCiné pour chaque titre de la
bibliothèque (recherche + fiche = 2 requêtes). Sur une grosse bibliothèque,
la répéter intégralement à chaque fois est lent (avec la limite de débit
par défaut, ~4 s/titre) et inutile : la note d'un film change rarement.

Ce module stocke le résultat par titre dans un fichier JSON unique, dans le
volume de données (/app/data, persiste entre redémarrages et reconstructions
d'image). Seuls les titres TROUVÉS sont mis en cache : un titre non trouvé
est recherché à nouveau à chaque synchro, sans limite de temps (AlloCiné peut
ajouter la fiche d'un titre entre deux synchros). Une entrée trouvée expire
après ALLOCINE_CACHE_TTL_DAYS jours, après quoi le titre est de nouveau
recherché normalement (la note peut évoluer avec le temps).

Le fichier entier est chargé/réécrit à chaque lecture/écriture (pas de vraie
base de données) : très largement suffisant pour quelques milliers de titres,
et beaucoup plus simple à inspecter/éditer à la main si besoin
(`docker exec ... cat /app/data/allocine_cache.json`).
"""
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Optional

from app.config import ALLOCINE_CACHE_PATH, ALLOCINE_CACHE_TTL_DAYS
from app.matching import normalize_title
from app.models import NoteAlloCine

logger = logging.getLogger("allocine_cache")

_lock = threading.Lock()
_cache: Optional[dict] = None  # chargé paresseusement, gardé en mémoire ensuite


def cache_enabled() -> bool:
    return ALLOCINE_CACHE_TTL_DAYS > 0


def make_key(titre: str, type_: str, annee: Optional[int] = None) -> str:
    """Clé de cache : titre normalisé + type (+ année si connue, pour ne pas
    mélanger deux titres identiques mais d'années différentes)."""
    base = f"{type_}:{normalize_title(titre)}"
    return f"{base}:{annee}" if annee else base


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if not os.path.exists(ALLOCINE_CACHE_PATH):
        _cache = {}
        return _cache
    try:
        with open(ALLOCINE_CACHE_PATH, "r", encoding="utf-8") as f:
            _cache = json.load(f)
        if not isinstance(_cache, dict):
            logger.warning("Cache AlloCiné invalide (pas un objet JSON), réinitialisé.")
            _cache = {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Impossible de lire le cache AlloCiné (%s), réinitialisé.", e)
        _cache = {}
    return _cache


def _save(cache: dict) -> None:
    """Écriture atomique : fichier temporaire puis renommage, pour ne jamais
    laisser un fichier de cache à moitié écrit en cas de coupure/crash."""
    directory = os.path.dirname(ALLOCINE_CACHE_PATH) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".allocine_cache_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, ALLOCINE_CACHE_PATH)
        except BaseException:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        logger.warning("Impossible d'écrire le cache AlloCiné (%s) : résultat non sauvegardé.", e)


def get_cached(titre: str, type_: str, annee: Optional[int] = None) -> Optional[NoteAlloCine]:
    """
    Retourne le résultat en cache pour ce titre s'il existe, correspond à une
    fiche TROUVÉE et n'a pas expiré ; sinon None (auquel cas il faut refaire
    la recherche normalement).

    Un titre non trouvé n'est JAMAIS servi depuis le cache, quel que soit son
    âge : AlloCiné peut ajouter la fiche d'un titre entre deux synchros (sortie
    récente, série en cours...), donc ces titres sont recherchés à chaque
    rescan. Cette règle s'applique même à une entrée "non trouvé" écrite par
    une version antérieure de ce cache (voir set_cached : plus jamais écrite,
    mais peut encore traîner dans un fichier existant).
    """
    if not cache_enabled():
        return None
    key = make_key(titre, type_, annee)
    with _lock:
        entry = _load().get(key)
    if entry is None:
        return None
    try:
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, ValueError):
        return None
    age_days = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 86400
    if age_days > ALLOCINE_CACHE_TTL_DAYS:
        return None
    note = NoteAlloCine(**(entry.get("note") or {}))
    if not note.trouve:
        return None
    return note


def set_cached(titre: str, type_: str, note: NoteAlloCine, annee: Optional[int] = None) -> None:
    """
    Enregistre un résultat TROUVÉ dans le cache. Un résultat non trouvé n'est
    jamais écrit (no-op) : le titre doit être recherché à nouveau à chaque
    synchro, sans limite de temps, au cas où AlloCiné aurait depuis publié sa
    fiche (voir get_cached pour la règle symétrique côté lecture).
    """
    if not cache_enabled() or not note.trouve:
        return
    key = make_key(titre, type_, annee)
    entry = {
        "titre": titre,  # non utilisé pour la clé, gardé pour inspection manuelle du fichier
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": note.model_dump(),
    }
    with _lock:
        cache = _load()
        cache[key] = entry
        _save(cache)


def clear_cache() -> int:
    """Vide entièrement le cache. Retourne le nombre d'entrées supprimées."""
    global _cache
    with _lock:
        cache = _load()
        n = len(cache)
        _cache = {}
        _save(_cache)
    logger.info("Cache AlloCiné vidé (%d entrée(s) supprimée(s)).", n)
    return n
