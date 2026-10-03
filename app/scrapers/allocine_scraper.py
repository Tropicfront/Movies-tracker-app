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
from app.matching import normalize_title, extract_year_from_title
from app.ratelimit import wait_for_slot
from app import allocine_cache
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


def _classer_candidats(pool: list, titre: str, annee: Optional[int]) -> list:
    """
    Classe les résultats de recherche, du plus au moins plausible, et retourne
    [(rang, candidat), ...] (tri stable : à rang égal, l'ordre d'AlloCiné est gardé).

    Un candidat = (chemin, titre affiché, années lues sur la carte). Deux critères :
    - le TITRE est « identique » (similarité >= 0,9 après normalisation) ou non ;
    - l'ANNÉE concorde (une année de la carte à ±1 de `annee`), est inconnue (carte
      sans année lisible, ou aucune année demandée), ou diffère.

    Rangs (0 = meilleur) :
      0  titre identique, année concorde
      1  titre identique, année inconnue
      2  titre différent, année concorde
      3  titre identique, année DIFFÉRENTE
      4  titre différent, année inconnue
      5  titre différent, année différente

    Pourquoi un classement plutôt qu'un filtre : les années lues sur les cartes
    sont des dates de SORTIE EN FRANCE (et de ressortie), pas l'année de
    production. « Akira » (1988) est sorti en France en 1991 et ressorti en 2026 :
    écarter tout candidat dont l'année diffère éliminait le vrai film et ne
    laissait que « Akira (Live Action) ». Un titre identique à année différente
    (rang 3) reste donc préférable à un titre approximatif sans année (rang 4),
    mais moins bon qu'un titre différent dont l'année concorde (rang 2) : c'est ce
    qui départage « Dune » (1984) de « Dune : Première partie » (2021) pour 2021,
    et les deux « Apocalypse Now » (1979 / 1998).
    """
    wanted = normalize_title(titre)

    def rang(c) -> int:
        exact = SequenceMatcher(None, wanted, normalize_title(c[1])).ratio() >= 0.9
        if annee and c[2]:
            etat = "concorde" if any(abs(y - annee) <= 1 for y in c[2]) else "differe"
        else:
            etat = "inconnue"
        return {
            (True, "concorde"): 0, (True, "inconnue"): 1, (False, "concorde"): 2,
            (True, "differe"): 3, (False, "inconnue"): 4, (False, "differe"): 5,
        }[(exact, etat)]

    return sorted(((rang(c), c) for c in pool), key=lambda rc: rc[0])


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
      Now" 1979 de Coppola et un film de 1998). Les candidats sont CLASSÉS, pas
      filtrés, selon le titre (identique ou non) et l'année (voir
      _classer_candidats) : l'année de Jellyfin départage les homonymes, mais un
      titre identique n'est jamais éliminé à cause d'une année différente, car
      les années des cartes AlloCiné sont des dates de sortie en France.
    - À rang égal, on garde le premier résultat (le classement AlloCiné est
      généralement bon, y compris quand le titre Jellyfin est en anglais).
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

    classes = _classer_candidats(pool, titre, annee)  # [(rang, candidat)] du meilleur au moins bon
    rang, chosen = classes[0]

    year_note = ""
    if annee and rang in (0, 2) and any(r in (3, 5) for r, _ in classes):
        year_note = " [homonymes d'une autre année écartés]"
    elif rang == 3:
        year_note = (
            f" [titre identique mais année AlloCiné différente ({', '.join(map(str, chosen[2]))} ≠ {annee}) : "
            "retenu — AlloCiné affiche des dates de sortie en France, pas l'année de production]"
        )

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


def _resolve_query(titre: str, annee: Optional[int]) -> tuple:
    """
    Résout le couple (titre de recherche, année effective) UNIQUE utilisé par
    tout ce qui lit ou écrit le cache AlloCiné : si le titre se termine par
    une année entre parenthèses ("Macross (1982)"), elle est extraite et la
    parenthèse retirée ; l'année fournie explicitement (ex. ProductionYear
    Jellyfin) reste prioritaire sur celle du titre.

    Centralisé ici pour que l'écriture (get_note_allocine) et toutes les
    lectures (get_cached_note_allocine) construisent EXACTEMENT la même clé :
    une lecture qui utiliserait le titre brut ne retrouverait jamais l'entrée
    écrite sous le titre nettoyé.
    """
    titre_recherche, annee_du_titre = extract_year_from_title(titre)
    if annee_du_titre is not None and annee is not None and annee_du_titre != annee:
        logger.debug(
            "Année du titre (%d) et année fournie (%d) diffèrent pour '%s' : l'année fournie "
            "est prioritaire (généralement plus fiable, ex. ProductionYear Jellyfin).",
            annee_du_titre, annee, titre,
        )
    return titre_recherche, (annee if annee is not None else annee_du_titre)


def get_cached_note_allocine(
    titre: str, type_: str = "film", annee: Optional[int] = None
) -> Optional[NoteAlloCine]:
    """
    Lecture PURE du cache AlloCiné pour un titre Jellyfin (jamais de requête
    réseau) : retourne la note en cache si elle existe et est valide, sinon
    None. À utiliser pour tout ce qui ne doit pas déclencher de recherche
    (application du cache vers Jellyfin, page bibliothèque...), à la place de
    allocine_cache.get_cached(), qui exige un titre déjà nettoyé.
    """
    titre_recherche, annee_effective = _resolve_query(titre, annee)
    return allocine_cache.get_cached(titre_recherche, type_, annee_effective)


def get_note_allocine(
    titre: str, type_: str = "film", annee: Optional[int] = None, force_refresh: bool = False
) -> NoteAlloCine:
    """
    Récupère les notes AlloCiné (presse et spectateurs) pour un film ou une
    série, à partir de son titre.

    Seuls les résultats TROUVÉS sont mis en cache sur disque
    (ALLOCINE_CACHE_PATH, valable ALLOCINE_CACHE_TTL_DAYS jours) : un titre
    déjà résolu lors d'une synchro précédente ne redéclenche aucune requête
    tant que le cache est valide ; un titre non trouvé est toujours
    recherché à nouveau.

    Si le titre se termine par une année entre parenthèses (convention
    fréquente côté Jellyfin pour distinguer des homonymes, ex.
    "Macross (1982)"), elle est extraite : la recherche AlloCiné utilise le
    titre sans la parenthèse (la parenthèse gênerait la recherche en texte
    libre), et cette année sert de la même façon que le paramètre `annee`
    pour départager les homonymes (voir _find_fiche_url). Si `annee` est
    fourni explicitement ET qu'une année est aussi trouvée dans le titre, le
    paramètre explicite est prioritaire (plus probablement fiable :
    généralement `ProductionYear` depuis Jellyfin).

    :param titre: Titre du film ou de la série, avec ou sans année entre parenthèses
    :param type_: "film" ou "serie"
    :param annee: année de production (ex. depuis Jellyfin) pour départager les homonymes
    :param force_refresh: ignore le cache et refait la recherche, même si une
        entrée valide existe (utile pour un titre précis via /allocine/note ;
        pour toute la bibliothèque, voir POST /jellyfin/sync-notes?force_refresh=true)
    """
    titre, annee_effective = _resolve_query(titre, annee)

    if not force_refresh:
        cached = allocine_cache.get_cached(titre, type_, annee_effective)
        if cached is not None:
            logger.debug("Cache AlloCiné : résultat réutilisé pour '%s' (%s)", titre, type_)
            return cached

    note = _fetch_note_allocine(titre, type_, annee_effective)
    allocine_cache.set_cached(titre, type_, note, annee_effective)
    return note


def _fetch_note_allocine(titre: str, type_: str, annee: Optional[int]) -> NoteAlloCine:
    """Récupération effective (sans cache) : logique inchangée par rapport à
    avant l'ajout du cache, simplement extraite dans sa propre fonction."""
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
