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

from app.config import ALLOCINE_REQUESTS_PER_SECOND, ALLOCINE_SYNC_DELAY_SECONDS
from app.matching import extract_year_from_title
from app.models import Film, ItemBibliotheque, StatutScraping, StatutSyncJellyfin
from app.scrapers.allocine_theater_scraper import scrape_allocine_theater
from app.scrapers.allocine_scraper import get_note_allocine, get_cached_note_allocine
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


def _statut_demarrage(precedent: StatutSyncJellyfin) -> StatutSyncJellyfin:
    """
    Statut publié DÈS la prise du verrou, avant même la lecture de la
    bibliothèque Jellyfin (qui prend plusieurs requêtes). Sans lui,
    GET /jellyfin/statut répondait "pas en cours" pendant ce délai : le
    dashboard cessait de suivre la progression et réactivait ses boutons alors
    que l'opération tournait. La date de dernière synchro est conservée.
    """
    return StatutSyncJellyfin(en_cours=True, derniere_sync=precedent.derniere_sync)


def run_jellyfin_notes_sync(force_refresh: bool = False) -> StatutSyncJellyfin:
    """
    Parcourt la bibliothèque Jellyfin (films + séries), récupère la note
    AlloCiné correspondante pour chaque titre, et met à jour CommunityRating
    (note spectateurs) et CriticRating (note presse) dans Jellyfin.

    Bloquant (peut durer plusieurs minutes) : pour un usage depuis l'API ou au
    démarrage, préférer start_jellyfin_notes_sync_async(). Une seule synchro
    à la fois : si une est déjà en cours, retourne simplement son statut.
    La progression est publiée en direct (en_cours / nb_traites).

    Ne lève jamais d'exception : les erreurs sont capturées et reportées.

    :param force_refresh: ignore le cache AlloCiné pour tous les titres et les
        recherche tous à nouveau (voir app/allocine_cache.py). Utile après un
        changement dans la logique de correspondance, ou si vous pensez que
        des notes sont restées figées par erreur.
    """
    global _statut_sync_jellyfin, _sync_running

    with _lock:
        if _sync_running:
            logger.info("Synchro Jellyfin déjà en cours : nouvelle demande ignorée")
            return _statut_sync_jellyfin
        _sync_running = True
        _statut_sync_jellyfin = _statut_demarrage(_statut_sync_jellyfin)

    try:
        return _run_jellyfin_notes_sync_locked(force_refresh=force_refresh)
    finally:
        with _lock:
            _sync_running = False


def apply_cached_notes_to_jellyfin() -> StatutSyncJellyfin:
    """
    Écrit vers Jellyfin les notes AlloCiné déjà présentes dans le cache,
    SANS effectuer aucune requête vers AlloCiné : les items dont le titre
    n'a pas d'entrée de cache valide sont simplement ignorés (ni recherchés,
    ni modifiés). Beaucoup plus rapide qu'une synchro complète — utile pour
    appliquer immédiatement des notes déjà connues, par exemple après une
    correction côté Jellyfin (ex. un bug de mise à jour corrigé entre-temps)
    sans attendre une nouvelle synchro complète.

    Bloquant : pour un usage depuis l'API, préférer
    start_apply_cached_notes_to_jellyfin_async(). Partage le même verrou
    qu'une synchro complète (run_jellyfin_notes_sync) : les deux opérations
    ne peuvent jamais tourner en même temps.

    Ne lève jamais d'exception : les erreurs sont capturées et reportées.
    """
    global _statut_sync_jellyfin, _sync_running

    with _lock:
        if _sync_running:
            logger.info("Une opération Jellyfin est déjà en cours : nouvelle demande ignorée")
            return _statut_sync_jellyfin
        _sync_running = True
        _statut_sync_jellyfin = _statut_demarrage(_statut_sync_jellyfin)

    try:
        return _run_jellyfin_notes_sync_locked(cache_only=True)
    finally:
        with _lock:
            _sync_running = False


def _run_jellyfin_notes_sync_locked(
    force_refresh: bool = False, cache_only: bool = False
) -> StatutSyncJellyfin:
    """
    :param force_refresh: ignore le cache AlloCiné, refait toutes les recherches
        (incompatible avec cache_only, voir apply_cached_notes_to_jellyfin).
    :param cache_only: n'effectue AUCUNE requête vers AlloCiné. Pour chaque item
        Jellyfin, écrit la note déjà présente dans le cache AlloCiné si elle
        existe, sinon ignore l'item sans le rechercher. Beaucoup plus rapide
        qu'une synchro complète (pas de limite de débit ni de pause à
        respecter, puisqu'aucune requête sortante n'est faite).
    """
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

    if cache_only:
        logger.info(
            "Application du cache AlloCiné vers Jellyfin démarrée : %d item(s) à traiter "
            "(aucune requête AlloCiné, uniquement le cache existant).",
            len(items),
        )
    else:
        # Un titre = 2 requêtes (recherche + fiche), espacées par la limite de débit,
        # puis la pause entre titres. Estimation basse (hors temps de réponse réseau).
        par_titre = ALLOCINE_SYNC_DELAY_SECONDS + (2.0 / ALLOCINE_REQUESTS_PER_SECOND if ALLOCINE_REQUESTS_PER_SECOND > 0 else 0.0)
        logger.info(
            "Synchro Jellyfin démarrée : %d titre(s) à traiter (durée minimale estimée : ~%.0f min)%s",
            len(items), len(items) * par_titre / 60,
            " [force_refresh: le cache AlloCiné est ignoré]" if force_refresh else "",
        )

    for i, item in enumerate(items):
        titre = item.get("Name")
        item_id = item.get("Id")
        item_type = item.get("Type")  # "Movie" ou "Series"
        if not titre or not item_id:
            nb_traites += 1
            continue

        type_allocine = "film" if item_type == "Movie" else "serie"
        # Année de production Jellyfin : sert à départager les homonymes côté AlloCiné
        annee = item.get("ProductionYear")
        annee = annee if isinstance(annee, int) else None

        if cache_only:
            # Lecture pure du cache : aucune requête réseau, donc aucune pause
            # à respecter ici (contrairement à la branche ci-dessous).
            note = get_cached_note_allocine(titre, type_allocine, annee)
            if note is None:
                nb_non_trouves += 1
                nb_traites += 1
                publish(len(items), True)
                continue
        else:
            # Pause entre chaque titre : la synchro peut interroger AlloCiné pour
            # des dizaines/centaines de titres à la suite, ce qui peut déclencher
            # une limitation de débit (429 Too Many Requests) sans cette pause.
            if i > 0 and ALLOCINE_SYNC_DELAY_SECONDS > 0:
                time.sleep(ALLOCINE_SYNC_DELAY_SECONDS)
            try:
                note = get_note_allocine(titre, type_=type_allocine, annee=annee, force_refresh=force_refresh)
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
                    logger.info(
                        "Jellyfin mis à jour : %r -> CommunityRating=%s, CriticRating=%s",
                        titre, community_rating, critic_rating,
                    )
                else:
                    erreurs.append(f"Jellyfin (mise à jour '{titre}'): échec API")
            except Exception as e:
                logger.exception("Erreur mise à jour Jellyfin pour '%s'", titre)
                erreurs.append(f"Jellyfin (mise à jour '{titre}'): {e}")

        nb_traites += 1
        publish(len(items), True)

    statut = publish(len(items), False, datetime.now(timezone.utc).isoformat())
    logger.info(
        "%s terminée: %d items, %d notes appliquées, %d %s, %d erreur(s)",
        "Application du cache" if cache_only else "Sync Jellyfin",
        len(items), nb_appliquees, nb_non_trouves,
        "sans entrée de cache" if cache_only else "non trouvés", len(erreurs),
    )
    return statut


def start_jellyfin_notes_sync_async(force_refresh: bool = False) -> StatutSyncJellyfin:
    """
    Lance la synchro Jellyfin dans un thread d'arrière-plan et rend la main
    immédiatement (l'API/la page web restent utilisables pendant que la synchro
    tourne). Si une synchro est déjà en cours, ne fait rien. Retourne le statut
    courant ; suivre l'avancement via GET /jellyfin/statut.
    """
    with _lock:
        if _sync_running:
            return _statut_sync_jellyfin
    Thread(target=run_jellyfin_notes_sync, args=(force_refresh,), name="jellyfin-sync", daemon=True).start()
    with _lock:
        # Marque tout de suite "en cours" pour que le premier affichage soit juste
        # (le thread peut mettre quelques ms à démarrer).
        return _statut_sync_jellyfin.model_copy(update={"en_cours": True})


def start_apply_cached_notes_to_jellyfin_async() -> StatutSyncJellyfin:
    """
    Lance apply_cached_notes_to_jellyfin() dans un thread d'arrière-plan et
    rend la main immédiatement. Comme il n'y a aucune requête AlloCiné, cette
    opération est rapide (quelques secondes à quelques minutes selon la
    taille de la bibliothèque), mais reste asynchrone par cohérence avec la
    synchro complète et pour ne jamais bloquer l'API/la page web.
    """
    with _lock:
        if _sync_running:
            return _statut_sync_jellyfin
    Thread(target=apply_cached_notes_to_jellyfin, name="jellyfin-cache-push", daemon=True).start()
    with _lock:
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


def _tronquer(texte: Optional[str], limite: int = 400) -> Optional[str]:
    """Raccourcit un synopsis à `limite` caractères, à une frontière de mot.
    La carte n'en montre que 3 lignes : inutile de transférer plusieurs Ko par
    item pour plusieurs centaines d'items."""
    if not texte:
        return None
    texte = " ".join(texte.split())
    if len(texte) <= limite:
        return texte
    return texte[:limite].rsplit(" ", 1)[0].rstrip(" ,;:.-—") + "…"


def get_jellyfin_library_with_notes() -> List[ItemBibliotheque]:
    """
    Retourne la bibliothèque Jellyfin (films + séries) enrichie de la note
    AlloCiné déjà en cache pour chaque titre, pour la page /bibliotheque.

    N'effectue AUCUNE requête vers AlloCiné (lecture pure du cache, comme
    apply_cached_notes_to_jellyfin) : un titre sans entrée de cache valide
    apparaît simplement sans note (`allocine: null`), il n'est jamais
    recherché depuis cet endpoint. Fait une requête vers Jellyfin (paginée,
    voir jellyfin_client.get_library_items) à chaque appel : pas de mise en
    cache de la liste elle-même, pour refléter la bibliothèque actuelle.

    Le titre affiché est le titre Jellyfin SANS l'année éventuellement
    placée entre parenthèses ("Macross (1982)" -> "Macross", année 1982) :
    l'année a sa propre colonne, l'afficher deux fois serait redondant.
    """
    items = jellyfin_client.get_library_items()
    resultat: List[ItemBibliotheque] = []
    for item in items:
        item_id = item.get("Id")
        titre_brut = item.get("Name")
        item_type = item.get("Type")
        if not item_id or not titre_brut:
            continue

        titre, annee_du_titre = extract_year_from_title(titre_brut)
        annee_jellyfin = item.get("ProductionYear")
        annee_jellyfin = annee_jellyfin if isinstance(annee_jellyfin, int) else None
        annee = annee_jellyfin if annee_jellyfin is not None else annee_du_titre

        type_allocine = "film" if item_type == "Movie" else "serie"
        # Même résolution de clé que l'écriture du cache (titre brut + année Jellyfin).
        note = get_cached_note_allocine(titre_brut, type_allocine, annee_jellyfin)

        # ImageTags absent de la réponse = inconnu (pas "pas d'affiche") : on
        # laisse le navigateur essayer, la page affiche un repli en cas d'échec.
        image_tags = item.get("ImageTags")
        a_une_affiche = True if image_tags is None else bool(image_tags.get("Primary"))

        resultat.append(ItemBibliotheque(
            id=item_id,
            titre=titre,
            type=type_allocine,
            annee=annee,
            genres=item.get("Genres") or [],
            synopsis=_tronquer(item.get("Overview")),
            a_une_affiche=a_une_affiche,
            allocine=note,
        ))
    return resultat
