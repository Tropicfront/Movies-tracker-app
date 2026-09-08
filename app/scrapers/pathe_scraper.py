"""
Scraper pour la page du cinéma Pathé Toulouse Wilson.

Récupération de la page : appel direct au binaire `curl` (via subprocess),
et non à une librairie HTTP Python (`requests`, `curl_cffi`) ni à un
navigateur headless (Playwright). `curl` reste utilisé par précaution (voir
historique ci-dessous), même si la cause du blocage rencontré s'est avérée
différente de ce qui était initialement suspecté.

Historique du diagnostic (pathe.fr est derrière Akamai) :
- Étape 1 : `curl` passait, `requests` Python et Playwright étaient bloqués
  (403) avec des en-têtes strictement identiques → suspicion d'empreinte
  TLS/HTTP2. `curl` a donc été adopté comme méthode de récupération.
- Étape 2 : le blocage a persisté même avec `curl` en subprocess. Un test
  verbeux (`curl -v`) a révélé la vraie cause : la réponse 403 était servie
  **depuis le cache d'Akamai** pour cette URL précise (en-têtes
  `server-timing: cdn-cache; desc=HIT` et `cache-control: max-age=120`) —
  probablement une erreur mise en cache lors des nombreux tests répétés
  pendant le débogage, indépendamment des en-têtes/IP/TLS envoyés depuis.
- Solution retenue : un paramètre de requête unique (`?_cb=<timestamp>`) à
  chaque appel, pour forcer Akamai à traiter chaque requête comme une
  nouvelle URL et contourner ce cache figé (voir `scrape_pathe_toulouse_wilson`).

Extraction des données (une fois le HTML obtenu) :
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
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from typing import List

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

_STATUS_MARKER = "__PATHE_SCRAPER_HTTP_STATUS__"


class PatheFetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


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


def _fetch_html(url: str, referer: str | None = None) -> str:
    """
    Récupère le HTML d'une page via le binaire `curl` en subprocess (voir
    l'explication en tête de fichier sur pourquoi ce n'est PAS une requête
    Python classique).
    """
    if shutil.which("curl") is None:
        raise PatheFetchError(
            "Le binaire `curl` n'est pas installé dans ce conteneur. "
            "Vérifiez que le Dockerfile installe bien `curl` (apt-get install curl)."
        )

    cmd = [
        "curl", "-s", "-L", "-v",
        "-4",  # force IPv4 (précaution ; la vraie cause du blocage observé
               # était en réalité un 403 mis en cache par Akamai pour cette
               # URL, voir le cache-busting dans scrape_pathe_toulouse_wilson)
        "--max-time", str(REQUEST_TIMEOUT),
        "-H", f"User-Agent: {DEFAULT_HEADERS['User-Agent']}",
        "-H", f"Accept-Language: {DEFAULT_HEADERS['Accept-Language']}",
        "-H", "Cache-Control: no-cache",
        "-H", "Pragma: no-cache",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    # -w ajoute le code HTTP à la fin de la sortie, précédé d'un marqueur
    # unique, pour pouvoir le séparer du corps de la réponse.
    cmd += ["-w", f"\n{_STATUS_MARKER}%{{http_code}}", url]

    # Log de la commande exacte, copiable-collable telle quelle, pour pouvoir
    # la reproduire manuellement à l'identique en cas de désaccord entre le
    # comportement de l'app et un test manuel.
    logger.info("Commande curl exécutée : %s", " ".join(shlex.quote(c) for c in cmd))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=REQUEST_TIMEOUT + 5, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise PatheFetchError(f"curl a dépassé le délai imparti pour {url}: {e}") from e

    # Avec -v, curl écrit le détail de la connexion (IP contactée, poignée de
    # main TLS, en-têtes envoyés/reçus) sur stderr. Précieux pour diagnostiquer
    # un écart entre le comportement de l'app et un test manuel.
    if result.stderr:
        logger.info("Détail curl (-v) pour %s :\n%s", url, result.stderr)

    if result.returncode != 0:
        raise PatheFetchError(
            f"curl a échoué (code {result.returncode}) pour {url}: {result.stderr.strip()}"
        )

    output = result.stdout
    body, _, status_str = output.rpartition(_STATUS_MARKER)
    try:
        status_code = int(status_str.strip())
    except ValueError:
        status_code = None

    if status_code is None or status_code >= 400:
        url_slug = url.rsplit('/', 1)[-1].split('?')[0] or 'page'
        _save_debug_html(body, f"erreur_{status_code}_{url_slug}")
        raise PatheFetchError(
            f"Statut HTTP {status_code} pour {url}", status_code=status_code, body=body
        )

    return body


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
    # Contournement de cache : un test verbeux (curl -v) a montré que le 403
    # rencontré était servi DEPUIS LE CACHE d'Akamai (server-timing:
    # cdn-cache; desc=HIT, cache-control: max-age=120), probablement une
    # ancienne erreur mise en cache lors des tests précédents. Un paramètre
    # de requête unique à chaque appel force Akamai à traiter la requête
    # comme une nouvelle URL, contournant ce cache figé.
    cache_bust_url = f"{PATHE_CINEMA_URL}?_cb={int(time.time())}"
    logger.info("Récupération de la page Pathé Toulouse Wilson: %s", cache_bust_url)
    html = _fetch_html(cache_bust_url, referer=PATHE_BASE_URL)
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
            "Aucun film détecté. Le site a peut-être changé de structure. "
            "Consultez le HTML de debug dans %s.", DEBUG_DATA_DIR
        )
    return films
