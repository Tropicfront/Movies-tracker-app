"""
API REST — Films à l'affiche au Pathé Toulouse Wilson, enrichis des notes
AlloCiné (presse / spectateurs).

Le scraping est effectué une fois au démarrage du conteneur. Un endpoint
POST /refresh permet de le relancer manuellement sans redémarrer le service.
"""
import logging
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from app.models import Film, NoteAlloCine, StatutScraping, StatutSyncJellyfin
from app.scrapers.allocine_scraper import get_note_allocine
from app.storage import (
    run_scraping_pipeline,
    get_all_films,
    get_film_by_slug,
    get_statut,
    run_jellyfin_notes_sync,
    get_statut_sync_jellyfin,
)
from app.calendar_builder import generate_calendar_ics
from app.config import jellyfin_configured
from fastapi import Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

app = FastAPI(
    title="Pathé Toulouse Wilson + AlloCiné API",
    description=(
        "Récupère les films à l'affiche au Pathé Toulouse Wilson et leurs "
        "notes AlloCiné (presse / spectateurs)."
    ),
    version="1.0.0",
)

# Fichiers statiques éventuels (favicon, images...). Le dossier existe toujours
# (même vide, via .gitkeep) pour éviter une erreur au montage.
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def on_startup() -> None:
    logger.info("Démarrage: lancement du scraping initial...")
    statut = run_scraping_pipeline()
    if statut.erreurs:
        logger.warning("Scraping initial terminé avec des erreurs: %s", statut.erreurs)
    else:
        logger.info("Scraping initial terminé: %d films récupérés", statut.nb_films)

    if jellyfin_configured():
        logger.info("Jellyfin configuré: lancement de la synchronisation des notes AlloCiné...")
        sync_statut = run_jellyfin_notes_sync()
        if sync_statut.erreurs:
            logger.warning("Sync Jellyfin terminée avec des erreurs: %s", sync_statut.erreurs)
        else:
            logger.info(
                "Sync Jellyfin terminée: %d/%d notes appliquées",
                sync_statut.nb_notes_appliquees, sync_statut.nb_items_bibliotheque,
            )
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
    Relance manuellement le scraping (Pathé + AlloCiné).
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
) -> NoteAlloCine:
    """
    Endpoint générique pour récupérer la note AlloCiné (presse/spectateurs)
    de n'importe quel film ou série, sans passer par le Pathé Toulouse Wilson.
    Exemple: /allocine/note?titre=Dune%20Deuxième%20Partie&type=film
    """
    return get_note_allocine(titre, type_=type)


@app.post("/jellyfin/sync-notes", response_model=StatutSyncJellyfin, tags=["Jellyfin"])
def jellyfin_sync_notes() -> StatutSyncJellyfin:
    """
    Parcourt la bibliothèque Jellyfin (films + séries), récupère la note
    AlloCiné de chaque titre, et met à jour CommunityRating (note spectateurs)
    et CriticRating (note presse) sur les items Jellyfin correspondants.

    Nécessite JELLYFIN_URL et JELLYFIN_API_KEY configurés (variables d'env).
    Lancé automatiquement au démarrage du conteneur si ces variables sont
    définies ; cet endpoint permet de relancer la synchro manuellement.
    """
    if not jellyfin_configured():
        raise HTTPException(
            status_code=400,
            detail="JELLYFIN_URL et JELLYFIN_API_KEY doivent être configurés (variables d'environnement).",
        )
    return run_jellyfin_notes_sync()


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
    http://<adresse-de-ce-conteneur>:8000/calendar.ics
    """
    ics_bytes = generate_calendar_ics()
    return Response(
        content=ics_bytes,
        media_type="text/calendar",
        headers={"Content-Disposition": "inline; filename=pathe-toulouse-wilson-jellyfin.ics"},
    )
