"""
Configuration centralisée de l'application.
Modifiez ici les URLs si les sites changent de structure d'adresse.
"""
import logging
import os

# --- AlloCiné ---

def _env_float(name: str, default: float) -> float:
    """
    Lit un nombre décimal dans l'environnement. Une valeur absente, vide,
    non numérique ou négative retombe sur la valeur par défaut (avec un
    avertissement) au lieu de faire planter le conteneur au démarrage.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        value = -1.0
    if value < 0:
        logging.getLogger("config").warning(
            "Valeur invalide pour %s=%r (attendu : nombre >= 0) : défaut %s utilisé.",
            name, raw, default,
        )
        return default
    return value

ALLOCINE_BASE_URL = "https://www.allocine.fr"
# Ancienne URL "/recherche/1/" retirée par AlloCiné (410 Gone confirmé le
# 27/09/2026, page d'erreur Apache standard). Nouvelle URL confirmée par un
# test manuel dans un vrai navigateur : "/rechercher/" (verbe, pas nom).
ALLOCINE_SEARCH_URL = f"{ALLOCINE_BASE_URL}/rechercher/"

# Code "salle" AlloCiné du Pathé Toulouse Wilson (visible dans l'URL de la
# page du cinéma : /seance/salle_gen_csalle=P0057.html). Cette page liste en
# une seule fois les films à l'affiche, leurs séances DU JOUR ET leurs notes
# AlloCiné (presse et spectateurs) — c'est la source unique utilisée par
# l'application (voir app/scrapers/allocine_theater_scraper.py). pathe.fr a
# été abandonné comme source : protégé par Akamai Bot Manager, qui a résisté
# à toutes les tentatives de contournement (en-têtes réalistes, curl_cffi,
# Playwright headless, cookies persistants, limitation du nombre de requêtes).
# Si ce code change un jour (renumérotation AlloCiné), ajustez-le ici.
ALLOCINE_SALLE_CODE = os.getenv("ALLOCINE_SALLE_CODE", "P0057")
ALLOCINE_SALLE_URL = f"{ALLOCINE_BASE_URL}/seance/salle_gen_csalle={ALLOCINE_SALLE_CODE}.html"

# --- HTTP ---
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
REQUEST_TIMEOUT = 15  # secondes

# --- Politesse envers AlloCiné (évite les 429 et les blocages) ---
# Nombre MAXIMAL de requêtes vers AlloCiné par seconde, toutes requêtes
# confondues (recherche, fiche, page salle). 1 = une requête par seconde.
# Accepte les décimales : 0.5 = une requête toutes les 2 s. 0 = pas de limite.
ALLOCINE_REQUESTS_PER_SECOND = _env_float("ALLOCINE_REQUESTS_PER_SECOND", 1.0)

# Pause supplémentaire (en secondes) entre chaque TITRE lors de la synchro
# Jellyfin, en plus de la limite ci-dessus (un titre = 2 requêtes : recherche
# + fiche). 0 = pas de pause supplémentaire.
ALLOCINE_SYNC_DELAY_SECONDS = _env_float("ALLOCINE_SYNC_DELAY_SECONDS", 2.0)

# --- Debug ---
# Si activé, sauvegarde le HTML brut récupéré dans /app/data pour permettre
# d'ajuster les sélecteurs CSS en cas de changement de structure du site.
DEBUG_SAVE_HTML = os.getenv("DEBUG_SAVE_HTML", "true").lower() == "true"
DEBUG_DATA_DIR = os.getenv("DEBUG_DATA_DIR", "/app/data")

# --- Jellyfin ---
# URL de votre serveur Jellyfin, sans slash final (ex: http://192.168.1.10:8096)
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
# Clé API générée dans Jellyfin: Dashboard > Clés API
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
# Optionnel : certains endpoints Jellyfin nécessitent un userId selon la version.
# Laissez vide pour utiliser les endpoints génériques (recommandé en premier essai).
JELLYFIN_USER_ID = os.getenv("JELLYFIN_USER_ID", "")

# Éléments de la bibliothèque Jellyfin à ignorer lors de la synchro des notes
# et du calendrier : les regroupements (ex. "Avengers - Saga", "300 - Saga")
# ne correspondent à aucune fiche AlloCiné précise. Expression régulière
# insensible à la casse appliquée au nom ; laisser vide pour désactiver.
JELLYFIN_IGNORE_REGEX = os.getenv("JELLYFIN_IGNORE_REGEX", r"\s[-–—]\s*Saga\s*$")

def jellyfin_configured() -> bool:
    return bool(JELLYFIN_URL and JELLYFIN_API_KEY)

# --- Calendrier (ICS) ---
CALENDAR_NAME = os.getenv("CALENDAR_NAME", "Pathé Toulouse Wilson (dans ma bibliothèque Jellyfin)")

# --- Correspondance de titres (matching AlloCiné <-> Jellyfin) ---
# Fichier JSON éditable à chaud (monté en volume) pour déclarer des alias de
# titres quand le titre français (AlloCiné) diffère du titre Jellyfin
# (souvent en anglais). Exemple de contenu :
# {"Dune : Deuxième Partie": "Dune: Part Two"}
TITLE_ALIASES_PATH = os.getenv("TITLE_ALIASES_PATH", "/app/data/title_aliases.json")
# Seuil de similarité (0-1) pour accepter un rapprochement approximatif de titres
TITLE_MATCH_THRESHOLD = float(os.getenv("TITLE_MATCH_THRESHOLD", "0.85"))
