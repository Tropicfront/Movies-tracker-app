"""
Client pour l'API Jellyfin.

Utilisé pour :
1. Lister les films/séries de la bibliothèque (pour le matching et pour
   savoir quels titres noter via AlloCiné).
2. Mettre à jour les champs CommunityRating / CriticRating d'un item.

Compatibilité Jellyfin 12.x : depuis la version 12.0 (7 septembre 2026),
Jellyfin désactive par défaut l'authentification "legacy" (en-têtes
X-Emby-Token/X-MediaBrowser-Token, paramètre ?api_key=) et exige le format
strict du schéma "MediaBrowser" pour l'en-tête Authorization, avec les
valeurs entre guillemets : `Authorization: MediaBrowser Token="...", ...`.
Ce client utilise déjà exclusivement les chemins natifs (/Items, /Users) et
l'en-tête Authorization — donc compatible avec Jellyfin 10.x comme 12.x —
mais le format de l'en-tête a été corrigé pour respecter cette syntaxe
stricte (les anciennes versions étaient tolérantes, 12.x l'est moins).

Note sur la mise à jour : pour éviter un bug connu de Jellyfin où l'envoi
d'un payload PARTIEL à POST /Items/{itemId} peut corrompre l'item (nécessitant
un rescan), on récupère systématiquement l'item complet avant de le renvoyer
avec uniquement les deux champs de note modifiés.
"""
import logging
import re
from collections import Counter
from typing import List, Optional, Tuple

import requests

from app.config import (
    JELLYFIN_URL,
    JELLYFIN_API_KEY,
    JELLYFIN_USER_ID,
    JELLYFIN_IGNORE_REGEX,
    REQUEST_TIMEOUT,
)

logger = logging.getLogger("jellyfin_client")


class JellyfinError(Exception):
    pass


def _headers() -> dict:
    # Schéma d'authentification Jellyfin, format strict requis depuis 12.x :
    # valeurs entre guillemets doubles, séparées par des virgules.
    auth = (
        'MediaBrowser Client="AllocinePatheJellyfinSync", '
        'Device="Docker", '
        'DeviceId="allocine-pathe-jellyfin-sync", '
        'Version="1.0.0", '
        f'Token="{JELLYFIN_API_KEY}"'
    )
    return {
        "Authorization": auth,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _items_base_url() -> str:
    if JELLYFIN_USER_ID:
        return f"{JELLYFIN_URL}/Users/{JELLYFIN_USER_ID}/Items"
    return f"{JELLYFIN_URL}/Items"


def get_library_items() -> List[dict]:
    """
    Récupère les films et séries de la bibliothèque Jellyfin.
    Retourne une liste de dicts avec au moins: Id, Name, Type.

    Paginé explicitement (StartIndex/Limit), plutôt que de se fier au
    comportement par défaut de l'API quand ces paramètres sont omis : ce
    comportement diffère selon les endpoints et les versions de Jellyfin (par
    exemple, certains endpoints appliquent une limite implicite de quelques
    dizaines d'éléments sans qu'on l'ait demandé). Sans pagination explicite,
    une bibliothèque de plusieurs centaines d'items risque d'être tronquée
    silencieusement — c'est exactement ce qui a été constaté (445 items
    traités pour 686 attendus). La boucle s'arrête sur TotalRecordCount, la
    valeur faisant foi renvoyée par le serveur.

    Les collections (BoxSet) et les éléments dont le nom correspond à
    JELLYFIN_IGNORE_REGEX (par défaut les regroupements "… - Saga") sont
    exclus : ils n'ont pas de fiche AlloCiné précise et recevraient une note
    d'un film au hasard.
    """
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise JellyfinError("JELLYFIN_URL / JELLYFIN_API_KEY non configurés")

    page_size = 200
    items: List[dict] = []
    start_index = 0
    total_record_count: Optional[int] = None

    while True:
        params = {
            "IncludeItemTypes": "Movie,Series",
            "ExcludeItemTypes": "BoxSet",
            "Recursive": "true",
            "Fields": "ProviderIds,Overview,Genres,ImageTags",
            # Tri DÉTERMINISTE obligatoire avec StartIndex/Limit : sans ordre
            # stable, deux pages successives peuvent se chevaucher ou laisser
            # des trous (un item apparaît deux fois, un autre jamais). C'est
            # le tri que Jellyfin utilise lui-même pour paginer ses écrans.
            "SortBy": "SortName",
            "SortOrder": "Ascending",
            # On n'a besoin que de l'affiche principale : évite de renvoyer
            # les étiquettes de toutes les autres images (fond, logo...).
            "EnableImages": "true",
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary",
            "StartIndex": start_index,
            "Limit": page_size,
        }
        resp = requests.get(
            _items_base_url(), headers=_headers(), params=params, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        page_items = data.get("Items", [])
        total_record_count = data.get("TotalRecordCount", total_record_count)

        items.extend(page_items)
        logger.debug(
            "Page Jellyfin: %d item(s) reçu(s) (StartIndex=%d), total annoncé=%s, cumul=%d",
            len(page_items), start_index, total_record_count, len(items),
        )

        if not page_items:
            break  # sécurité : page vide, on arrête même si TotalRecordCount dit autre chose
        start_index += len(page_items)
        if total_record_count is not None and start_index >= total_record_count:
            break

    # Garde-fou : même avec un tri, un item ajouté/supprimé pendant la lecture
    # peut décaler les pages. On ne garde qu'une occurrence par Id.
    vus: set = set()
    uniques: List[dict] = []
    for it in items:
        it_id = it.get("Id")
        if it_id in vus:
            continue
        vus.add(it_id)
        uniques.append(it)
    if len(uniques) != len(items):
        logger.warning(
            "%d doublon(s) retiré(s) de la liste Jellyfin (bibliothèque modifiée pendant la lecture ?).",
            len(items) - len(uniques),
        )
    items = uniques

    if total_record_count is not None and len(items) != total_record_count:
        logger.warning(
            "Jellyfin a annoncé %d item(s) au total mais %d ont été effectivement récupérés "
            "après pagination : une bibliothèque a peut-être changé pendant la lecture.",
            total_record_count, len(items),
        )
    logger.info(
        "Bibliothèque Jellyfin : %d film(s)/série(s) récupéré(s) (sur %s annoncé(s) par le serveur, "
        "%d page(s) de %d).",
        len(items), total_record_count if total_record_count is not None else "?",
        (start_index // page_size) + (1 if start_index % page_size else 0), page_size,
    )

    ignore = re.compile(JELLYFIN_IGNORE_REGEX, re.IGNORECASE) if JELLYFIN_IGNORE_REGEX else None
    kept: List[dict] = []
    ignored: List[dict] = []
    for item in items:
        name = item.get("Name") or ""
        if item.get("Type") not in ("Movie", "Series") or (ignore and ignore.search(name)):
            ignored.append(item)
        else:
            kept.append(item)

    if ignored:
        types = dict(Counter(i.get("Type") for i in ignored))
        logger.info(
            "Bibliothèque Jellyfin : %d élément(s) ignoré(s) (collections / motif %r), types vus : %s. "
            "Exemples : %s",
            len(ignored), JELLYFIN_IGNORE_REGEX, types,
            ", ".join(repr(i.get("Name")) for i in ignored[:5]),
        )
    logger.info(
        "Bibliothèque Jellyfin : %d film(s)/série(s) retenu(s) après filtrage (sur %d récupéré(s)).",
        len(kept), len(items),
    )
    return kept


def _get_full_item(item_id: str) -> Optional[dict]:
    """
    Récupère l'objet complet d'un item via /Items?Ids=... (plus fiable
    d'une version de Jellyfin à l'autre que GET /Items/{itemId} direct).
    """
    params = {
        "Ids": item_id,
        "Recursive": "true",
        "Fields": "Overview,Genres,ProviderIds,Studios,Tags,ProductionYear,PremiereDate,CommunityRating,CriticRating,LockedFields,LockData",
    }
    resp = requests.get(
        _items_base_url(), headers=_headers(), params=params, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    items = resp.json().get("Items", [])
    return items[0] if items else None


def update_item_ratings(
    item_id: str,
    community_rating: Optional[float] = None,
    critic_rating: Optional[float] = None,
) -> bool:
    """
    Met à jour CommunityRating et/ou CriticRating pour un item Jellyfin.

    Verrouille l'item (LockData=true) pour que Jellyfin ne considère plus ses
    métadonnées comme gérées par ses propres fournisseurs et ne les écrase pas
    à la prochaine actualisation (scan de bibliothèque, rafraîchissement
    programmé...) — sans ce verrou, la mise à jour semble réussir sur le
    moment (la requête répond 200) mais ne "tient" pas dans le temps.
    Comportement documenté et largement rapporté côté Jellyfin, pas propre à
    ce projet.

    "CommunityRating" et "CriticRating" ne sont PAS ajoutés individuellement à
    LockedFields : une tentative en ce sens a été testée et rejetée par un
    vrai serveur Jellyfin (12.x) avec une erreur 400 explicite — ces deux noms
    ne font pas partie de l'énumération MetadataField que le serveur accepte
    pour ce champ (elle couvre des champs comme Cast, Genres, Overview...,
    pas les notes). Le verrou reste donc au niveau de l'item entier (LockData)
    plutôt que par champ : plus large (il protège aussi les autres métadonnées
    de l'item contre un rafraîchissement automatique), mais c'est la seule
    option qui fonctionne réellement contre cette version de l'API. Un verrou
    déjà posé par vous dans Jellyfin (LockedFields sur d'autres champs) est
    conservé tel quel, sans y toucher.

    Retourne True si la mise à jour a réussi.
    """
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise JellyfinError("JELLYFIN_URL / JELLYFIN_API_KEY non configurés")

    full_item = _get_full_item(item_id)
    if full_item is None:
        logger.warning("Item Jellyfin introuvable pour mise à jour: %s", item_id)
        return False

    if community_rating is not None:
        full_item["CommunityRating"] = round(community_rating, 1)
    if critic_rating is not None:
        full_item["CriticRating"] = round(critic_rating, 1)
    # LockedFields n'est PAS modifié (voir docstring) : on renvoie tel quel
    # ce que le GET a retourné, pour ne rien perdre d'un verrou existant.
    full_item["LockData"] = True

    resp = requests.post(
        f"{JELLYFIN_URL}/Items/{item_id}",
        headers=_headers(),
        json=full_item,
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        logger.error(
            "Échec mise à jour Jellyfin pour l'item %s (%s): %s",
            item_id, resp.status_code, resp.text[:300],
        )
        return False
    return True


def get_item_image(
    item_id: str, image_type: str = "Primary", max_width: Optional[int] = None
) -> Optional[Tuple[bytes, str]]:
    """
    Récupère l'image (par défaut : affiche/poster) d'un item Jellyfin.

    Les images Jellyfin ne sont pas forcément accessibles publiquement sans
    authentification selon la configuration du serveur ; cette fonction sert
    donc de proxy authentifié (voir GET /jellyfin/image/{item_id} dans
    main.py), plutôt que de construire une URL Jellyfin directe côté
    navigateur — qui échouerait aussi si JELLYFIN_URL n'est joignable que
    depuis le conteneur (ex. nom d'hôte Docker interne).

    :param max_width: largeur maximale demandée à Jellyfin, qui redimensionne
        lui-même l'image (paramètre maxWidth). Indispensable pour une grille de
        plusieurs centaines d'affiches : l'original fait souvent plusieurs
        centaines de Ko à plusieurs Mo, alors qu'une carte en affiche ~250 px.

    Retourne (contenu_binaire, content_type) ou None si l'item n'a pas
    d'image de ce type (404 côté Jellyfin, traité comme un cas normal, pas
    une erreur).
    """
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise JellyfinError("JELLYFIN_URL / JELLYFIN_API_KEY non configurés")

    params = {}
    if max_width:
        params["maxWidth"] = int(max_width)
        params["quality"] = 85

    resp = requests.get(
        f"{JELLYFIN_URL}/Items/{item_id}/Images/{image_type}",
        headers=_headers(),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return resp.content, content_type
