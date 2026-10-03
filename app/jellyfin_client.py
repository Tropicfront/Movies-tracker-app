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
from dataclasses import dataclass
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


_PAGE_SIZE = 200
_LIST_FIELDS = "ProviderIds,Overview,Genres,ImageTags,CommunityRating,CriticRating"


def _fetch_paginated(extra_params: dict, what: str = "bibliothèque") -> tuple:
    """
    Lit TOUTES les pages d'une requête /Items et retourne (items, total_annoncé).

    Paginé explicitement (StartIndex/Limit) et TRIÉ : sans ordre stable, deux
    pages successives peuvent se chevaucher ou laisser des trous. Les doublons
    éventuels sont retirés. La boucle s'arrête sur TotalRecordCount (valeur
    faisant foi renvoyée par le serveur) ou sur une page vide.
    """
    items: List[dict] = []
    start_index = 0
    total: Optional[int] = None
    avertissement_redirection = False

    while True:
        params = {
            "IncludeItemTypes": "Movie,Series",
            "Recursive": "true",
            "Fields": _LIST_FIELDS,
            # Tri déterministe obligatoire avec StartIndex/Limit.
            "SortBy": "SortName",
            "SortOrder": "Ascending",
            # Seule l'affiche principale nous intéresse.
            "EnableImages": "true",
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary",
            "StartIndex": start_index,
            "Limit": _PAGE_SIZE,
            **extra_params,
        }
        resp = requests.get(
            _items_base_url(), headers=_headers(), params=params, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        if resp.history and not avertissement_redirection:
            avertissement_redirection = True
            logger.warning(
                "JELLYFIN_URL est redirigé (%s -> %s). Les lectures suivent la redirection, "
                "mais pas les écritures : mettez l'URL finale dans JELLYFIN_URL.",
                JELLYFIN_URL, resp.url,
            )
        data = resp.json()
        page_items = data.get("Items", [])
        total = data.get("TotalRecordCount", total)
        items.extend(page_items)
        logger.debug(
            "Page Jellyfin (%s): %d item(s) (StartIndex=%d), total annoncé=%s, cumul=%d",
            what, len(page_items), start_index, total, len(items),
        )
        if not page_items:
            break  # page vide : on s'arrête même si TotalRecordCount dit autre chose
        start_index += len(page_items)
        if total is not None and start_index >= total:
            break

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
    if total is not None and len(uniques) != total:
        logger.warning(
            "Jellyfin a annoncé %d item(s) mais %d ont été récupérés (%s) : "
            "une bibliothèque a peut-être changé pendant la lecture.",
            total, len(uniques), what,
        )
    return uniques, total


def _members_of_collection(boxset_id: str) -> List[dict]:
    """Films/séries contenus dans une collection (BoxSet) Jellyfin."""
    members, _ = _fetch_paginated(
        {"ParentId": boxset_id, "CollapseBoxSetItems": "false"}, what=f"collection {boxset_id}"
    )
    return members


# Compte annoncé par Jellyfin (/Items/Counts), gardé 60 s : get_library_items() est
# appelé à chaque ouverture de la page bibliothèque et à chaque synchro.
_COUNTS_CACHE: tuple = (0.0, None)


def get_library_counts() -> Optional[Tuple[int, int]]:
    """
    (nombre de films, nombre de séries) annoncés par Jellyfin lui-même, ou None
    si l'information est indisponible. Sert à repérer des titres MANQUANTS :
    on compare ce chiffre à ce que la liste a réellement renvoyé.

    Indicatif seulement : Jellyfin a déjà renvoyé ici un compte différent de celui
    de son interface web (ticket jellyfin/jellyfin#13970). Un écart déclenche donc
    un avertissement, jamais une erreur. Appel SANS userId volontairement : le
    compte est global, ce qui permet justement de voir si l'utilisateur
    JELLYFIN_USER_ID est limité (droits, contrôle parental) par rapport au total.
    """
    global _COUNTS_CACHE
    import time
    ts, val = _COUNTS_CACHE
    if val is not None and time.time() - ts < 60:
        return val
    try:
        resp = requests.get(f"{JELLYFIN_URL}/Items/Counts", headers=_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        d = resp.json()
        val = (int(d["MovieCount"]), int(d["SeriesCount"]))
    except Exception as e:
        logger.debug("Compte Jellyfin (/Items/Counts) indisponible : %s", e)
        return None
    _COUNTS_CACHE = (time.time(), val)
    return val


def get_library_items() -> List[dict]:
    """
    Récupère les films et séries de la bibliothèque Jellyfin.
    Retourne une liste de dicts avec au moins: Id, Name, Type.

    Films qui "disparaissent" : par défaut Jellyfin peut MASQUER les films d'une
    collection derrière la collection elle-même (paramètre collapseBoxSetItems).
    Une bibliothèque rangée en sagas (« Alien - Saga »...) perd alors tous les
    films de ces sagas dans la liste : constaté ici, 443 films/séries reçus pour
    687 attendus, et 59 collections renvoyées alors qu'on ne demande que des
    films et des séries. On désactive donc explicitement ce repliage.

    Filet de sécurité : si le serveur renvoie malgré tout des collections, on
    ouvre chacune (ParentId) pour en ajouter les membres manquants, au lieu de
    se fier à un seul paramètre qu'on ne peut pas tester sur toutes les versions.

    Les collections (BoxSet) et les éléments dont le nom correspond à
    JELLYFIN_IGNORE_REGEX (par défaut les regroupements "… - Saga") ne sont
    jamais retournés eux-mêmes : ils n'ont pas de fiche AlloCiné précise et
    recevraient une note d'un film au hasard.
    """
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise JellyfinError("JELLYFIN_URL / JELLYFIN_API_KEY non configurés")

    items, total = _fetch_paginated({"CollapseBoxSetItems": "false"})
    logger.info(
        "Bibliothèque Jellyfin : %d élément(s) récupéré(s) (%s annoncé(s) par le serveur).",
        len(items), total if total is not None else "?",
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

    boxsets = [i for i in ignored if i.get("Type") == "BoxSet"]
    if boxsets:
        # Normalement impossible : on ne demande que des films et des séries.
        # Cela signifie que le serveur replie les films derrière leurs collections.
        logger.warning(
            "Jellyfin renvoie %d collection(s) alors que seuls des films/séries sont demandés : "
            "les films qu'elles contiennent sont probablement masqués. Ouverture de chaque collection "
            "pour les récupérer.", len(boxsets),
        )
        deja = {_normalise_id(i.get("Id")) for i in kept}
        ajoutes = 0
        for bs in boxsets:
            try:
                for m in _members_of_collection(bs["Id"]):
                    if _normalise_id(m.get("Id")) in deja:
                        continue
                    if m.get("Type") not in ("Movie", "Series"):
                        continue
                    if ignore and ignore.search(m.get("Name") or ""):
                        continue
                    deja.add(_normalise_id(m.get("Id")))
                    kept.append(m)
                    ajoutes += 1
            except Exception as e:
                logger.warning("Collection %r illisible (%s) : ses films ne seront pas traités.", bs.get("Name"), e)
        logger.info("%d film(s)/série(s) récupéré(s) en ouvrant %d collection(s).", ajoutes, len(boxsets))

    if ignored:
        types = dict(Counter(i.get("Type") for i in ignored))
        logger.info(
            "Bibliothèque Jellyfin : %d élément(s) écarté(s) (collections / motif %r), types vus : %s. "
            "Exemples : %s",
            len(ignored), JELLYFIN_IGNORE_REGEX, types,
            ", ".join(repr(i.get("Name")) for i in ignored[:5]),
        )
    par_type = Counter(i.get("Type") for i in kept)
    logger.info(
        "Bibliothèque Jellyfin : %d film(s) + %d série(s) = %d titre(s) retenu(s).",
        par_type.get("Movie", 0), par_type.get("Series", 0), len(kept),
    )

    # Recoupement avec le compte annoncé par Jellyfin : des titres peuvent manquer
    # pour une raison autre que les collections (droits de l'utilisateur
    # JELLYFIN_USER_ID, contrôle parental, bibliothèque non accessible...).
    annonce = get_library_counts()
    if annonce is not None:
        films_jf, series_jf = annonce
        # les "… - Saga" écartés par la regex sont bien présents côté Jellyfin : on les compte
        films_vus = par_type.get("Movie", 0) + sum(1 for i in ignored if i.get("Type") == "Movie")
        series_vus = par_type.get("Series", 0) + sum(1 for i in ignored if i.get("Type") == "Series")
        if films_vus < films_jf or series_vus < series_jf:
            logger.warning(
                "Jellyfin annonce %d film(s) et %d série(s), mais seuls %d film(s) et %d série(s) ont pu être "
                "récupérés : il manque %d film(s) et %d série(s). Causes possibles : utilisateur JELLYFIN_USER_ID "
                "aux droits restreints (essayez sans), contrôle parental, ou bibliothèque non accessible à la clé API. "
                "(Le compte de Jellyfin est indicatif : il peut différer de celui de son interface.)",
                films_jf, series_jf, films_vus, series_vus,
                max(0, films_jf - films_vus), max(0, series_jf - series_vus),
            )
        else:
            logger.info("Recoupement OK : Jellyfin annonce %d film(s) et %d série(s).", films_jf, series_jf)
    return kept


def _normalise_id(x) -> str:
    """Identifiant Jellyfin sous forme comparable (minuscules, sans tirets)."""
    return str(x or "").replace("-", "").lower()


# Champs demandés pour relire un item AVANT de le renvoyer à POST /Items/{id}.
# Jellyfin réécrit l'item à partir du corps reçu : un champ absent de ce corps
# peut être remis à vide. Beaucoup de champs ne sont renvoyés que s'ils sont
# explicitement demandés (titre original, titre de tri, accroche, pays de
# production, note personnalisée...) : on les demande tous pour ne rien perdre.
# Une valeur inconnue dans cette liste est ignorée par Jellyfin (constaté).
_FULL_ITEM_FIELDS = ",".join([
    "Overview", "Genres", "ProviderIds", "Studios", "Tags", "ProductionYear",
    "PremiereDate", "CommunityRating", "CriticRating", "LockedFields", "LockData",
    "OriginalTitle", "SortName", "CustomRating", "Taglines", "ProductionLocations",
    "ExternalUrls", "RemoteTrailers", "DateCreated",
])


def _get_full_item(item_id: str) -> Optional[dict]:
    """
    Récupère l'objet complet d'un item via /Items?Ids=... (plus fiable
    d'une version de Jellyfin à l'autre que GET /Items/{itemId} direct).

    Deux protections, car cet objet est ensuite renvoyé tel quel à Jellyfin :
    - CollapseBoxSetItems=false : sans ça, Jellyfin peut remplacer un film
      appartenant à une collection par la COLLECTION elle-même dans le résultat.
    - l'Id du résultat doit être exactement celui demandé. Sinon on retourne
      None : écrire les données d'un autre item (par exemple le nom et le
      synopsis d'une collection) sur le film demandé l'écraserait.
    """
    params = {
        "Ids": item_id,
        "Recursive": "true",
        "CollapseBoxSetItems": "false",
        "Fields": _FULL_ITEM_FIELDS,
    }
    resp = requests.get(
        _items_base_url(), headers=_headers(), params=params, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    items = resp.json().get("Items", [])
    for it in items:
        if _normalise_id(it.get("Id")) == _normalise_id(item_id):
            return it
    if items:
        logger.error(
            "Jellyfin a renvoyé un AUTRE item (%r, id %s, type %s) pour la demande de %s : "
            "écriture annulée, pour ne jamais écraser un item avec les données d'un autre.",
            items[0].get("Name"), items[0].get("Id"), items[0].get("Type"), item_id,
        )
    return None


@dataclass
class ResultatMiseAJour:
    """
    Issue d'une mise à jour de notes. Une réponse HTTP positive ne prouve pas
    que la valeur est bien enregistrée : on relit l'item pour le vérifier.

    acceptee  : Jellyfin a répondu 200/204 (sans redirection).
    persistee : True = relu et identique à ce qui a été envoyé ; False = relu
                mais DIFFÉRENT (accepté puis ignoré/écrasé) ; None = relecture
                impossible, non vérifié.
    """
    acceptee: bool
    persistee: Optional[bool] = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.acceptee and self.persistee is not False


_TOLERANCE_NOTE = 0.06  # Jellyfin stocke un flottant : 8.4 peut revenir en 8.399999


def _ecarts(attendu: dict, relu: dict) -> List[str]:
    """Liste lisible des champs dont la valeur relue diffère de celle envoyée."""
    out = []
    for champ, valeur in attendu.items():
        lue = relu.get(champ)
        if lue is None or abs(float(lue) - float(valeur)) > _TOLERANCE_NOTE:
            out.append(f"{champ}: envoyé {valeur}, relu {lue}")
    return out


def _post_full_item(item_id: str, full_item: dict) -> Optional[str]:
    """
    Renvoie l'item complet à Jellyfin (POST /Items/{id}). Retourne None si Jellyfin a
    répondu 200/204, sinon un message d'erreur lisible.

    La requête n'est JAMAIS redirigée automatiquement : un client HTTP qui suit une
    redirection 301/302 transforme un POST en GET et abandonne le corps, ce qui ressemble
    à un succès (200) sans rien écrire. Une redirection est donc traitée comme un échec
    explicite, avec l'adresse vers laquelle Jellyfin redirige.
    """
    resp = requests.post(
        f"{JELLYFIN_URL}/Items/{item_id}",
        headers=_headers(),
        json=full_item,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=False,
    )
    if resp.status_code in (200, 204):
        return None
    if 300 <= resp.status_code < 400:
        detail = (
            f"redirection HTTP {resp.status_code} vers {resp.headers.get('Location')!r} : "
            "l'écriture n'a PAS été effectuée. Mettez l'URL finale (ex. https://…) dans JELLYFIN_URL."
        )
    else:
        detail = f"HTTP {resp.status_code}: {resp.text[:300]}"
    logger.error("Échec mise à jour Jellyfin pour l'item %s (%s)", item_id, detail)
    return detail


def update_item_ratings(
    item_id: str,
    community_rating: Optional[float] = None,
    critic_rating: Optional[float] = None,
) -> ResultatMiseAJour:
    """
    Met à jour CommunityRating et/ou CriticRating pour un item Jellyfin, puis
    RELIT l'item pour vérifier que les valeurs sont bien enregistrées.

    Verrouille l'item (LockData=true) pour que Jellyfin ne considère plus ses
    métadonnées comme gérées par ses propres fournisseurs et ne les écrase pas
    à la prochaine actualisation (scan de bibliothèque, rafraîchissement
    programmé...) — sans ce verrou, la mise à jour semble réussir sur le
    moment mais ne "tient" pas dans le temps.

    "CommunityRating" et "CriticRating" ne sont PAS ajoutés individuellement à
    LockedFields : une tentative en ce sens a été rejetée par un vrai serveur
    Jellyfin (12.x) avec une erreur 400 (ces noms ne font pas partie de
    l'énumération MetadataField acceptée). Le verrou est donc posé sur l'item
    entier (LockData). Un verrou déjà posé par vous sur d'autres champs
    (LockedFields) est conservé tel quel.

    La requête n'est JAMAIS redirigée automatiquement : un client HTTP qui suit
    une redirection 301/302 transforme un POST en GET et abandonne le corps,
    ce qui ressemble à un succès (200) sans rien écrire. Une redirection est
    donc traitée comme un échec explicite.
    """
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise JellyfinError("JELLYFIN_URL / JELLYFIN_API_KEY non configurés")

    full_item = _get_full_item(item_id)
    if full_item is None:
        logger.warning("Item Jellyfin introuvable (ou ambigu) pour mise à jour: %s", item_id)
        return ResultatMiseAJour(False, None, "item introuvable, ou Jellyfin a renvoyé un autre item")

    envoye = {}
    if community_rating is not None:
        full_item["CommunityRating"] = envoye["CommunityRating"] = round(community_rating, 1)
    if critic_rating is not None:
        full_item["CriticRating"] = envoye["CriticRating"] = round(critic_rating, 1)
    # LockedFields n'est PAS modifié (voir docstring) : on renvoie tel quel
    # ce que le GET a retourné, pour ne rien perdre d'un verrou existant.
    full_item["LockData"] = True

    erreur = _post_full_item(item_id, full_item)
    if erreur:
        return ResultatMiseAJour(False, None, erreur)

    # Relecture : la réponse 2xx ne prouve pas que la valeur est enregistrée.
    try:
        relu = _get_full_item(item_id)
    except Exception as e:  # relecture impossible : on ne prétend pas avoir vérifié
        logger.warning("Relecture impossible pour l'item %s (%s) : écriture non vérifiée.", item_id, e)
        return ResultatMiseAJour(True, None, f"relecture impossible: {e}")
    if relu is None:
        return ResultatMiseAJour(True, None, "relecture : item introuvable")
    ecarts = _ecarts(envoye, relu)
    if ecarts:
        return ResultatMiseAJour(True, False, "Jellyfin a accepté mais ne conserve pas la valeur — " + "; ".join(ecarts))
    return ResultatMiseAJour(True, True, "")


# Les notes écrites par l'application ont UNE décimale : on exige une égalité stricte pour
# reconnaître nos valeurs. Une tolérance large (0,06) prendrait pour les nôtres des notes
# TMDb comme 8,222 ; une note Rotten Tomatoes entière égale à la nôtre est, elle, sans risque
# (la retirer puis la retélécharger donne la même valeur).
_TOLERANCE_NETTOYAGE = 0.001


def clear_item_ratings(item_id: str, attendu: dict) -> ResultatMiseAJour:
    """
    Retire d'un item les notes que l'application y avait écrites (étoile et/ou tomate) et
    le verrou posé en même temps, pour que Jellyfin puisse à nouveau les remplir lui-même.

    :param attendu: {"CommunityRating": x, "CriticRating": y} — UNIQUEMENT les champs à retirer,
        avec la valeur que l'application y a écrite. Un champ n'est vidé que si sa valeur
        ACTUELLE est encore celle-là : si quelqu'un (ou Jellyfin) l'a changée depuis, c'est une
        donnée qui n'est plus la nôtre, elle est laissée intacte. Les champs absents de
        `attendu` ne sont jamais touchés (ex. une tomate Rotten Tomatoes d'origine).

    Retourne acceptee=False sans rien écrire si l'item est introuvable/ambigu ou si plus aucune
    valeur ne correspond ; sinon relit l'item pour vérifier que les champs sont bien vides.
    """
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        raise JellyfinError("JELLYFIN_URL / JELLYFIN_API_KEY non configurés")

    full_item = _get_full_item(item_id)
    if full_item is None:
        return ResultatMiseAJour(False, None, "item introuvable, ou Jellyfin a renvoyé un autre item")

    a_vider = []
    for champ, valeur in attendu.items():
        actuelle = full_item.get(champ)
        if actuelle is not None and abs(float(actuelle) - float(valeur)) <= _TOLERANCE_NETTOYAGE:
            a_vider.append(champ)
    if not a_vider:
        return ResultatMiseAJour(False, None, "aucune valeur ne correspond plus à celle écrite par l'application : item laissé intact")

    for champ in a_vider:
        full_item[champ] = None
    full_item["LockData"] = False

    erreur = _post_full_item(item_id, full_item)
    if erreur:
        return ResultatMiseAJour(False, None, erreur)

    try:
        relu = _get_full_item(item_id)
    except Exception as e:
        return ResultatMiseAJour(True, None, f"relecture impossible: {e}")
    if relu is None:
        return ResultatMiseAJour(True, None, "relecture : item introuvable")
    restes = [c for c in a_vider if relu.get(c) is not None]
    if restes:
        return ResultatMiseAJour(True, False, "Jellyfin a accepté mais n'a pas vidé : " + ", ".join(restes))
    return ResultatMiseAJour(True, True, "")


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
