"""
Scraper pour la page du cinéma Pathé Toulouse Wilson.

Stratégie à deux niveaux :
1. Les sites Pathé sont généralement construits en Next.js/React et embarquent
   souvent leurs données dans un <script id="__NEXT_DATA__"> ou un <script>
   contenant un objet JSON (window.__INITIAL_STATE__, etc.). On tente d'abord
   d'extraire ces données structurées, plus fiables qu'un parsing HTML.
2. En repli, on parse le HTML rendu à la recherche de "cartes films" classiques
   (titre, image, horaires).

Si la structure du site a changé et qu'aucune des deux stratégies ne fonctionne,
activez DEBUG_SAVE_HTML=true pour inspecter le HTML brut sauvegardé dans /app/data
et ajuster les sélecteurs ci-dessous en conséquence.
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import List

import requests
from bs4 import BeautifulSoup

from app.config import (
    PATHE_CINEMA_URL,
    PATHE_BASE_URL,
    DEFAULT_HEADERS,
    REQUEST_TIMEOUT,
    DEBUG_SAVE_HTML,
    DEBUG_DATA_DIR,
)
from app.models import Film, Seance

logger = logging.getLogger("pathe_scraper")

# pathe.fr est protégé par Akamai, qui détecte notamment les bots via
# l'empreinte TLS (JA3) de la connexion — un signal que la librairie
# `requests` standard ne peut pas imiter (elle utilise le TLS natif de
# Python, facilement reconnaissable). curl_cffi imite l'empreinte TLS d'un
# vrai navigateur Chrome, ce qui suffit souvent à passer ce type de
# protection. Si curl_cffi n'est pas disponible (échec d'installation sur
# une plateforme non supportée), on retombe sur `requests` classique.
try:
    from curl_cffi import requests as cf_requests
    _HAS_CURL_CFFI = True
except ImportError:
    cf_requests = None
    _HAS_CURL_CFFI = False

_IMPERSONATE_PROFILE = "chrome124"

# Session réutilisée pour conserver les cookies entre la page d'accueil
# (warm-up) et la page cible, comme le ferait un vrai navigateur.
if _HAS_CURL_CFFI:
    logger.info("curl_cffi disponible : les requêtes Pathé imiteront l'empreinte TLS de Chrome")
    _session = cf_requests.Session(impersonate=_IMPERSONATE_PROFILE)
else:
    logger.warning(
        "curl_cffi non disponible : repli sur `requests` standard, plus susceptible "
        "d'être bloqué par la protection Akamai de pathe.fr."
    )
    _session = requests.Session()
_session.headers.update(DEFAULT_HEADERS)


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


def _warm_up() -> None:
    """
    Visite la page d'accueil pathe.fr avant la page cible, pour obtenir des
    cookies de session comme le ferait un vrai navigateur. Certaines
    protections anti-bot basiques (pas les challenges JS type Cloudflare)
    bloquent les requêtes "à froid" sans cookies ni referer.
    """
    try:
        resp = _session.get(PATHE_BASE_URL, timeout=REQUEST_TIMEOUT)
        logger.info("Warm-up sur %s: statut %s, %d cookie(s) obtenu(s)",
                     PATHE_BASE_URL, resp.status_code, len(_session.cookies))
    except Exception as e:
        # Exception générique volontaire : curl_cffi lève ses propres classes
        # d'erreur, pas toujours des sous-classes de requests.RequestException.
        logger.warning("Échec du warm-up sur %s (on continue quand même): %s", PATHE_BASE_URL, e)


def _fetch_html(url: str, referer: str | None = None) -> str:
    headers = {"Referer": referer} if referer else {}
    resp = _session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        # On sauvegarde quand même le corps de la réponse d'erreur : utile pour
        # distinguer un simple refus (403 minimal) d'une page de challenge
        # Cloudflare/Akamai (souvent bien plus longue, avec du JS spécifique).
        _save_debug_html(
            resp.text, f"erreur_{resp.status_code}_{url.rsplit('/', 1)[-1] or 'page'}"
        )
        logger.warning(
            "Réponse HTTP %s pour %s (taille du corps: %d octets) — voir le HTML de "
            "debug pour identifier s'il s'agit d'une page de challenge anti-bot.",
            resp.status_code, url, len(resp.text),
        )
    resp.raise_for_status()
    return resp.text


def _try_extract_next_data(soup: BeautifulSoup) -> dict | None:
    """Tente d'extraire le JSON embarqué par Next.js (__NEXT_DATA__)."""
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None
    try:
        return json.loads(script.string)
    except json.JSONDecodeError:
        logger.warning("__NEXT_DATA__ trouvé mais JSON invalide")
        return None


def _films_from_next_data(data: dict) -> List[Film]:
    """
    Parcourt récursivement le JSON Next.js à la recherche d'objets ressemblant
    à des films (titre + séances). La structure exacte varie selon les sites
    Pathé ; on cherche donc de façon heuristique plutôt qu'avec un chemin fixe.
    """
    films: List[Film] = []
    seen_titles = set()

    def looks_like_film(obj: dict) -> bool:
        keys = {k.lower() for k in obj.keys()}
        title_keys = {"title", "titre", "name", "originaltitle"}
        return bool(keys & title_keys)

    def extract_title(obj: dict) -> str | None:
        for k in ("title", "titre", "name", "originalTitle"):
            if obj.get(k):
                return str(obj[k])
        return None

    def extract_seances(obj: dict) -> List[Seance]:
        seances = []
        for k in ("showtimes", "seances", "sessions", "screenings"):
            val = obj.get(k)
            if isinstance(val, list):
                for s in val:
                    if isinstance(s, dict):
                        seances.append(Seance(
                            date=str(s.get("date") or s.get("day") or "") or None,
                            heure=str(s.get("time") or s.get("heure") or "") or None,
                            version=s.get("version") or s.get("language"),
                            salle=s.get("room") or s.get("salle"),
                        ))
        return seances

    def walk(node):
        if isinstance(node, dict):
            if looks_like_film(node):
                title = extract_title(node)
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    films.append(Film(
                        titre=title,
                        affiche_url=node.get("poster") or node.get("image") or node.get("posterUrl"),
                        synopsis=node.get("synopsis") or node.get("description"),
                        duree=str(node.get("duration") or node.get("duree") or "") or None,
                        genres=node.get("genres") or [],
                        seances=extract_seances(node),
                    ))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return films


def _films_from_html_fallback(soup: BeautifulSoup) -> List[Film]:
    """
    Repli : parsing HTML classique. Cible des cartes de films avec des classes
    usuelles ("movie", "film", "product-movie", data-testid, etc.).
    À ADAPTER si le site utilise d'autres noms de classes — inspectez le HTML
    sauvegardé en debug pour identifier les bons sélecteurs.
    """
    films: List[Film] = []

    candidates = soup.select(
        "[class*='movie-card'], [class*='film-card'], [class*='product-movie'], "
        "[data-testid*='movie'], article[class*='movie'], li[class*='movie']"
    )

    for card in candidates:
        title_el = card.select_one("h2, h3, [class*='title']")
        if not title_el:
            continue
        titre = title_el.get_text(strip=True)
        if not titre:
            continue

        img_el = card.select_one("img")
        affiche_url = None
        if img_el:
            affiche_url = img_el.get("src") or img_el.get("data-src")

        seances = []
        for time_el in card.select("[class*='hour'], [class*='time'], [class*='seance'], time"):
            heure = time_el.get_text(strip=True)
            if heure:
                seances.append(Seance(heure=heure))

        films.append(Film(titre=titre, affiche_url=affiche_url, seances=seances))

    return films


def scrape_pathe_toulouse_wilson() -> List[Film]:
    """
    Récupère la liste des films actuellement à l'affiche au Pathé Toulouse
    Wilson, avec leurs séances si disponibles.
    """
    logger.info("Récupération de la page Pathé Toulouse Wilson: %s", PATHE_CINEMA_URL)
    _warm_up()
    html = _fetch_html(PATHE_CINEMA_URL, referer=PATHE_BASE_URL)
    _save_debug_html(html, "pathe_toulouse_wilson")

    soup = BeautifulSoup(html, "lxml")

    next_data = _try_extract_next_data(soup)
    if next_data:
        films = _films_from_next_data(next_data)
        if films:
            logger.info("%d films extraits via __NEXT_DATA__", len(films))
            return films
        logger.warning("__NEXT_DATA__ présent mais aucun film détecté, repli sur le HTML")

    films = _films_from_html_fallback(soup)
    logger.info("%d films extraits via parsing HTML de repli", len(films))
    if not films:
        logger.warning(
            "Aucun film détecté. Le site a peut-être changé de structure, "
            "ou nécessite un rendu JavaScript complet (envisager Playwright). "
            "Consultez le HTML de debug dans %s.", DEBUG_DATA_DIR
        )
    return films
