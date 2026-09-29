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
from typing import List, Optional

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

    Les collections (BoxSet) et les éléments dont le nom correspond à
    JELLYFIN_IGNORE_REGEX (par défaut les regroupements "… - Saga") sont
    exclus : ils n'ont pas de fiche AlloCiné précise et recevraient une note
    d'un film au hasard.
    """
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise JellyfinError("JELLYFIN_URL / JELLYFIN_API_KEY non configurés")

    params = {
        "IncludeItemTypes": "Movie,Series",
        "ExcludeItemTypes": "BoxSet",
        "Recursive": "true",
        "Fields": "ProviderIds",
    }
    resp = requests.get(
        _items_base_url(), headers=_headers(), params=params, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    items = resp.json().get("Items", [])

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
