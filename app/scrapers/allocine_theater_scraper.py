"""
Scraper principal : films à l'affiche, séances ET notes AlloCiné pour le
Pathé Toulouse Wilson — le tout depuis UNE SEULE page AlloCiné
(/seance/salle_gen_csalle=P0057.html), plutôt que depuis pathe.fr.

Pourquoi ce choix : pathe.fr est protégé par Akamai Bot Manager, qui a résisté
à toutes les tentatives de contournement (en-têtes réalistes, curl_cffi avec
empreinte TLS de Chrome, Playwright/Chromium headless). AlloCiné, en
revanche, n'a jamais bloqué nos requêtes, et héberge lui-même la page des
séances de ce cinéma — avec en prime les notes presse/spectateurs déjà
présentes sur la même page. C'est donc la source la plus fiable disponible.

Limites connues de cette approche :
- La page ne montre que les séances du jour actuellement sélectionné
  (aujourd'hui par défaut). AlloCiné propose un sélecteur de date, mais son
  paramètre d'URL exact n'a pas pu être vérifié (accès direct au site
  indisponible depuis l'environnement de développement). Si vous identifiez
  ce paramètre (inspectez les requêtes réseau du sélecteur de date dans votre
  navigateur), je peux étendre le scraper pour couvrir plusieurs jours.
- La durée des films n'est pas affichée sur cette page (contrairement à la
  page pathe.fr) : le champ `duree` du modèle Film restera vide.
- L'extraction repose sur la structure textuelle de la page plutôt que sur
  des classes CSS précises (non vérifiables sans accès direct au HTML brut).
  Activez DEBUG_SAVE_HTML=true et inspectez le fichier sauvegardé dans
  /app/data en cas de résultat vide ou incomplet.
"""
import logging
import os
import re
from datetime import datetime
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.config import (
    ALLOCINE_SALLE_URL,
    ALLOCINE_BASE_URL,
    DEFAULT_HEADERS,
    REQUEST_TIMEOUT,
    DEBUG_SAVE_HTML,
    DEBUG_DATA_DIR,
)
from app.models import Film, Seance, NoteAlloCine

logger = logging.getLogger("allocine_theater_scraper")

_session = requests.Session()
_session.headers.update(DEFAULT_HEADERS)

# Repère un bloc film : titre + lien vers sa fiche AlloCiné
_FILM_HEADING_PATTERN = re.compile(r"fichefilm_gen_cfilm=")

# Date + genres + pays, ex: "12 août 2026 | Comédie, Famille / France"
_DATE_GENRE_PATTERN = re.compile(
    r"(\d{1,2}\s+[a-zéèûôA-ZÉÈÛÔ]+\s+\d{4})\s*\|\s*([^/\n]+)/\s*([^\n]+)"
)

# Notes presse/spectateurs, ex: "Presse\n2,6" / "Spectateurs\n3,5"
_NOTE_PRESSE_PATTERN = re.compile(r"Presse\s*\n?\s*([\d,]+)")
_NOTE_SPECT_PATTERN = re.compile(r"Spectateurs\s*\n?\s*([\d,]+)")

# Bloc séances, ex: "15 août 2026 -\nEn VF\n16:30 ...\n19:30 ..."
_SEANCES_PATTERN = re.compile(
    r"(\d{1,2}\s+[a-zéèûôA-ZÉÈÛÔ]+\s+\d{4})\s*-\s*\n?\s*En\s+([^\d\n]+?)\n"
    r"((?:\s*\d{1,2}:\d{2}[^\n]*\n?)+)"
)


def _save_debug_html(html: str, name: str) -> None:
    if not DEBUG_SAVE_HTML:
        return
    try:
        os.makedirs(DEBUG_DATA_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DATA_DIR, f"{name}_{datetime.now():%Y%m%d_%H%M%S}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("HTML de debug sauvegardé: %s", path)
    except OSError as e:
        logger.warning("Impossible de sauvegarder le HTML de debug: %s", e)


def _fetch_html(url: str) -> str:
    resp = _session.get(url, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        _save_debug_html(resp.text, f"erreur_{resp.status_code}_allocine_salle")
        logger.warning("Réponse HTTP %s pour %s", resp.status_code, url)
    resp.raise_for_status()
    return resp.text


def _parse_note(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _extract_seances(block_text: str) -> List[Seance]:
    seances: List[Seance] = []
    for date_str, version, times_blob in _SEANCES_PATTERN.findall(block_text):
        version = version.strip()
        heures = re.findall(r"\d{1,2}:\d{2}", times_blob)
        for heure in heures:
            seances.append(Seance(date=date_str.strip(), heure=heure, version=version))
    return seances


def _extract_film_block(heading, next_heading_text: Optional[str]) -> Optional[Film]:
    """
    Construit un Film à partir du <a> de titre et du texte du bloc qui suit,
    jusqu'au prochain titre de film (ou fin de page).
    """
    anchor = heading.find("a", href=_FILM_HEADING_PATTERN)
    if anchor is None:
        return None

    titre = anchor.get_text(strip=True)
    href = anchor.get("href", "")
    if not titre or not href:
        return None
    url_fiche = urljoin(ALLOCINE_BASE_URL, href)

    # Récupère le texte du bloc : tout ce qui suit ce titre dans le document,
    # jusqu'au texte du titre suivant (ou toute la fin si c'est le dernier film).
    block_parts = []
    for sibling in heading.find_all_next(string=True):
        text = str(sibling)
        if next_heading_text and text.strip() == next_heading_text:
            break
        block_parts.append(text)
        if len(block_parts) > 400:  # garde-fou
            break
    block_text = "\n".join(p.strip() for p in block_parts if p.strip())

    genre_match = _DATE_GENRE_PATTERN.search(block_text)
    genres = []
    if genre_match:
        genres = [g.strip() for g in genre_match.group(2).split(",") if g.strip()]

    presse_match = _NOTE_PRESSE_PATTERN.search(block_text)
    spect_match = _NOTE_SPECT_PATTERN.search(block_text)
    note_presse = _parse_note(presse_match.group(1)) if presse_match else None
    note_spectateurs = _parse_note(spect_match.group(1)) if spect_match else None

    # Synopsis approximatif : première phrase longue après les notes
    synopsis = None
    for line in block_text.split("\n"):
        if len(line) > 60 and "Réserver" not in line and "Choisissez" not in line:
            synopsis = line
            break

    seances = _extract_seances(block_text)

    allocine = NoteAlloCine(
        note_presse=note_presse,
        note_spectateurs=note_spectateurs,
        url_fiche=url_fiche,
        trouve=(note_presse is not None or note_spectateurs is not None),
    )

    return Film(
        titre=titre,
        genres=genres,
        synopsis=synopsis,
        seances=seances,
        allocine=allocine,
    )


def scrape_allocine_theater() -> List[Film]:
    """
    Récupère les films à l'affiche, leurs séances et leurs notes AlloCiné
    pour le Pathé Toulouse Wilson, depuis la page salle AlloCiné dédiée.
    """
    logger.info("Récupération de la page salle AlloCiné: %s", ALLOCINE_SALLE_URL)
    html = _fetch_html(ALLOCINE_SALLE_URL)
    _save_debug_html(html, "allocine_salle_pathe_toulouse_wilson")

    soup = BeautifulSoup(html, "lxml")

    # Titres de films : liens vers une fiche film, situés dans un titre (h2/h3)
    film_headings = []
    for heading_tag in soup.find_all(["h2", "h3"]):
        anchor = heading_tag.find("a", href=_FILM_HEADING_PATTERN)
        if anchor:
            film_headings.append(heading_tag)

    if not film_headings:
        logger.warning(
            "Aucun film détecté sur la page salle AlloCiné. La structure de "
            "la page a peut-être changé. Consultez le HTML de debug dans %s.",
            DEBUG_DATA_DIR,
        )
        return []

    films: List[Film] = []
    for i, heading in enumerate(film_headings):
        next_text = None
        if i + 1 < len(film_headings):
            next_anchor = film_headings[i + 1].find("a", href=_FILM_HEADING_PATTERN)
            if next_anchor:
                next_text = next_anchor.get_text(strip=True)
        film = _extract_film_block(heading, next_text)
        if film:
            films.append(film)

    logger.info("%d films extraits depuis la page salle AlloCiné", len(films))
    return films
