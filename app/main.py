"""
API REST — Films à l'affiche au Pathé Toulouse Wilson, enrichis des notes
AlloCiné (presse / spectateurs).

Le scraping est effectué une fois au démarrage du conteneur. Un endpoint
POST /refresh permet de le relancer manuellement sans redémarrer le service.
"""
import logging
import re
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from app.models import Film, ItemBibliotheque, NoteAlloCine, StatutScraping, StatutSyncJellyfin
from app.scrapers.allocine_scraper import get_note_allocine
from app import allocine_cache, image_cache, jellyfin_client
from app.storage import (
    run_scraping_pipeline,
    get_all_films,
    get_film_by_slug,
    get_statut,
    start_jellyfin_notes_sync_async,
    start_apply_cached_notes_to_jellyfin_async,
    get_statut_sync_jellyfin,
    get_jellyfin_library_with_notes,
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
_BIBLIOTHEQUE_PATH = Path(__file__).parent / "static" / "bibliotheque.html"


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


@app.get("/bibliotheque", response_class=HTMLResponse, tags=["Interface"])
def bibliotheque() -> HTMLResponse:
    """
    Page web listant les films et séries de la bibliothèque Jellyfin, avec
    leur affiche et leur note AlloCiné (celle déjà en cache — cette page ne
    déclenche jamais de recherche AlloCiné elle-même). Lit le fichier à
    chaque requête (pas de cache), donc éditable sans reconstruire l'image.
    """
    if not _BIBLIOTHEQUE_PATH.exists():
        raise HTTPException(status_code=500, detail="bibliotheque.html introuvable dans app/static/")
    return HTMLResponse(content=_BIBLIOTHEQUE_PATH.read_text(encoding="utf-8"))


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
        False,
        description=(
            "Rescan complet : ignore le cache AlloCiné et refait la recherche pour TOUS les "
            "titres, ET réécrit la note de TOUS les items Jellyfin correspondants (même ceux "
            "qui ont déjà une note à jour). Voir POST /jellyfin/push-cached-notes pour l'inverse "
            "(écrire ce qui est déjà en cache, sans aucune recherche AlloCiné)."
        ),
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
    nouveau, et Jellyfin n'est réécrit que pour ceux-là. **force_refresh=true
    déclenche un rescan complet des DEUX systèmes** : chaque titre est
    recherché à nouveau sur AlloCiné (le cache est ignoré, pas juste complété)
    et chaque item Jellyfin correspondant est réécrit, y compris ceux qui ont
    déjà une note. C'est l'opération la plus lente et la plus complète des
    trois disponibles (voir aussi DELETE /allocine/cache, qui vide le cache
    sans relancer de synchro, et POST /jellyfin/push-cached-notes, qui écrit
    vers Jellyfin sans aucune recherche AlloCiné).

    La synchro dure plusieurs minutes : elle tourne en arrière-plan et cet
    endpoint répond immédiatement. Suivre l'avancement via GET /jellyfin/statut
    (champs en_cours / nb_traites / nb_items_bibliotheque). Si une opération
    Jellyfin est déjà en cours (cet endpoint ou push-cached-notes), aucune
    seconde opération n'est lancée : celle en cours va jusqu'au bout.
    """
    if not jellyfin_configured():
        raise HTTPException(
            status_code=400,
            detail="JELLYFIN_URL et JELLYFIN_API_KEY doivent être configurés (variables d'environnement).",
        )
    return start_jellyfin_notes_sync_async(force_refresh=force_refresh)


@app.post("/jellyfin/push-cached-notes", response_model=StatutSyncJellyfin, tags=["Jellyfin"])
def jellyfin_push_cached_notes() -> StatutSyncJellyfin:
    """
    Écrit vers Jellyfin les notes AlloCiné déjà présentes dans le cache de
    l'application, SANS effectuer aucune requête vers AlloCiné : les items
    dont le titre n'a pas d'entrée de cache valide sont simplement ignorés
    (ni recherchés, ni modifiés). Beaucoup plus rapide qu'un
    POST /jellyfin/sync-notes classique, puisqu'aucune requête sortante n'est
    faite vers AlloCiné.

    Utile pour appliquer immédiatement des notes déjà connues — par exemple
    juste après avoir corrigé un problème côté Jellyfin (mise à jour rejetée,
    verrou de métadonnées...), sans attendre une nouvelle synchro complète de
    plusieurs minutes pour des titres déjà résolus.

    Nécessite JELLYFIN_URL et JELLYFIN_API_KEY configurés. Tourne en
    arrière-plan comme POST /jellyfin/sync-notes, et partage le même verrou :
    si une synchro complète est déjà en cours, cet appel est ignoré (et
    inversement). Suivre l'avancement via GET /jellyfin/statut.
    """
    if not jellyfin_configured():
        raise HTTPException(
            status_code=400,
            detail="JELLYFIN_URL et JELLYFIN_API_KEY doivent être configurés (variables d'environnement).",
        )
    return start_apply_cached_notes_to_jellyfin_async()


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


@app.get("/jellyfin/library", response_model=List[ItemBibliotheque], tags=["Jellyfin"])
def jellyfin_library() -> List[ItemBibliotheque]:
    """
    Films et séries de la bibliothèque Jellyfin, avec la note AlloCiné déjà
    en cache pour chacun (`allocine: null` si aucune entrée de cache valide —
    cet endpoint ne déclenche jamais de recherche AlloCiné). Alimente la page
    /bibliotheque ; utilisable aussi directement.

    Interroge Jellyfin à chaque appel (liste paginée, pas de cache local sur
    la liste elle-même) pour refléter la bibliothèque actuelle.
    """
    if not jellyfin_configured():
        raise HTTPException(
            status_code=400,
            detail="JELLYFIN_URL et JELLYFIN_API_KEY doivent être configurés (variables d'environnement).",
        )
    try:
        return get_jellyfin_library_with_notes()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erreur lors de la lecture de Jellyfin: {e}")


# Identifiant d'item Jellyfin : GUID de 32 caractères hexadécimaux (forme
# renvoyée par l'API), éventuellement avec tirets. Tout le reste est refusé
# avant d'être inséré dans une URL vers Jellyfin.
_ITEM_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)
_POSTER_WIDTH_DEFAULT = 400
# Largeurs d'affiche autorisées. La largeur demandée est arrondie au palier
# SUPÉRIEUR : sans cela, chaque valeur entière entre 100 et 1000 créerait son
# propre fichier sur disque (jusqu'à ~900 variantes par affiche, par simple
# changement du paramètre `w`).
_POSTER_WIDTHS = (200, 300, 400, 600, 800, 1000)


@app.get("/jellyfin/image/{item_id}", tags=["Jellyfin"])
def jellyfin_image(
    item_id: str,
    w: int = Query(
        _POSTER_WIDTH_DEFAULT,
        description=(
            "Largeur de l'affiche en pixels, arrondie au palier supérieur parmi "
            f"{', '.join(map(str, _POSTER_WIDTHS))} (au-delà de {_POSTER_WIDTHS[-1]} : {_POSTER_WIDTHS[-1]}). "
            "Jellyfin redimensionne lui-même l'image."
        ),
    ),
    tag: Optional[str] = Query(
        None,
        description=(
            "Version de l'affiche (ImageTags.Primary de Jellyfin). Fournie par la page "
            "/bibliotheque : l'entrée de cache est alors valable indéfiniment, et se renouvelle "
            "d'elle-même quand l'affiche change côté Jellyfin."
        ),
    ),
) -> Response:
    """
    Proxy authentifié vers l'affiche (poster) d'un item Jellyfin. Le
    navigateur ne peut pas charger l'image Jellyfin directement (elle exige
    généralement une authentification, et JELLYFIN_URL peut être une adresse
    interne au conteneur, injoignable depuis le navigateur) : cet endpoint
    la récupère avec la clé API du serveur et la retransmet.

    L'image est redimensionnée par Jellyfin (paramètre `w`, 400 px par défaut)
    et GARDÉE SUR DISQUE (voir POSTER_CACHE_DIR ; largeurs arrondies à quelques paliers pour borner l'espace disque) : une affiche déjà vue est
    servie sans aucune requête vers Jellyfin, même si Jellyfin est éteint.
    L'en-tête `X-Poster-Cache` indique HIT (disque) ou MISS (Jellyfin).
    """
    if not _ITEM_ID_RE.match(item_id):
        raise HTTPException(status_code=400, detail="Identifiant d'item Jellyfin invalide.")
    if tag is not None and not image_cache.TAG_RE.match(tag):
        raise HTTPException(status_code=400, detail="Paramètre tag invalide.")
    largeur = next((p for p in _POSTER_WIDTHS if p >= w), _POSTER_WIDTHS[-1])

    # Avec un tag, l'URL change quand l'affiche change : le navigateur peut donc
    # la garder « pour toujours ». Sans tag, une journée.
    cache_control = "public, max-age=31536000, immutable" if tag else "public, max-age=86400"

    cached = image_cache.get(item_id, largeur, tag)
    if cached is not None:
        content, content_type = cached
        return Response(content=content, media_type=content_type,
                        headers={"Cache-Control": cache_control, "X-Poster-Cache": "HIT"})

    if not jellyfin_configured():
        raise HTTPException(status_code=400, detail="JELLYFIN_URL/JELLYFIN_API_KEY non configurés.")
    try:
        result = jellyfin_client.get_item_image(item_id, max_width=largeur)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erreur Jellyfin: {e}")
    if result is None:
        raise HTTPException(status_code=404, detail="Cet item n'a pas d'affiche sur Jellyfin.")
    content, content_type = result
    image_cache.put(item_id, largeur, tag, content, content_type)
    return Response(content=content, media_type=content_type,
                    headers={"Cache-Control": cache_control, "X-Poster-Cache": "MISS"})


@app.delete("/jellyfin/image-cache", tags=["Jellyfin"])
def jellyfin_image_cache_clear() -> dict:
    """Vide le cache disque des affiches (elles seront redemandées à Jellyfin à la prochaine vue)."""
    return {"fichiers_supprimes": image_cache.clear()}


@app.get("/jellyfin/image-cache", tags=["Jellyfin"])
def jellyfin_image_cache_stats() -> dict:
    """Nombre d'affiches en cache et espace disque utilisé."""
    return {"actif": image_cache.enabled(), **image_cache.stats()}


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
