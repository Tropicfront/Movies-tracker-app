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

from app.config import ALLOCINE_REQUESTS_PER_SECOND, ALLOCINE_SYNC_DELAY_SECONDS, JELLYFIN_WRITE_RATINGS
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


MSG_ECRITURE_DESACTIVEE = (
    "Écriture vers Jellyfin désactivée (JELLYFIN_WRITE_RATINGS=false) : rien n'a été fait. "
    "Les notes AlloCiné sont gardées dans le cache et affichées par le badge ; l'étoile et la tomate "
    "de Jellyfin ne sont pas touchées."
)


def ecriture_jellyfin_activee() -> bool:
    """JELLYFIN_WRITE_RATINGS : écrire les notes AlloCiné dans l'étoile / la tomate de Jellyfin ?"""
    return bool(JELLYFIN_WRITE_RATINGS)


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

    Sans objet (et refusée) quand JELLYFIN_WRITE_RATINGS est désactivé : rien ne doit
    alors être écrit dans l'étoile ni la tomate de Jellyfin.
    """
    global _statut_sync_jellyfin, _sync_running

    if not ecriture_jellyfin_activee():
        logger.warning(
            "Application du cache vers Jellyfin refusée : l'écriture dans l'étoile/la tomate est désactivée "
            "(JELLYFIN_WRITE_RATINGS=false). Les notes AlloCiné sont affichées par le badge."
        )
        return get_statut_sync_jellyfin().model_copy(update={"erreurs": [MSG_ECRITURE_DESACTIVEE]})

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
    nb_erreurs_total = 0
    nb_appliquees = 0
    nb_non_persistees = 0
    nb_non_trouves = 0
    nb_trouvees = 0
    nb_traites = 0
    ecriture = ecriture_jellyfin_activee()  # figé pour toute l'opération
    MAX_ERREURS = 100  # le statut est renvoyé tel quel par l'API : on borne sa taille

    def add_erreur(msg: str) -> None:
        nonlocal nb_erreurs_total
        nb_erreurs_total += 1
        if len(erreurs) < MAX_ERREURS:
            erreurs.append(msg)

    def publish(items_count: int, en_cours: bool, derniere_sync: Optional[str] = None) -> StatutSyncJellyfin:
        global _statut_sync_jellyfin
        statut = StatutSyncJellyfin(
            en_cours=en_cours,
            nb_traites=nb_traites,
            derniere_sync=derniere_sync,
            nb_items_bibliotheque=items_count,
            nb_notes_appliquees=nb_appliquees,
            nb_non_persistees=nb_non_persistees,
            nb_non_trouves=nb_non_trouves,
            nb_notes_trouvees=nb_trouvees,
            ecriture_jellyfin=ecriture,
            operation="application_cache" if cache_only else "synchro",
            erreurs=list(erreurs) + (
                [f"… et {nb_erreurs_total - MAX_ERREURS} autre(s) erreur(s) non listée(s)"]
                if nb_erreurs_total > MAX_ERREURS else []
            ),
        )
        with _lock:
            _statut_sync_jellyfin = statut
        return statut

    try:
        items = jellyfin_client.get_library_items()
    except Exception as e:
        logger.exception("Échec de la récupération de la bibliothèque Jellyfin")
        add_erreur(f"Jellyfin (lecture bibliothèque): {e}")
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
            "Synchro des notes AlloCiné démarrée : %d titre(s) à traiter (durée minimale estimée : ~%.0f min)%s",
            len(items), len(items) * par_titre / 60,
            " [force_refresh: le cache AlloCiné est ignoré]" if force_refresh else "",
        )
        if not ecriture:
            logger.info(
                "Jellyfin n'est PAS modifié (JELLYFIN_WRITE_RATINGS=false) : les notes sont gardées dans le "
                "cache de l'application et affichées par le badge AlloCiné (étoile et tomate laissées intactes)."
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
                add_erreur(f"AlloCiné ({titre}): {e}")
                nb_traites += 1
                publish(len(items), True)
                continue

        a_une_note = bool(note.trouve and (note.note_spectateurs is not None or note.note_presse is not None))
        if not a_une_note:
            nb_non_trouves += 1
        else:
            nb_trouvees += 1
            if ecriture:
                community_rating = (
                    _note_to_community_rating(note.note_spectateurs) if note.note_spectateurs is not None else None
                )
                critic_rating = _note_to_critic_rating(note.note_presse) if note.note_presse is not None else None
                try:
                    res = jellyfin_client.update_item_ratings(
                        item_id, community_rating=community_rating, critic_rating=critic_rating
                    )
                    if res.persistee is False:
                        # Jellyfin a répondu OK, mais la relecture montre une AUTRE valeur :
                        # la note n'est pas enregistrée. C'est exactement le cas "les logs
                        # disent mis à jour mais rien n'apparaît dans Jellyfin".
                        nb_non_persistees += 1
                        add_erreur(f"Jellyfin (non persistée '{titre}'): {res.detail}")
                        logger.warning("Jellyfin NE CONSERVE PAS la note de %r : %s", titre, res.detail)
                    elif res.acceptee:
                        nb_appliquees += 1
                        logger.info(
                            "Jellyfin mis à jour%s : %r -> CommunityRating=%s, CriticRating=%s",
                            " (relu et confirmé)" if res.persistee else " (non vérifié)",
                            titre, community_rating, critic_rating,
                        )
                    else:
                        add_erreur(f"Jellyfin (mise à jour '{titre}'): {res.detail or 'échec API'}")
                except Exception as e:
                    logger.exception("Erreur mise à jour Jellyfin pour '%s'", titre)
                    add_erreur(f"Jellyfin (mise à jour '{titre}'): {e}")
            else:
                logger.info(
                    "Note AlloCiné gardée en cache : %r -> presse=%s, spectateurs=%s",
                    titre, note.note_presse, note.note_spectateurs,
                )

        nb_traites += 1
        publish(len(items), True)

    statut = publish(len(items), False, datetime.now(timezone.utc).isoformat())
    if ecriture:
        logger.info(
            "%s terminée: %d items, %d notes AlloCiné connues, %d écrites dans Jellyfin, %d NON persistées, %d %s, %d erreur(s)",
            "Application du cache" if cache_only else "Sync Jellyfin",
            len(items), nb_trouvees, nb_appliquees, nb_non_persistees, nb_non_trouves,
            "sans entrée de cache" if cache_only else "non trouvés", nb_erreurs_total,
        )
    else:
        logger.info(
            "Sync des notes AlloCiné terminée: %d items, %d avec une note AlloCiné (en cache, Jellyfin non modifié), "
            "%d sans note, %d erreur(s)",
            len(items), nb_trouvees, nb_non_trouves, nb_erreurs_total,
        )
    if nb_non_persistees:
        logger.warning(
            "%d note(s) acceptée(s) par Jellyfin mais NON conservée(s) : Jellyfin les écrase ou les ignore. "
            "Voir /jellyfin/statut (erreurs) et la colonne « Jellyfin » de la page /bibliotheque.",
            nb_non_persistees,
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
    if not ecriture_jellyfin_activee():
        return apply_cached_notes_to_jellyfin()  # refus explicite, sans lancer de thread
    with _lock:
        if _sync_running:
            return _statut_sync_jellyfin
    Thread(target=apply_cached_notes_to_jellyfin, name="jellyfin-cache-push", daemon=True).start()
    with _lock:
        return _statut_sync_jellyfin.model_copy(update={"en_cours": True})


def get_statut_sync_jellyfin() -> StatutSyncJellyfin:
    with _lock:
        s = _statut_sync_jellyfin
    # Le réglage actuel fait foi (le statut mémorisé date de la dernière opération, ou n'existe
    # pas encore) : le tableau de bord s'en sert pour savoir quels boutons afficher.
    return s.model_copy(update={"ecriture_jellyfin": ecriture_jellyfin_activee()})


def get_jellyfin_library_titles() -> List[str]:
    """Retourne les titres de la bibliothèque Jellyfin (pour le matching calendrier)."""
    try:
        items = jellyfin_client.get_library_items()
        return [item.get("Name") for item in items if item.get("Name")]
    except Exception as e:
        logger.warning("Impossible de récupérer la bibliothèque Jellyfin pour le calendrier: %s", e)
        return []


_NOTES_INDEX_TTL = 300.0   # secondes : la liste Jellyfin n'est relue que toutes les 5 min
_notes_index_cache: tuple = (0.0, None)
_notes_index_lock = Lock()


def get_allocine_notes_index(force: bool = False) -> dict:
    """
    Index {id d'item Jellyfin -> notes AlloCiné} pour le badge affiché dans
    l'interface web de Jellyfin (voir app/static/jellyfin-allocine.js).

    Chaque entrée : {"s": note spectateurs /5, "p": note presse /5, "u": URL de la fiche}
    (clés courtes : l'index peut compter des centaines d'items). Seuls les items
    ayant une note AlloCiné en cache y figurent. Lecture pure du cache : aucune
    requête vers AlloCiné.

    Les identifiants sont normalisés (minuscules, sans tirets), comme dans l'URL
    de la page de détail Jellyfin. L'index est gardé 5 minutes en mémoire :
    le script du navigateur le demande à chaque ouverture de Jellyfin, et le
    reconstruire relit toute la bibliothèque Jellyfin.
    """
    global _notes_index_cache
    import time as _time
    with _notes_index_lock:
        ts, cached = _notes_index_cache
        if not force and cached is not None and _time.time() - ts < _NOTES_INDEX_TTL:
            return cached
        index = {}
        for it in get_jellyfin_library_with_notes():
            n = it.allocine
            if n is None or not n.trouve or (n.note_spectateurs is None and n.note_presse is None):
                continue
            index[str(it.id).replace("-", "").lower()] = {
                "s": n.note_spectateurs, "p": n.note_presse, "u": n.url_fiche,
            }
        _notes_index_cache = (_time.time(), index)
        return index


def _champs_a_nettoyer(item: dict, note) -> dict:
    """
    {"CommunityRating": valeur, "CriticRating": valeur} : les champs de `item` dont la valeur
    ACTUELLE est exactement celle que l'application y aurait écrite d'après la note AlloCiné
    en cache (spectateurs x2 pour l'étoile, presse x20 pour la tomate, une décimale).

    Égalité stricte (voir jellyfin_client._TOLERANCE_NETTOYAGE) : une note communautaire de
    Jellyfin (ex. 8,222) n'est jamais prise pour la nôtre. Vide si l'item n'a pas de note
    AlloCiné connue : sans elle, on ne peut pas savoir ce qui nous appartient.
    """
    out: dict = {}
    if note is None or not note.trouve:
        return out
    tol = jellyfin_client._TOLERANCE_NETTOYAGE
    if note.note_spectateurs is not None:
        v = round(_note_to_community_rating(note.note_spectateurs), 1)
        cur = item.get("CommunityRating")
        if cur is not None and abs(float(cur) - v) <= tol:
            out["CommunityRating"] = v
    if note.note_presse is not None:
        v = round(_note_to_critic_rating(note.note_presse), 1)
        cur = item.get("CriticRating")
        if cur is not None and abs(float(cur) - v) <= tol:
            out["CriticRating"] = v
    return out


def _candidats_nettoyage() -> tuple:
    """(candidats, stats) : les items dont l'étoile et/ou la tomate contiennent nos notes."""
    items = jellyfin_client.get_library_items()
    candidats = []
    stats = {"total": len(items), "etoiles": 0, "tomates": 0, "laissees_intactes": 0, "sans_note_allocine": 0}
    for item in items:
        titre, item_id = item.get("Name"), item.get("Id")
        if not titre or not item_id:
            continue
        annee = item.get("ProductionYear")
        annee = annee if isinstance(annee, int) else None
        note = get_cached_note_allocine(titre, "film" if item.get("Type") == "Movie" else "serie", annee)
        if note is None or not note.trouve:
            stats["sans_note_allocine"] += 1
            continue
        champs = _champs_a_nettoyer(item, note)
        if not champs:
            stats["laissees_intactes"] += 1  # notes de Jellyfin (autre fournisseur) : on n'y touche pas
            continue
        stats["etoiles"] += "CommunityRating" in champs
        stats["tomates"] += "CriticRating" in champs
        candidats.append((item_id, titre, champs))
    return candidats, stats


def apercu_nettoyage_notes_jellyfin() -> dict:
    """
    Aperçu, EN LECTURE SEULE, du nettoyage : combien d'items ont dans leur étoile / leur
    tomate une note identique à celle que cette application y avait écrite. Ne modifie rien.
    """
    candidats, stats = _candidats_nettoyage()
    exemples = []
    for _id, titre, champs in candidats[:10]:
        morceaux = []
        if "CommunityRating" in champs:
            morceaux.append(f"étoile {champs['CommunityRating']}")
        if "CriticRating" in champs:
            morceaux.append(f"tomate {champs['CriticRating']}")
        exemples.append(f"{titre} : " + ", ".join(morceaux))
    return {"ecriture_active": ecriture_jellyfin_activee(), "a_nettoyer": len(candidats), **stats, "exemples": exemples}


def restore_jellyfin_ratings() -> StatutSyncJellyfin:
    """
    Retire de Jellyfin les notes que cette application y avait écrites dans l'étoile et la
    tomate, ainsi que le verrou (LockData) posé en même temps, pour que Jellyfin puisse à
    nouveau les remplir lui-même (« Rechercher les métadonnées manquantes »).

    N'agit que sur les champs dont la valeur est EXACTEMENT celle que l'application avait
    écrite ; revérifiée juste avant chaque retrait. Les valeurs d'origine de Jellyfin, écrasées
    à l'époque, n'avaient pas été sauvegardées : elles ne peuvent PAS être restaurées d'ici, seulement
    redemandées aux fournisseurs de métadonnées de Jellyfin.

    Refusée si JELLYFIN_WRITE_RATINGS est activé (la synchro les réécrirait aussitôt).
    Bloquant ; pour l'API, utiliser start_restore_jellyfin_ratings_async(). Partage le verrou
    des autres opérations Jellyfin. Ne lève jamais d'exception.
    """
    global _statut_sync_jellyfin, _sync_running

    if ecriture_jellyfin_activee():
        return get_statut_sync_jellyfin().model_copy(update={"erreurs": [
            "Nettoyage refusé : JELLYFIN_WRITE_RATINGS est activé, la prochaine synchro réécrirait ces notes. "
            "Désactivez-le d'abord."]})

    with _lock:
        if _sync_running:
            logger.info("Une opération Jellyfin est déjà en cours : nettoyage ignoré")
            return _statut_sync_jellyfin
        _sync_running = True
        _statut_sync_jellyfin = _statut_demarrage(_statut_sync_jellyfin).model_copy(update={"operation": "nettoyage"})

    erreurs: List[str] = []
    nb_nettoyees = nb_ignorees = nb_traites = 0
    total = 0

    def publish(en_cours: bool, derniere: Optional[str] = None) -> StatutSyncJellyfin:
        global _statut_sync_jellyfin
        st_ = StatutSyncJellyfin(
            en_cours=en_cours, nb_traites=nb_traites, derniere_sync=derniere, nb_items_bibliotheque=total,
            nb_nettoyees=nb_nettoyees, nb_ignorees=nb_ignorees, operation="nettoyage",
            ecriture_jellyfin=False, erreurs=list(erreurs[:100]),
        )
        with _lock:
            _statut_sync_jellyfin = st_
        return st_

    try:
        try:
            candidats, _stats = _candidats_nettoyage()
        except Exception as e:
            logger.exception("Nettoyage : lecture de la bibliothèque Jellyfin impossible")
            erreurs.append(f"Jellyfin (lecture bibliothèque): {e}")
            return publish(False, datetime.now(timezone.utc).isoformat())

        total = len(candidats)
        publish(True)
        logger.info("Nettoyage Jellyfin démarré : %d item(s) dont l'étoile/la tomate contiennent nos notes.", total)
        for item_id, titre, champs in candidats:
            try:
                res = jellyfin_client.clear_item_ratings(item_id, champs)
                if res.acceptee and res.persistee is not False:
                    nb_nettoyees += 1
                    logger.info("Nettoyé : %r (%s)", titre, ", ".join(champs))
                elif not res.acceptee and res.detail.startswith("aucune valeur"):
                    nb_ignorees += 1  # changée depuis l'aperçu : plus la nôtre, laissée intacte
                else:
                    erreurs.append(f"Jellyfin (nettoyage '{titre}'): {res.detail or 'échec API'}")
            except Exception as e:
                logger.exception("Erreur de nettoyage pour %r", titre)
                erreurs.append(f"Jellyfin (nettoyage '{titre}'): {e}")
            nb_traites += 1
            publish(True)
        statut = publish(False, datetime.now(timezone.utc).isoformat())
        logger.info("Nettoyage Jellyfin terminé : %d nettoyé(s), %d laissé(s) intact(s) (valeur changée), %d erreur(s).",
                    nb_nettoyees, nb_ignorees, len(erreurs))
        if nb_nettoyees:
            logger.info(
                "Étape suivante dans Jellyfin : actualiser les métadonnées des bibliothèques avec « Rechercher les "
                "métadonnées manquantes » (Search for missing metadata) pour que les notes d'origine reviennent."
            )
        return statut
    finally:
        with _lock:
            _sync_running = False


def start_restore_jellyfin_ratings_async() -> StatutSyncJellyfin:
    """Lance restore_jellyfin_ratings() en arrière-plan ; suivre l'avancement via GET /jellyfin/statut."""
    if ecriture_jellyfin_activee():
        return restore_jellyfin_ratings()  # refus explicite, sans thread
    with _lock:
        if _sync_running:
            return _statut_sync_jellyfin
    Thread(target=restore_jellyfin_ratings, name="jellyfin-nettoyage", daemon=True).start()
    with _lock:
        return _statut_sync_jellyfin.model_copy(update={"en_cours": True, "operation": "nettoyage"})


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


_TOLERANCE_ETAT = 0.06


def _etat_jellyfin(note: Optional[object], community: Optional[float], critic: Optional[float]) -> Optional[str]:
    """
    Compare ce que Jellyfin CONTIENT à ce qu'il devrait contenir d'après la
    note AlloCiné en cache (spectateurs x2 -> /10, presse x20 -> /100).

    "different" : au moins une note est présente dans Jellyfin avec une AUTRE
                  valeur (ex. une note issue d'un autre fournisseur).
    "absente"   : aucune note ne diffère, mais au moins une note attendue
                  MANQUE dans Jellyfin (y compris si l'autre est bien présente).
    "a_jour"    : toutes les notes attendues sont présentes et identiques.
    None        : pas de note AlloCiné connue, donc rien à comparer.
    """
    if note is None or not getattr(note, "trouve", False):
        return None
    attendues = []  # (valeur attendue, valeur réelle dans Jellyfin)
    if note.note_spectateurs is not None:
        attendues.append((_note_to_community_rating(note.note_spectateurs), community))
    if note.note_presse is not None:
        attendues.append((_note_to_critic_rating(note.note_presse), critic))
    if not attendues:
        return None

    if any(reel is not None and abs(float(reel) - voulu) > _TOLERANCE_ETAT for voulu, reel in attendues):
        return "different"
    if any(reel is None for _, reel in attendues):
        return "absente"
    return "a_jour"


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
        poster_tag = (image_tags or {}).get("Primary")
        c_jf, k_jf = item.get("CommunityRating"), item.get("CriticRating")

        resultat.append(ItemBibliotheque(
            id=item_id,
            titre=titre,
            type=type_allocine,
            annee=annee,
            genres=item.get("Genres") or [],
            synopsis=_tronquer(item.get("Overview")),
            a_une_affiche=a_une_affiche,
            poster_tag=poster_tag,
            allocine=note,
            jellyfin_community_rating=c_jf,
            jellyfin_critic_rating=k_jf,
            # Sans écriture vers Jellyfin, comparer son étoile/sa tomate à AlloCiné n'a aucun sens :
            # elles contiennent les notes communautaires / Rotten Tomatoes, pas les nôtres.
            jellyfin_etat=_etat_jellyfin(note, c_jf, k_jf) if ecriture_jellyfin_activee() else None,
        ))
    return resultat
