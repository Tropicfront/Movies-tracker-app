"""
Scraper AlloCiné — récupère uniquement les notes (presse / spectateurs)
pour un film ou une série, à partir de son titre.

Fonctionnement :
1. Recherche du titre via la page de recherche AlloCiné (/rechercher/?q=...).
2. Récupération du premier résultat pertinent (lien vers une fiche film/série).
3. Ouverture de la fiche et extraction des notes presse/spectateurs.

Particularité constatée le 27/09/2026 : sur la page de résultats de recherche,
AlloCiné n'utilise plus de balises <a href="..."> classiques pour les liens
vers les fiches. Le lien est à la place encodé dans l'attribut class d'un
<span> (ex: class="ACrL2ZACrpbG0vZmljaGVmaWxtX2dlbl9jZmlsbT0xMDAwMDE4ODU0Lmh0bWw=
meta-title-link"). Décodage : retirer toutes les occurrences de "ACr" dans ce
token de classe, puis décoder le résultat en base64 → on obtient le chemin
relatif réel (ex: "/film/fichefilm_gen_cfilm=1000018854.html"). Voir
_decode_allocine_class_link() ci-dessous. Cette obfuscation ne concerne (pour
l'instant) que la page de recherche ; la page salle (allocine_theater_scraper.py)
utilise toujours des <a href> classiques.

Comme pour le reste du projet, si AlloCiné change encore sa structure,
activez DEBUG_SAVE_HTML=true pour inspecter le HTML brut sauvegardé.
"""
import base64
import logging
import os
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
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
from app.matching import normalize_title
from app.ratelimit import wait_for_slot
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


def _fetch_html(url: str, params: dict | None = None, debug_name: str = "erreur") -> str:
    """
    Requête GET avec gestion du 429 (Too Many Requests) : nouvelle tentative
    après une pause, en respectant l'en-tête Retry-After du serveur s'il est
    présent, sinon une pause par défaut. Utile notamment lors de la synchro
    Jellyfin, qui peut interroger AlloCiné pour de nombreux titres à la suite.

    En cas d'erreur HTTP (quel que soit le code, y compris 410 Gone), le
    corps de la réponse est sauvegardé en HTML de debug avant de lever
    l'exception, pour pouvoir diagnostiquer précisément la cause.
    """
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        wait_for_slot()  # limite globale de requêtes/seconde vers AlloCiné
        resp = requests.get(url, headers=DEFAULT_HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429 and attempt < max_retries:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 5.0
            except ValueError:
                wait = 5.0
            wait = min(max(wait, 1.0), 30.0)  # borné entre 1s et 30s
            logger.warning(
                "429 Too Many Requests pour %s (tentative %d/%d) — attente %.1fs avant réessai",
                url, attempt, max_retries, wait,
            )
            time.sleep(wait)
            continue
        if not resp.ok:
            _save_debug_html(resp.text, f"{debug_name}_{resp.status_code}")
            logger.warning(
                "Réponse HTTP %s pour %s (taille du corps: %d octets) — HTML de debug sauvegardé",
                resp.status_code, url, len(resp.text),
            )
        resp.raise_for_status()
        return resp.text


def _decode_allocine_class_link(class_value) -> Optional[str]:
    """
    Décode un lien encodé dans un attribut class à la AlloCiné (voir
    l'explication en tête de fichier). class_value peut être une liste
    (BeautifulSoup) ou une chaîne. Retourne le chemin relatif décodé
    (ex: "/film/fichefilm_gen_cfilm=1000018854.html") ou None si aucun des
    tokens de la classe ne se décode en un chemin plausible.
    """
    if not class_value:
        return None
    tokens = class_value if isinstance(class_value, list) else class_value.split()
    for token in tokens:
        cleaned = token.replace("ACr", "")
        if len(cleaned) < 8:  # trop court pour être notre encodage, on ignore
            continue
        padded = cleaned + "=" * (-len(cleaned) % 4)
        try:
            decoded = base64.b64decode(padded, validate=False).decode("utf-8")
        except Exception:
            continue
        if decoded.startswith("/"):
            return decoded
    return None


_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _years_in(text: str) -> list[int]:
    return [int(y) for y in _YEAR_RE.findall(text or "")]


def _card_years(el, candidate_ids: set) -> list[int]:
    """
    Années affichées sur la "carte" du résultat qui contient `el`.

    La carte = le plus grand ancêtre de `el` qui ne contient AUCUN autre
    résultat (sinon on mélangerait les années de résultats voisins). On lit
    d'abord le bloc d'infos (date, durée, genres) ; à défaut, tout le texte de
    la carte (le titre peut contenir l'année, ex. "(2012)").
    """
    def count_candidates(node) -> int:
        return sum(1 for x in node.find_all(True) if id(x) in candidate_ids)

    node = el
    for _ in range(8):
        parent = node.parent
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if count_candidates(parent) > 1:
            break
        node = parent

    info_text = " ".join(x.get_text(" ") for x in node.select(".meta-body-info, .meta-body"))
    years = _years_in(info_text)
    if not years:
        # Repli : texte de la carte SANS le synopsis (qui cite souvent d'autres
        # années : "en 1968, un astronaute...") pour ne pas fausser la lecture.
        text = node.get_text(" ")
        for syn in node.select(".synopsis"):
            text = text.replace(syn.get_text(" "), " ")
        years = _years_in(text)
    return sorted(set(years))


def _find_fiche_url(titre: str, type_: str = "film", annee: Optional[int] = None) -> Optional[str]:
    """
    Recherche le titre sur AlloCiné et retourne l'URL de la fiche la plus
    pertinente. Les liens de la page de résultats ne sont pas de simples
    <a href> mais encodés dans l'attribut class (voir l'explication en tête
    de fichier) — on décode donc chaque candidat plutôt que de lire un href.

    Garde-fous :
    - Les colonnes latérales (<aside>, ex. widget "Top films au box office"),
      l'en-tête et le pied de page sont ignorés : ils contiennent eux aussi des
      liens de fiches (présents sur TOUTES les pages de recherche) qui
      donneraient de fausses correspondances si aucun vrai résultat n'existe.
    - Le type demandé (film/série) est prioritaire.
    - Homonymes : plusieurs films portent souvent le même titre (ex. "Apocalypse
      Now" 1979 de Coppola et un film de 1998). Si `annee` (année de production
      dans Jellyfin) est fournie, on écarte les résultats dont l'année affichée
      est connue et éloignée de plus d'1 an. Un résultat dont l'année est
      illisible n'est JAMAIS écarté (les cartes d'animes, notamment, n'en ont
      souvent pas) ; si tous seraient écartés, l'année est ignorée.
    - Parmi les candidats restants, on garde le premier résultat (le classement
      AlloCiné est généralement bon, y compris quand le titre Jellyfin est en
      anglais), sauf si un autre candidat a un titre quasi identique.
    """
    html = _fetch_html(ALLOCINE_SEARCH_URL, params={"q": titre}, debug_name=f"recherche_{titre}")
    _save_debug_html(html, f"recherche_{titre}")
    soup = BeautifulSoup(html, "lxml")

    for zone in soup.select("aside, footer, header, nav"):
        zone.decompose()

    path_prefix = "/film/fichefilm" if type_ == "film" else "/series/ficheserie"

    found: list[tuple[str, str, object]] = []  # (chemin, titre affiché, élément)
    seen_paths: set[str] = set()
    for el in soup.select(".meta-title-link, .list-entity-title-link, a[href]"):
        # Repli inclus : si AlloCiné revient un jour à de vrais <a href>,
        # cette même boucle les capte aussi via le sélecteur "a[href]".
        path = el.get("href") or _decode_allocine_class_link(el.get("class"))
        if not path or path in seen_paths:
            continue
        if "fichefilm" not in path and "ficheserie" not in path:
            continue  # actus, vidéos, personnes, liens de service...
        seen_paths.add(path)
        found.append((path, el.get_text(strip=True), el))

    if not found:
        return None

    candidate_ids = {id(el) for _, _, el in found}
    # (chemin, titre affiché, années lues sur la carte)
    candidates = [(path, shown, _card_years(el, candidate_ids)) for path, shown, el in found]

    same_type = [c for c in candidates if path_prefix in c[0]]
    pool = same_type or candidates  # repli : n'importe quel type de fiche

    year_note = ""
    if annee:
        def mismatch(c) -> bool:  # année connue ET différente : c'est un homonyme
            return bool(c[2]) and not any(abs(y - annee) <= 1 for y in c[2])

        kept = [c for c in pool if not mismatch(c)]
        if not kept:
            year_note = " [aucun résultat de cette année : année ignorée]"
        elif len(kept) < len(pool):
            pool = kept
            year_note = " [homonymes d'une autre année écartés]"

    wanted = normalize_title(titre)
    chosen = pool[0]
    for cand in pool:
        if SequenceMatcher(None, wanted, normalize_title(cand[1])).ratio() >= 0.9:
            chosen = cand
            break

    logger.info(
        "Recherche %r%s -> %r %s parmi %d candidat(s)%s",
        titre, f" ({annee})" if annee else "", chosen[1], chosen[2] or "(année inconnue)",
        len(candidates), year_note,
    )

    link_path = chosen[0]
    if link_path.startswith("http"):
        return link_path
    return f"{ALLOCINE_BASE_URL}{link_path}"


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


def get_note_allocine(titre: str, type_: str = "film", annee: Optional[int] = None) -> NoteAlloCine:
    """
    Récupère les notes AlloCiné (presse et spectateurs) pour un film ou une
    série, à partir de son titre.

    :param titre: Titre du film ou de la série
    :param type_: "film" ou "serie"
    :param annee: année de production (ex. depuis Jellyfin) pour départager les homonymes
    """
    try:
        fiche_url = _find_fiche_url(titre, type_, annee)
        if not fiche_url:
            logger.warning("Aucune fiche AlloCiné trouvée pour '%s'", titre)
            return NoteAlloCine(trouve=False)

        html = _fetch_html(fiche_url, debug_name=f"fiche_{titre}")
        _save_debug_html(html, f"fiche_{titre}")
        soup = BeautifulSoup(html, "lxml")

        note_presse, note_spectateurs = _extract_notes(soup)

        if note_presse is None and note_spectateurs is None:
            logger.warning(
                "Fiche trouvée pour '%s' (%s) mais aucune note extraite : soit AlloCiné n'en "
                "publie pas encore pour ce titre (fréquent pour les titres confidentiels), soit la "
                "structure de la fiche a changé (voir le fichier allocine_fiche_* dans /app/data).",
                titre, fiche_url,
            )
        else:
            logger.info(
                "Notes AlloCiné pour '%s' : presse=%s, spectateurs=%s (%s)",
                titre, note_presse, note_spectateurs, fiche_url,
            )

        return NoteAlloCine(
            note_presse=note_presse,
            note_spectateurs=note_spectateurs,
            url_fiche=fiche_url,
            trouve=(note_presse is not None or note_spectateurs is not None),
        )
    except requests.RequestException as e:
        logger.error("Erreur réseau lors de la récupération de la note pour '%s': %s", titre, e)
        return NoteAlloCine(trouve=False)
