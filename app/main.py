"""
API REST — Films à l'affiche au Pathé Toulouse Wilson, enrichis des notes
AlloCiné (presse / spectateurs).

Le scraping est effectué une fois au démarrage du conteneur. Un endpoint
POST /refresh permet de le relancer manuellement sans redémarrer le service.
"""
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from app.models import Film, NoteAlloCine, StatutScraping, StatutSyncJellyfin
from app.scrapers.allocine_scraper import get_note_allocine
from app import allocine_cache
from app.storage import (
    run_scraping_pipeline,
    get_all_films,
    get_film_by_slug,
    get_statut,
    start_jellyfin_notes_sync_async,
    get_statut_sync_jellyfin,
)
from app.calendar_builder import generate_calendar_ics
from app.config import (
    ALLOCINE_REQUESTS_PER_SECOND,
    ALLOCINE_SYNC_DELAY_SECONDS,
    jellyfin_configured,
)
from fastapi import Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

app = FastAPI(
    title="🎬 Movies Tracker",
    description=(
        "Films à l'affiche au Pathé Toulouse Wilson, séances et notes AlloCiné, "
        "synchronisés avec Jellyfin."
    ),
    version="1.0.0",
)

# Fichiers statiques éventuels (favicon, images...). Le dossier existe toujours
# (même vide, via .gitkeep) pour éviter une erreur au montage.
app.mount("/static", StaticFiles(directory="app/static"), name="static")

_DASHBOARD_PATH = Path(__file__).parent / "static" / "dashboard.html"


@app.get("/", response_class=HTMLResponse, tags=["Interface"])
def dashboard() -> HTMLResponse:
    """
    Page web listant les films à l'affiche, leurs séances, leurs notes
    AlloCiné, et les statuts de scraping / synchro Jellyfin. Lit le fichier
    à chaque requête (pas de cache), donc éditable sans reconstruire l'image.
    """
    if not _DASHBOARD_PATH.exists():
        raise HTTPException(status_code=500, detail="dashboard.html introuvable dans app/static/")
    return HTMLResponse(content=_DASHBOARD_PATH.read_text(encoding="utf-8"))


@app.on_event("startup")
def on_startup() -> None:
    limite = (
        f"{ALLOCINE_REQUESTS_PER_SECOND:g} requête(s)/seconde max"
        if ALLOCINE_REQUESTS_PER_SECOND > 0 else "aucune limite de débit"
    )
    logger.info(
        "Réglages AlloCiné : %s, pause de %gs entre chaque titre lors de la synchro Jellyfin.",
        limite, ALLOCINE_SYNC_DELAY_SECONDS,
    )
    # 1) Récupération de la page salle AlloCiné : rapide (~1 s), faite ici pour
    #    que les films soient disponibles dès que l'API répond.
    logger.info("Démarrage: lancement du scraping initial...")
    statut = run_scraping_pipeline()
    if statut.erreurs:
        logger.warning("Scraping initial terminé avec des erreurs: %s", statut.erreurs)
    else:
        logger.info("Scraping initial terminé: %d films récupérés", statut.nb_films)

    # 2) Synchro des notes vers Jellyfin : LONGUE (une requête AlloCiné par
    #    titre de la bibliothèque, avec pause entre chaque). Lancée en arrière-
    #    plan pour ne PAS bloquer le démarrage : sinon l'API et la page web
    #    resteraient injoignables pendant toute la durée de la synchro.
    if jellyfin_configured():
        logger.info(
            "Jellyfin configuré: synchronisation des notes AlloCiné lancée en arrière-plan "
            "(progression : GET /jellyfin/statut)."
        )
        start_jellyfin_notes_sync_async()
    else:
        logger.info(
            "JELLYFIN_URL/JELLYFIN_API_KEY non configurés : synchronisation des notes "
            "et calendrier filtré désactivés (déclenchables manuellement une fois configurés)."
        )


@app.get("/health", tags=["Système"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/statut", response_model=StatutScraping, tags=["Système"])
def statut() -> StatutScraping:
    """Informations sur le dernier scraping effectué (date, nb de films, erreurs)."""
    return get_statut()


@app.post("/refresh", response_model=StatutScraping, tags=["Système"])
def refresh() -> StatutScraping:
    """
    Relance manuellement le scraping AlloCiné (films, séances, notes).
    Utile si vous voulez rafraîchir les données sans redémarrer le conteneur,
    même si la configuration par défaut ne scrape qu'au démarrage.
    """
    logger.info("Rafraîchissement manuel demandé via /refresh")
    return run_scraping_pipeline()


@app.get("/films", response_model=List[Film], tags=["Films"])
def list_films() -> List[Film]:
    """Liste des films actuellement à l'affiche au Pathé Toulouse Wilson."""
    return get_all_films()


@app.get("/films/{slug}", response_model=Film, tags=["Films"])
def get_film(slug: str) -> Film:
    """Détail d'un film (séances + note AlloCiné) à partir de son slug."""
    film = get_film_by_slug(slug)
    if film is None:
        raise HTTPException(status_code=404, detail=f"Film introuvable pour le slug '{slug}'")
    return film


@app.get("/allocine/note", response_model=NoteAlloCine, tags=["AlloCiné"])
def allocine_note(
    titre: str = Query(..., description="Titre du film ou de la série"),
    type: str = Query("film", pattern="^(film|serie)$", description="'film' ou 'serie'"),
    annee: Optional[int] = Query(None, description="Année de production (départage les homonymes)"),
    force_refresh: bool = Query(False, description="Ignore le cache AlloCiné et refait la recherche"),
) -> NoteAlloCine:
    """
    Endpoint générique pour récupérer la note AlloCiné (presse/spectateurs)
    de n'importe quel film ou série.
    Exemple: /allocine/note?titre=Dune%20Deuxième%20Partie&type=film

    Le résultat est mis en cache (voir ALLOCINE_CACHE_TTL_DAYS) : un appel
    répété pour le même titre ne refait pas la recherche tant que le cache
    est valide, sauf avec force_refresh=true.
    """
    return get_note_allocine(titre, type_=type, annee=annee, force_refresh=force_refresh)


@app.post("/jellyfin/sync-notes", response_model=StatutSyncJellyfin, tags=["Jellyfin"])
def jellyfin_sync_notes(
    force_refresh: bool = Query(
        False, description="Ignore le cache AlloCiné et refait la recherche pour TOUS les titres"
    ),
) -> StatutSyncJellyfin:
    """
    Parcourt la bibliothèque Jellyfin (films + séries), récupère la note
    AlloCiné de chaque titre, et met à jour CommunityRating (note spectateurs)
    et CriticRating (note presse) sur les items Jellyfin correspondants.

    Nécessite JELLYFIN_URL et JELLYFIN_API_KEY configurés (variables d'env).
    Lancé automatiquement au démarrage du conteneur si ces variables sont
    définies ; cet endpoint permet de relancer la synchro manuellement.

    Les titres déjà résolus lors d'une synchro précédente sont servis depuis
    le cache AlloCiné (voir ALLOCINE_CACHE_TTL_DAYS) plutôt que recherchés à
    nouveau : passez force_refresh=true pour forcer une recherche complète
    (voir aussi DELETE /allocine/cache pour vider le cache sans relancer de
    synchro).

    La synchro dure plusieurs minutes : elle tourne en arrière-plan et cet
    endpoint répond immédiatement. Suivre l'avancement via GET /jellyfin/statut
    (champs en_cours / nb_traites / nb_items_bibliotheque). Si une synchro est
    déjà en cours, aucune seconde synchro n'est lancée (y compris avec
    force_refresh différent : la demande en cours va jusqu'au bout).
    """
    if not jellyfin_configured():
        raise HTTPException(
            status_code=400,
            detail="JELLYFIN_URL et JELLYFIN_API_KEY doivent être configurés (variables d'environnement).",
        )
    return start_jellyfin_notes_sync_async(force_refresh=force_refresh)


@app.delete("/allocine/cache", tags=["AlloCiné"])
def allocine_cache_clear() -> dict:
    """
    Vide le cache des résultats AlloCiné (recherche + note par titre). Le
    prochain appel à /allocine/note ou à la synchro Jellyfin recherchera donc
    chaque titre à nouveau. Alternative à force_refresh quand on veut vider
    le cache sans forcément relancer une synchro dans la foulée.
    """
    return {"entrees_supprimees": allocine_cache.clear_cache()}


@app.get("/jellyfin/statut", response_model=StatutSyncJellyfin, tags=["Jellyfin"])
def jellyfin_statut() -> StatutSyncJellyfin:
    """Statut de la dernière synchronisation des notes vers Jellyfin."""
    return get_statut_sync_jellyfin()


@app.get("/calendar.ics", tags=["Calendrier"])
def calendar_ics() -> Response:
    """
    Flux iCal (.ics) des films actuellement à l'affiche au Pathé Toulouse
    Wilson dont le titre correspond à un film/série déjà présent dans votre
    bibliothèque Jellyfin.

    À utiliser directement comme URL de calendrier dans Homepage
    (widget "calendar", type "ical") ou Homarr (widget "Calendar",
    intégration iCal). Exemple d'URL à renseigner :
    http://<adresse-de-ce-conteneur>:8095/calendar.ics
    """
    ics_bytes = generate_calendar_ics()
    return Response(
        content=ics_bytes,
        media_type="text/calendar",
        headers={"Content-Disposition": "inline; filename=pathe-toulouse-wilson-jellyfin.ics"},
    )
