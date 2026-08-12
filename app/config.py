"""
Configuration centralisée de l'application.
Modifiez ici les URLs si les sites changent de structure d'adresse.
"""
import os

# --- Pathé Toulouse Wilson ---
# Slug de la page cinéma sur pathe.fr. À vérifier/ajuster si le site change.
PATHE_CINEMA_SLUG = os.getenv("PATHE_CINEMA_SLUG", "pathe-toulouse-wilson")
PATHE_BASE_URL = "https://www.pathe.fr"
PATHE_CINEMA_URL = f"{PATHE_BASE_URL}/cinemas/{PATHE_CINEMA_SLUG}"

# --- AlloCiné ---
ALLOCINE_BASE_URL = "https://www.allocine.fr"
ALLOCINE_SEARCH_URL = f"{ALLOCINE_BASE_URL}/recherche/1/"

# --- HTTP ---
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
REQUEST_TIMEOUT = 15  # secondes

# --- Debug ---
# Si activé, sauvegarde le HTML brut récupéré dans /app/data pour permettre
# d'ajuster les sélecteurs CSS en cas de changement de structure des sites.
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

def jellyfin_configured() -> bool:
    return bool(JELLYFIN_URL and JELLYFIN_API_KEY)

# --- Calendrier (ICS) ---
CALENDAR_NAME = os.getenv("CALENDAR_NAME", "Pathé Toulouse Wilson (dans ma bibliothèque Jellyfin)")

# --- Correspondance de titres (matching Pathé <-> Jellyfin) ---
# Fichier JSON éditable à chaud (monté en volume) pour déclarer des alias de
# titres quand le titre français (Pathé/AlloCiné) diffère du titre Jellyfin
# (souvent en anglais). Exemple de contenu :
# {"Dune : Deuxième Partie": "Dune: Part Two"}
TITLE_ALIASES_PATH = os.getenv("TITLE_ALIASES_PATH", "/app/data/title_aliases.json")
# Seuil de similarité (0-1) pour accepter un rapprochement approximatif de titres
TITLE_MATCH_THRESHOLD = float(os.getenv("TITLE_MATCH_THRESHOLD", "0.85"))
