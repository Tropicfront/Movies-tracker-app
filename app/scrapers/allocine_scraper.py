"""
Scraper AlloCiné — récupère uniquement les notes (presse / spectateurs)
pour un film ou une série, à partir de son titre.

Fonctionnement :
1. Recherche du titre via la page de recherche AlloCiné.
2. Récupération du premier résultat pertinent (lien /film/... ou /series/...).
3. Ouverture de la fiche et extraction des notes presse/spectateurs.

Comme pour le scraper Pathé, les sélecteurs CSS peuvent nécessiter un ajustement
si AlloCiné change la structure de ses pages. Activez DEBUG_SAVE_HTML=true
pour inspecter le HTML brut sauvegardé.
"""
import logging
import os
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from app.config import (
    ALLOCINE_BASE_URL,
    ALLOCINE_SEARCH_URL,
    DEFAULT_HEADERS,
    REQUEST_TIMEOUT,
    DEBUG_SAVE_HTML,
    DEBUG_DATA_DIR,
)
from app.models import NoteAlloCine

logger = logging.getLogger("allocine_scraper")


def _save_debug_html(html: str, name: str) -> None:
    if not DEBUG_SAVE_HTML:
        return
    try:
        os.makedirs(DEBUG_DATA_DIR, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        path = os.path.join(DEBUG_DATA_DIR, f"allocine_{safe_name}_{datetime.now():%Y%m%d_%H%M%S}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("HTML de debug sauvegardé: %s", path)
    except OSError as e:
        logger.warning("Impossible de sauvegarder le HTML de debug: %s", e)


def _fetch_html(url: str, params: dict | None = None) -> str:
    resp = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _find_fiche_url(titre: str, type_: str = "film") -> Optional[str]:
    """Recherche le titre sur AlloCiné et retourne l'URL de la première fiche pertinente."""
    html = _fetch_html(ALLOCINE_SEARCH_URL, params={"q": titre})
    _save_debug_html(html, f"recherche_{titre}")
    soup = BeautifulSoup(html, "lxml")

    path_prefix = "/film/fichefilm" if type_ == "film" else "/series/ficheserie"

    link = soup.select_one(f"a[href*='{path_prefix}']")
    if not link:
        # Repli : accepter n'importe quel type de fiche (film ou série)
        link = soup.select_one("a[href*='fichefilm'], a[href*='ficheserie']")

    if not link:
        return None

    href = link.get("href")
    if not href:
        return None
    if href.startswith("http"):
        return href
    return f"{ALLOCINE_BASE_URL}{href}"


def _extract_notes(soup: BeautifulSoup) -> tuple[Optional[float], Optional[float]]:
    """
    Extrait la note presse et la note spectateurs d'une fiche AlloCiné.
    AlloCiné utilise généralement des blocs "rating-item" contenant un label
    ("Presse" / "Spectateurs") et une note dans un élément type "stareval-note".
    """
    note_presse = None
    note_spectateurs = None

    for item in soup.select("[class*='rating-item'], [class*='rating-mdl']"):
        label_el = item.select_one("[class*='rating-title'], [class*='label']")
        note_el = item.select_one("[class*='stareval-note'], [class*='rating-note']")
        if not note_el:
            continue

        note_txt = note_el.get_text(strip=True).replace(",", ".")
        match = re.search(r"(\d+(\.\d+)?)", note_txt)
        if not match:
            continue
        note_val = float(match.group(1))

        label_txt = (label_el.get_text(strip=True).lower() if label_el else "")
        if "presse" in label_txt:
            note_presse = note_val
        elif "spectat" in label_txt:
            note_spectateurs = note_val
        else:
            # Impossible de distinguer : on remplit ce qui manque
            if note_presse is None:
                note_presse = note_val
            elif note_spectateurs is None:
                note_spectateurs = note_val

    return note_presse, note_spectateurs


def get_note_allocine(titre: str, type_: str = "film") -> NoteAlloCine:
    """
    Récupère les notes AlloCiné (presse et spectateurs) pour un film ou une
    série, à partir de son titre.

    :param titre: Titre du film ou de la série
    :param type_: "film" ou "serie"
    """
    try:
        fiche_url = _find_fiche_url(titre, type_)
        if not fiche_url:
            logger.warning("Aucune fiche AlloCiné trouvée pour '%s'", titre)
            return NoteAlloCine(trouve=False)

        html = _fetch_html(fiche_url)
        _save_debug_html(html, f"fiche_{titre}")
        soup = BeautifulSoup(html, "lxml")

        note_presse, note_spectateurs = _extract_notes(soup)

        return NoteAlloCine(
            note_presse=note_presse,
            note_spectateurs=note_spectateurs,
            url_fiche=fiche_url,
            trouve=(note_presse is not None or note_spectateurs is not None),
        )
    except requests.RequestException as e:
        logger.error("Erreur réseau lors de la récupération de la note pour '%s': %s", titre, e)
        return NoteAlloCine(trouve=False)
