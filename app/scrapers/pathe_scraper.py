"""
Scraper pour la page du cinéma Pathé Toulouse Wilson.

Récupération de la page (3 niveaux, du plus léger au plus robuste) :
1. Playwright (navigateur headless Chromium) — méthode principale. pathe.fr
   est protégé par Akamai Bot Manager, qui exige l'exécution réelle de
   JavaScript (télémétrie de session via Boomerang/mPulse) avant de laisser
   passer une requête : aucun client HTTP, même avec une empreinte TLS
   parfaitement imitée, ne peut satisfaire cette exigence. Un vrai navigateur
   headless est donc nécessaire ici (vérifié empiriquement : curl_cffi seul
   ne suffit pas contre cette protection).
2. curl_cffi (empreinte TLS de Chrome) — repli si Playwright/Chromium n'est
   pas disponible sur la plateforme (ex. installation échouée).
3. requests classique — dernier repli si curl_cffi n'est pas non plus
   disponible.

Extraction des données (une fois le HTML obtenu, quelle que soit la méthode) :
1. Les sites Pathé sont généralement construits en Next.js/React et embarquent
   souvent leurs données dans un <script id="__NEXT_DATA__">. On tente
   d'abord d'extraire ces données structurées, plus fiables qu'un parsing HTML.
2. En repli, on parse le HTML rendu à la recherche de "cartes films" classiques
   (titre, image, horaires).

Si la structure du site a changé et qu'aucune des deux stratégies d'extraction
ne fonctionne, activez DEBUG_SAVE_HTML=true pour inspecter le HTML brut
sauvegardé dans /app/data et ajuster les sélecteurs ci-dessous en conséquence.
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

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    _HAS_PLAYWRIGHT = True
except ImportError:
    sync_playwright = None
    PlaywrightTimeoutError = Exception
    _HAS_PLAYWRIGHT = False

try:
    from curl_cffi import requests as cf_requests
    _HAS_CURL_CFFI = True
except ImportError:
    cf_requests = None
    _HAS_CURL_CFFI = False

_IMPERSONATE_PROFILE = "chrome124"
_PLAYWRIGHT_TIMEOUT_MS = REQUEST_TIMEOUT * 1000 * 2  # le rendu JS est plus lent qu'une requête HTTP

# Session HTTP de repli (curl_cffi si possible, sinon requests), utilisée
# uniquement si Playwright n'est pas disponible.
if _HAS_CURL_CFFI:
    _session = cf_requests.Session(impersonate=_IMPERSONATE_PROFILE)
else:
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


def _fetch_html_playwright(url: str) -> str:
    """Charge la page dans un vrai Chromium headless (exécute le JS Akamai)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=DEFAULT_HEADERS["User-Agent"],
                locale="fr-FR",
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=_PLAYWRIGHT_TIMEOUT_MS)
            # Laisse le temps au JS Akamai / à d'éventuels appels API internes
            # de charger les films après le rendu initial de la page.
            page.wait_for_timeout(2000)
            html = page.content()
            return html
        finally:
            browser.close()


def _fetch_html_http(url: str, referer: str | None = None) -> str:
    """Repli sans navigateur (curl_cffi ou requests)."""
    headers = {"Referer": referer} if referer else {}
    resp = _session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        _save_debug_html(
            resp.text, f"erreur_{resp.status_code}_{url.rsplit('/', 1)[-1] or 'page'}"
        )
        logger.warning(
            "Réponse HTTP %s pour %s (taille du corps: %d octets)",
            resp.status_code, url, len(resp.text),
        )
    resp.raise_for_status()
    return resp.text


def _fetch_html(url: str, referer: str | None = None) -> str:
    """
    Récupère le HTML d'une page Pathé. Utilise Playwright en priorité
    (nécessaire pour passer la protection Akamai Bot Manager), avec repli
    automatique sur une requête HTTP classique si Playwright échoue ou n'est
    pas disponible.
    """
    if _HAS_PLAYWRIGHT:
        try:
            logger.info("Récupération de %s via Playwright (Chromium headless)", url)
            return _fetch_html_playwright(url)
        except PlaywrightTimeoutError as e:
            logger.warning(
                "Timeout Playwright sur %s (%s) — repli sur une requête HTTP classique", url, e
            )
        except Exception as e:
            logger.warning(
                "Échec Playwright sur %s (%s) — repli sur une requête HTTP classique", url, e
            )
    else:
        logger.warning(
            "Playwright non disponible : utilisation d'une requête HTTP classique "
            "(risque élevé de blocage par la protection Akamai de pathe.fr)."
        )

    return _fetch_html_http(url, referer=referer)


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
