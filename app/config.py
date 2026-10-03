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

# --- Cache des résultats AlloCiné (recherche + note) ---
# Évite de refaire une recherche + une fiche pour un titre déjà résolu lors
# d'une synchro précédente. Stocké dans le volume de données (persiste entre
# redémarrages/reconstructions). Voir app/allocine_cache.py.
ALLOCINE_CACHE_PATH = os.getenv("ALLOCINE_CACHE_PATH", "/app/data/allocine_cache.json")
# Durée de validité d'une entrée, en jours (accepte les décimales). Passé ce
# délai, le titre est de nouveau recherché sur AlloCiné (utile si une note
# était absente lors du premier passage, ou a changé). <= 0 désactive le
# cache : chaque titre est toujours recherché à nouveau.
ALLOCINE_CACHE_TTL_DAYS = _env_float("ALLOCINE_CACHE_TTL_DAYS", 30.0)

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

# Écrire les notes AlloCiné dans les cases NATIVES de Jellyfin : l'étoile (CommunityRating,
# note communautaire /10) et la tomate (CriticRating, note critique /100).
# DÉSACTIVÉ par défaut : ces deux cases sont celles des notes communautaires et Rotten
# Tomatoes ; y mettre des notes AlloCiné (converties) les fausse. Les notes sont alors
# seulement gardées dans le cache de l'application, et affichées par le badge AlloCiné
# (icônes journal / personne) injecté dans l'interface web de Jellyfin.
# Mettre "true" pour retrouver l'ancien comportement (étoile = spectateurs x2, tomate = presse x20).
JELLYFIN_WRITE_RATINGS = os.getenv("JELLYFIN_WRITE_RATINGS", "false").strip().lower() in ("1", "true", "yes", "oui", "on")

# Éléments de la bibliothèque Jellyfin à ignorer lors de la synchro des notes
# et du calendrier : les regroupements (ex. "Avengers - Saga", "300 - Saga")
# ne correspondent à aucune fiche AlloCiné précise. Expression régulière
# insensible à la casse appliquée au nom ; laisser vide pour désactiver.
JELLYFIN_IGNORE_REGEX = os.getenv("JELLYFIN_IGNORE_REGEX", r"\s[-–—]\s*Saga\s*$")

# --- Cache disque des affiches Jellyfin ---
# Les affiches récupérées via /jellyfin/image/{id} sont gardées dans ce dossier
# (le volume de données, donc elles survivent aux redémarrages) : une affiche
# déjà vue ne redéclenche AUCUNE requête vers Jellyfin. Vide = cache désactivé.
POSTER_CACHE_DIR = os.getenv("POSTER_CACHE_DIR", "/app/data/posters")
# Durée de validité (jours) d'une affiche demandée SANS identifiant de version
# (usage direct de l'API). La page /bibliotheque, elle, fournit le tag d'image
# de Jellyfin : l'entrée est alors valable indéfiniment, et remplacée d'elle-même
# quand l'affiche change côté Jellyfin.
POSTER_CACHE_TTL_DAYS = _env_float("POSTER_CACHE_TTL_DAYS", 7.0)

# --- Badge AlloCiné dans l'interface web de Jellyfin ---
# Origine autorisée à lire GET /jellyfin/allocine-notes depuis le navigateur (CORS) :
# l'adresse par laquelle VOUS ouvrez Jellyfin, ex. "http://192.168.1.10:8096".
# "*" (défaut) autorise toute origine : acceptable ici (le contenu ne comprend que des
# notes AlloCiné publiques et des identifiants d'items), mais restreindre est plus propre.
NOTES_CORS_ORIGIN = os.getenv("NOTES_CORS_ORIGIN", "*").strip() or "*"

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
