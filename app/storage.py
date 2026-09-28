"""
Stockage en mémoire des films scrapés + orchestration du pipeline
(page salle AlloCiné du Pathé Toulouse Wilson -> films + séances + notes,
le tout en une seule source).
"""
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from threading import Lock, Thread
from typing import List, Optional

from app.config import ALLOCINE_SYNC_DELAY_SECONDS
from app.models import Film, StatutScraping, StatutSyncJellyfin
from app.scrapers.allocine_theater_scraper import scrape_allocine_theater
from app.scrapers.allocine_scraper import get_note_allocine
from app import jellyfin_client

logger = logging.getLogger("storage")

_lock = Lock()
_films: List[Film] = []
_statut = StatutScraping()
_statut_sync_jellyfin = StatutSyncJellyfin()
_sync_running = False  # protégé par _lock : évite deux synchros simultanées

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
    Récupère les films à l'affiche, leurs séances et leurs notes AlloCiné
    pour le Pathé Toulouse Wilson, en une seule requête vers la page salle
    AlloCiné dédiée (voir app/scrapers/allocine_theater_scraper.py).

    Contrairement à l'ancienne version (pathe.fr + recherche AlloCiné par
    titre pour chaque film), les notes sont déjà présentes sur cette page,
    directement associées au bon film : pas de risque de mauvaise
    correspondance de titre.

    Ne lève jamais d'exception : les erreurs sont capturées et reportées dans
    le statut, pour ne pas empêcher le démarrage de l'API en cas de souci
    réseau ou de changement de structure du site.
    """
    global _films, _statut
    erreurs: List[str] = []
    films: List[Film] = []

    try:
        films = scrape_allocine_theater()
        for film in films:
            film.slug = _slugify(film.titre)
    except Exception as e:
        logger.exception("Échec du scraping AlloCiné (page salle)")
        erreurs.append(f"AlloCiné: {e}")

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

    Bloquant (peut durer plusieurs minutes) : pour un usage depuis l'API ou au
    démarrage, préférer start_jellyfin_notes_sync_async(). Une seule synchro
    à la fois : si une est déjà en cours, retourne simplement son statut.
    La progression est publiée en direct (en_cours / nb_traites).

    Ne lève jamais d'exception : les erreurs sont capturées et reportées.
    """
    global _statut_sync_jellyfin, _sync_running

    with _lock:
        if _sync_running:
            logger.info("Synchro Jellyfin déjà en cours : nouvelle demande ignorée")
            return _statut_sync_jellyfin
        _sync_running = True

    try:
        return _run_jellyfin_notes_sync_locked()
    finally:
        with _lock:
            _sync_running = False


def _run_jellyfin_notes_sync_locked() -> StatutSyncJellyfin:
    global _statut_sync_jellyfin
    erreurs: List[str] = []
    nb_appliquees = 0
    nb_non_trouves = 0
    nb_traites = 0

    def publish(items_count: int, en_cours: bool, derniere_sync: Optional[str] = None) -> StatutSyncJellyfin:
        global _statut_sync_jellyfin
        statut = StatutSyncJellyfin(
            en_cours=en_cours,
            nb_traites=nb_traites,
            derniere_sync=derniere_sync,
            nb_items_bibliotheque=items_count,
            nb_notes_appliquees=nb_appliquees,
            nb_non_trouves=nb_non_trouves,
            erreurs=list(erreurs),
        )
        with _lock:
            _statut_sync_jellyfin = statut
        return statut

    try:
        items = jellyfin_client.get_library_items()
    except Exception as e:
        logger.exception("Échec de la récupération de la bibliothèque Jellyfin")
        erreurs.append(f"Jellyfin (lecture bibliothèque): {e}")
        return publish(0, False, datetime.now(timezone.utc).isoformat())

    publish(len(items), True)  # la progression devient visible tout de suite
    logger.info("Synchro Jellyfin démarrée : %d titre(s) à traiter", len(items))

    for i, item in enumerate(items):
        titre = item.get("Name")
        item_id = item.get("Id")
        item_type = item.get("Type")  # "Movie" ou "Series"
        if not titre or not item_id:
            nb_traites += 1
            continue

        # Pause entre chaque titre : la synchro peut interroger AlloCiné pour
        # des dizaines/centaines de titres à la suite, ce qui peut déclencher
        # une limitation de débit (429 Too Many Requests) sans cette pause.
        if i > 0 and ALLOCINE_SYNC_DELAY_SECONDS > 0:
            time.sleep(ALLOCINE_SYNC_DELAY_SECONDS)

        type_allocine = "film" if item_type == "Movie" else "serie"

        try:
            note = get_note_allocine(titre, type_=type_allocine)
        except Exception as e:
            logger.exception("Erreur récupération note AlloCiné pour '%s'", titre)
            erreurs.append(f"AlloCiné ({titre}): {e}")
            nb_traites += 1
            publish(len(items), True)
            continue

        community_rating = (
            _note_to_community_rating(note.note_spectateurs)
            if note.trouve and note.note_spectateurs is not None else None
        )
        critic_rating = (
            _note_to_critic_rating(note.note_presse)
            if note.trouve and note.note_presse is not None else None
        )

        if community_rating is None and critic_rating is None:
            nb_non_trouves += 1
        else:
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

        nb_traites += 1
        publish(len(items), True)

    statut = publish(len(items), False, datetime.now(timezone.utc).isoformat())
    logger.info(
        "Sync Jellyfin terminée: %d items, %d notes appliquées, %d non trouvés, %d erreur(s)",
        len(items), nb_appliquees, nb_non_trouves, len(erreurs),
    )
    return statut


def start_jellyfin_notes_sync_async() -> StatutSyncJellyfin:
    """
    Lance la synchro Jellyfin dans un thread d'arrière-plan et rend la main
    immédiatement (l'API/la page web restent utilisables pendant que la synchro
    tourne). Si une synchro est déjà en cours, ne fait rien. Retourne le statut
    courant ; suivre l'avancement via GET /jellyfin/statut.
    """
    with _lock:
        if _sync_running:
            return _statut_sync_jellyfin
    Thread(target=run_jellyfin_notes_sync, name="jellyfin-sync", daemon=True).start()
    with _lock:
        # Marque tout de suite "en cours" pour que le premier affichage soit juste
        # (le thread peut mettre quelques ms à démarrer).
        return _statut_sync_jellyfin.model_copy(update={"en_cours": True})


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
