"""
Correspondance de titres entre les films Pathé/AlloCiné (souvent en français)
et les titres présents dans la bibliothèque Jellyfin (souvent dans la langue
originale, ex. anglais).

Approche :
1. Alias explicites (fichier JSON éditable, ex: {"Dune : Deuxième Partie": "Dune: Part Two"})
2. Normalisation (accents, casse, ponctuation, articles) + égalité ou inclusion
3. Similarité approximative (difflib) en dernier recours

Limite connue : sans base de données de correspondance de titres (type TMDb),
deux titres très différents dans deux langues (ex. un titre français "libre"
qui ne reprend pas le titre original) peuvent ne pas être rapprochés
automatiquement. Utilisez le fichier d'alias dans ce cas.
"""
import json
import logging
import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Dict

from app.config import TITLE_ALIASES_PATH, TITLE_MATCH_THRESHOLD

logger = logging.getLogger("matching")

_STOPWORDS_PREFIX = (
    "le ", "la ", "les ", "l'", "un ", "une ", "des ",
    "the ", "a ", "an ",
)


def load_title_aliases() -> Dict[str, str]:
    """
    Charge le fichier d'alias de titres. Relu à chaque appel (peu coûteux,
    petit fichier) pour permettre une édition à chaud sans redémarrage.
    """
    if not os.path.exists(TITLE_ALIASES_PATH):
        return {}
    try:
        with open(TITLE_ALIASES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Le fichier d'alias %s ne contient pas un objet JSON, ignoré", TITLE_ALIASES_PATH)
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Impossible de lire le fichier d'alias %s: %s", TITLE_ALIASES_PATH, e)
        return {}


def normalize_title(title: str) -> str:
    """Normalise un titre pour comparaison : minuscules, sans accents/ponctuation/articles."""
    if not title:
        return ""
    nfkd = unicodedata.normalize("NFKD", title)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()

    for prefix in _STOPWORDS_PREFIX:
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break

    cleaned = re.sub(r"[^a-z0-9\s]", " ", lowered)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def titles_match(
    titre_a: str,
    titre_b: str,
    aliases: Dict[str, str] | None = None,
    threshold: float = TITLE_MATCH_THRESHOLD,
) -> bool:
    """Détermine si deux titres désignent probablement la même œuvre."""
    if not titre_a or not titre_b:
        return False

    aliases = aliases or {}
    # Vérification des alias explicites (dans les deux sens)
    for k, v in aliases.items():
        if {k.strip(), v.strip()} == {titre_a.strip(), titre_b.strip()}:
            return True

    norm_a = normalize_title(titre_a)
    norm_b = normalize_title(titre_b)

    if not norm_a or not norm_b:
        return False

    if norm_a == norm_b:
        return True

    # Inclusion (utile pour sous-titres/éditions : "Dune" vs "Dune Deuxieme Partie")
    # uniquement si le titre le plus court fait au moins 4 caractères, pour
    # éviter les faux positifs sur des titres très courts.
    shorter, longer = sorted([norm_a, norm_b], key=len)
    if len(shorter) >= 4 and shorter in longer:
        return True

    ratio = SequenceMatcher(None, norm_a, norm_b).ratio()
    return ratio >= threshold
