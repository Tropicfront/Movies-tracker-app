"""
Stockage en mémoire des films scrapés + orchestration du pipeline
(Pathé Toulouse Wilson -> enrichissement AlloCiné).

Les données sont récupérées une seule fois (au démarrage du conteneur, ou
manuellement via POST /refresh) et gardées en mémoire pour être servies par
l'API. Pas de base de données : simple et suffisant pour ce cas d'usage.
"""
import logging
import re
import unicodedata
from datetime import datetime, timezone
from threading import Lock
from typing import List, Optional

from app.models import Film, StatutScraping, StatutSyncJellyfin
from app.scrapers.pathe_scraper import scrape_pathe_toulouse_wilson
from app.scrapers.allocine_scraper import get_note_allocine
from app import jellyfin_client

logger = logging.getLogger("storage")

_lock = Lock()
_films: List[Film] = []
_statut = StatutScraping()
_statut_sync_jellyfin = StatutSyncJellyfin()

# Conversion note AlloCiné (0-5) -> échelles Jellyfin
# CommunityRating: 0-10 (comme IMDb)   -> note spectateurs x2
# CriticRating: 0-100 (comme Metacritic/RT) -> note presse x20
def _note_to_community_rating(note_sur_5: float) -> float:
    return note_sur_5 * 2

def _note_to_critic_rating(note_sur_5: float) -> float:
    return note_sur_5 * 20


def _slugify(titre: str) -> str:
    nfkd = unicodedata.normalize("NFKD", titre)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")


def run_scraping_pipeline() -> StatutScraping:
    """
    Lance le pipeline complet :
    1. Récupère les films à l'affiche au Pathé Toulouse Wilson.
    2. Pour chaque film, récupère sa note AlloCiné.
    3. Stocke le résultat en mémoire.

    Ne lève jamais d'exception : les erreurs sont capturées et reportées dans
    le statut, pour ne pas empêcher le démarrage de l'API en cas de souci
    réseau ou de changement de structure d'un des deux sites.
    """
    global _films, _statut
    erreurs: List[str] = []
    films: List[Film] = []

    try:
        films = scrape_pathe_toulouse_wilson()
    except Exception as e:
        logger.exception("Échec du scraping Pathé Toulouse Wilson")
        erreurs.append(f"Pathé: {e}")

    for film in films:
        try:
            film.slug = _slugify(film.titre)
            film.allocine = get_note_allocine(film.titre, type_="film")
        except Exception as e:
            logger.exception("Échec récupération note AlloCiné pour '%s'", film.titre)
            erreurs.append(f"AlloCiné ({film.titre}): {e}")

    with _lock:
        _films = films
        _statut = StatutScraping(
            derniere_maj=datetime.now(timezone.utc).isoformat(),
            nb_films=len(films),
            erreurs=erreurs,
        )

    logger.info("Pipeline terminé: %d films, %d erreur(s)", len(films), len(erreurs))
    return _statut


def get_all_films() -> List[Film]:
    with _lock:
        return list(_films)


def get_film_by_slug(slug: str) -> Optional[Film]:
    with _lock:
        for film in _films:
            if film.slug == slug:
                return film
    return None


def get_statut() -> StatutScraping:
    with _lock:
        return _statut


def run_jellyfin_notes_sync() -> StatutSyncJellyfin:
    """
    Parcourt la bibliothèque Jellyfin (films + séries), récupère la note
    AlloCiné correspondante pour chaque titre, et met à jour CommunityRating
    (note spectateurs) et CriticRating (note presse) dans Jellyfin.

    Ne lève jamais d'exception : les erreurs sont capturées et reportées.
    """
    global _statut_sync_jellyfin
    erreurs: List[str] = []
    nb_appliquees = 0
    nb_non_trouves = 0

    try:
        items = jellyfin_client.get_library_items()
    except Exception as e:
        logger.exception("Échec de la récupération de la bibliothèque Jellyfin")
        statut = StatutSyncJellyfin(
            derniere_sync=datetime.now(timezone.utc).isoformat(),
            erreurs=[f"Jellyfin (lecture bibliothèque): {e}"],
        )
        with _lock:
            _statut_sync_jellyfin = statut
        return statut

    for item in items:
        titre = item.get("Name")
        item_id = item.get("Id")
        item_type = item.get("Type")  # "Movie" ou "Series"
        if not titre or not item_id:
            continue

        type_allocine = "film" if item_type == "Movie" else "serie"

        try:
            note = get_note_allocine(titre, type_=type_allocine)
        except Exception as e:
            logger.exception("Erreur récupération note AlloCiné pour '%s'", titre)
            erreurs.append(f"AlloCiné ({titre}): {e}")
            continue

        if not note.trouve:
            nb_non_trouves += 1
            continue

        community_rating = (
            _note_to_community_rating(note.note_spectateurs)
            if note.note_spectateurs is not None else None
        )
        critic_rating = (
            _note_to_critic_rating(note.note_presse)
            if note.note_presse is not None else None
        )

        if community_rating is None and critic_rating is None:
            nb_non_trouves += 1
            continue

        try:
            ok = jellyfin_client.update_item_ratings(
                item_id, community_rating=community_rating, critic_rating=critic_rating
            )
            if ok:
                nb_appliquees += 1
            else:
                erreurs.append(f"Jellyfin (mise à jour '{titre}'): échec API")
        except Exception as e:
            logger.exception("Erreur mise à jour Jellyfin pour '%s'", titre)
            erreurs.append(f"Jellyfin (mise à jour '{titre}'): {e}")

    statut = StatutSyncJellyfin(
        derniere_sync=datetime.now(timezone.utc).isoformat(),
        nb_items_bibliotheque=len(items),
        nb_notes_appliquees=nb_appliquees,
        nb_non_trouves=nb_non_trouves,
        erreurs=erreurs,
    )
    with _lock:
        _statut_sync_jellyfin = statut

    logger.info(
        "Sync Jellyfin terminée: %d items, %d notes appliquées, %d non trouvés, %d erreur(s)",
        len(items), nb_appliquees, nb_non_trouves, len(erreurs),
    )
    return statut


def get_statut_sync_jellyfin() -> StatutSyncJellyfin:
    with _lock:
        return _statut_sync_jellyfin


def get_jellyfin_library_titles() -> List[str]:
    """Retourne les titres de la bibliothèque Jellyfin (pour le matching calendrier)."""
    try:
        items = jellyfin_client.get_library_items()
        return [item.get("Name") for item in items if item.get("Name")]
    except Exception as e:
        logger.warning("Impossible de récupérer la bibliothèque Jellyfin pour le calendrier: %s", e)
        return []
