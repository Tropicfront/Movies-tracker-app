"""
Client pour l'API Jellyfin.

Utilisé pour :
1. Lister les films/séries de la bibliothèque (pour le matching avec Pathé
   et pour savoir quels titres noter via AlloCiné).
2. Mettre à jour les champs CommunityRating / CriticRating d'un item.

Note sur la mise à jour : pour éviter un bug connu de Jellyfin où l'envoi
d'un payload PARTIEL à POST /Items/{itemId} peut corrompre l'item (nécessitant
un rescan), on récupère systématiquement l'item complet avant de le renvoyer
avec uniquement les deux champs de note modifiés.
"""
import logging
from typing import List, Optional

import requests

from app.config import (
    JELLYFIN_URL,
    JELLYFIN_API_KEY,
    JELLYFIN_USER_ID,
    REQUEST_TIMEOUT,
)

logger = logging.getLogger("jellyfin_client")


class JellyfinError(Exception):
    pass


def _headers() -> dict:
    return {
        "Authorization": f"MediaBrowser Token={JELLYFIN_API_KEY}",
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
    """
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise JellyfinError("JELLYFIN_URL / JELLYFIN_API_KEY non configurés")

    params = {
        "IncludeItemTypes": "Movie,Series",
        "Recursive": "true",
        "Fields": "ProviderIds",
    }
    resp = requests.get(
        _items_base_url(), headers=_headers(), params=params, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("Items", [])


def _get_full_item(item_id: str) -> Optional[dict]:
    """
    Récupère l'objet complet d'un item via /Items?Ids=... (plus fiable
    d'une version de Jellyfin à l'autre que GET /Items/{itemId} direct).
    """
    params = {
        "Ids": item_id,
        "Recursive": "true",
        "Fields": "Overview,Genres,ProviderIds,Studios,Tags,ProductionYear,PremiereDate,CommunityRating,CriticRating",
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
