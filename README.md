# Pathé Toulouse Wilson + AlloCiné + Jellyfin API

API REST (FastAPI) qui :
- récupère les films actuellement à l'affiche au **Pathé Toulouse Wilson** (titre, séances, affiche...),
- les enrichit de leur **note AlloCiné** (presse / spectateurs),
- **injecte ces notes dans Jellyfin** (CommunityRating = note spectateurs, CriticRating = note presse) pour tous les films/séries de votre bibliothèque,
- expose un **flux calendrier (.ics)** listant les films Pathé Toulouse Wilson dont le titre correspond à un film/série déjà présent dans votre bibliothèque Jellyfin — intégrable dans **Homepage** ou **Homarr**.

Le scraping Pathé/AlloCiné est effectué **une seule fois, au démarrage du conteneur**.
La synchronisation Jellyfin est **automatique au démarrage si `JELLYFIN_URL`/`JELLYFIN_API_KEY` sont configurés**, et peut être relancée manuellement via `POST /jellyfin/sync-notes`.
Le calendrier `.ics`, lui, est **généré à la volée à chaque requête** (toujours à jour par rapport aux dernières données en mémoire).

## ⚠️ Avertissement important

Ce code a été écrit **sans pouvoir tester l'accès réel** à pathe.fr, allocine.fr, ni à un serveur
Jellyfin réel (environnement de développement sans accès internet général ni serveur Jellyfin
disponible). La logique de scraping et d'appel API Jellyfin est écrite sur la base de patterns
documentés et habituels, mais **un ajustement est probable** au premier lancement chez vous.
Voir [Déboguer le scraping](#déboguer-le-scraping) et [Déboguer Jellyfin](#déboguer-jellyfin).

Pensez aussi à consulter les conditions d'utilisation d'AlloCiné et Pathé : ce projet est prévu
pour un usage personnel/non commercial, avec une fréquence de scraping raisonnable.

## Démarrage rapide

```bash
cp .env.example .env
# éditez .env avec l'URL et la clé API de votre serveur Jellyfin
cp data/title_aliases.example.json data/title_aliases.json   # optionnel, voir plus bas
docker compose up --build
```

L'API est alors disponible sur `http://localhost:8000`.
Documentation interactive (Swagger) : `http://localhost:8000/docs`

Si vous ne voulez utiliser que le scraping Pathé/AlloCiné sans Jellyfin, ne renseignez simplement
pas `JELLYFIN_URL`/`JELLYFIN_API_KEY` : ces fonctionnalités seront juste désactivées (log
d'information au démarrage), le reste de l'API fonctionne normalement.

## Endpoints

| Méthode | Route | Description |
|---|---|---|
| GET | `/health` | Vérifie que le service tourne |
| GET | `/statut` | Date du dernier scraping Pathé/AlloCiné, nb de films, erreurs |
| POST | `/refresh` | Relance manuellement le scraping Pathé + AlloCiné |
| GET | `/films` | Liste des films à l'affiche au Pathé Toulouse Wilson (+ note AlloCiné) |
| GET | `/films/{slug}` | Détail d'un film (séances, synopsis, note...) |
| GET | `/allocine/note?titre=...&type=film\|serie` | Note AlloCiné pour n'importe quel titre |
| POST | `/jellyfin/sync-notes` | Relance la synchro des notes AlloCiné → Jellyfin |
| GET | `/jellyfin/statut` | Statut de la dernière synchro Jellyfin |
| GET | `/calendar.ics` | Flux iCal des films Pathé présents dans votre bibliothèque Jellyfin |

## Intégration Jellyfin (notes AlloCiné)

1. Dans Jellyfin : **Dashboard → Clés API → Ajouter**, copiez la clé.
2. Renseignez `JELLYFIN_URL` (ex: `http://192.168.1.10:8096`) et `JELLYFIN_API_KEY` dans `.env`.
3. Au démarrage du conteneur (ou via `POST /jellyfin/sync-notes`), chaque film/série de votre
   bibliothèque est recherché sur AlloCiné par titre, et :
   - la **note spectateurs** (/5) est convertie en `CommunityRating` (/10, ×2),
   - la **note presse** (/5) est convertie en `CriticRating` (/100, ×20).
4. Ces champs s'affichent nativement dans les clients Jellyfin (web, apps mobiles/TV) comme des
   badges de notation, au même endroit que les notes IMDb/Rotten Tomatoes habituelles.

Par mesure de sécurité, la mise à jour récupère toujours l'item Jellyfin complet avant de le
renvoyer avec uniquement les deux champs de note modifiés (pour éviter un bug connu de Jellyfin où
un envoi partiel peut corrompre les métadonnées d'un item).

### Déboguer Jellyfin

- Si `POST /jellyfin/sync-notes` retourne des erreurs 401/403 : vérifiez la clé API.
- Si la liste d'items récupérée est vide ou incomplète : certaines versions de Jellyfin exigent un
  identifiant utilisateur pour lister les items. Renseignez alors `JELLYFIN_USER_ID` (visible dans
  Dashboard → Utilisateurs, dans l'URL du profil) dans `.env`.
- Consultez `GET /jellyfin/statut` pour voir le détail des erreurs (par titre) après une synchro.

## Calendrier pour Homepage / Homarr

L'endpoint `GET /calendar.ics` renvoie un flux iCal standard contenant uniquement les films
actuellement à l'affiche au Pathé Toulouse Wilson **dont le titre correspond à un film/série déjà
présent dans votre bibliothèque Jellyfin**.

### Configuration Homepage (gethomepage.dev)

Dans `services.yaml` :
```yaml
- Cinéma:
    widget:
      type: calendar
      integrations:
        - type: ical
          url: http://<adresse-de-ce-conteneur>:8000/calendar.ics
          name: Pathé Toulouse Wilson
```

### Configuration Homarr

Ajoutez un widget **Calendar**, choisissez l'intégration **iCal générique**, et renseignez :
```
http://<adresse-de-ce-conteneur>:8000/calendar.ics
```

### Correspondance des titres (Pathé/AlloCiné en français vs Jellyfin)

Le rapprochement entre le titre affiché au Pathé (généralement en français) et le titre dans votre
bibliothèque Jellyfin (souvent dans la langue originale) se fait automatiquement pour les titres
identiques ou très proches (accents, casse, ponctuation ignorés). Pour les titres réellement
différents d'une langue à l'autre (ex. "Dune : Deuxième Partie" vs "Dune: Part Two"), utilisez le
fichier d'alias éditable à chaud :

```bash
cp data/title_aliases.example.json data/title_aliases.json
```

```json
{
  "Dune : Deuxième Partie": "Dune: Part Two",
  "Vice-Versa 2": "Inside Out 2"
}
```

Ce fichier est relu à chaque génération du calendrier : pas besoin de redémarrer le conteneur après
modification.

## Déboguer le scraping

Si `/films` renvoie une liste vide ou incomplète, le site ciblé a probablement changé de structure
(ou nécessite un rendu JavaScript que `requests` seul ne fait pas).

1. Le HTML brut récupéré est automatiquement sauvegardé dans le dossier `./data` (monté depuis le
   conteneur) grâce à `DEBUG_SAVE_HTML=true` (activé par défaut).
2. Ouvrez les fichiers `pathe_toulouse_wilson_*.html` ou `allocine_*.html` dans un navigateur ou un
   éditeur pour identifier la structure réelle (classes CSS, présence ou non d'un
   `<script id="__NEXT_DATA__">`, etc.).
3. Ajustez les sélecteurs dans :
   - `app/scrapers/pathe_scraper.py` (fonction `_films_from_html_fallback` ou `_films_from_next_data`)
   - `app/scrapers/allocine_scraper.py` (fonction `_extract_notes` et `_find_fiche_url`)
4. Reconstruisez l'image : `docker compose up --build`

### Si le site Pathé nécessite du JavaScript

Si le HTML sauvegardé ne contient presque aucune donnée (page quasi vide, contenu chargé
dynamiquement en JS), il faudra remplacer `requests` par un navigateur headless comme **Playwright** :

```bash
pip install playwright
playwright install chromium
```

et adapter `_fetch_html()` dans `pathe_scraper.py` pour utiliser Playwright au lieu de `requests`.
Je peux faire cet ajustement pour vous si vous confirmez que c'est nécessaire après inspection du
HTML de debug.

## Configuration (variables d'environnement)

| Variable | Défaut | Description |
|---|---|---|
| `PATHE_CINEMA_SLUG` | `pathe-toulouse-wilson` | Slug de l'URL du cinéma sur pathe.fr |
| `DEBUG_SAVE_HTML` | `true` | Sauvegarde le HTML brut récupéré pour debug |
| `DEBUG_DATA_DIR` | `/app/data` | Dossier de sauvegarde du HTML de debug |
| `JELLYFIN_URL` | *(vide)* | URL du serveur Jellyfin, sans slash final |
| `JELLYFIN_API_KEY` | *(vide)* | Clé API Jellyfin |
| `JELLYFIN_USER_ID` | *(vide)* | Optionnel, voir [Déboguer Jellyfin](#déboguer-jellyfin) |
| `TITLE_ALIASES_PATH` | `/app/data/title_aliases.json` | Fichier d'alias de titres Pathé ↔ Jellyfin |
| `TITLE_MATCH_THRESHOLD` | `0.85` | Seuil de similarité (0-1) pour le rapprochement approximatif de titres |
| `CALENDAR_NAME` | `Pathé Toulouse Wilson (dans ma bibliothèque Jellyfin)` | Nom affiché du calendrier (X-WR-CALNAME) |

## Structure du projet

```
app/
  main.py                    # Endpoints FastAPI
  config.py                  # URLs, headers HTTP, config debug/Jellyfin/calendrier
  models.py                  # Modèles Pydantic (Film, Seance, NoteAlloCine, StatutSyncJellyfin...)
  storage.py                 # Stockage en mémoire + orchestration des pipelines
  matching.py                # Normalisation et rapprochement de titres (+ alias)
  jellyfin_client.py         # Client API Jellyfin (lecture bibliothèque + mise à jour notes)
  calendar_builder.py        # Génération du flux ICS filtré par bibliothèque Jellyfin
  scrapers/
    pathe_scraper.py         # Scraping Pathé Toulouse Wilson
    allocine_scraper.py      # Scraping des notes AlloCiné
data/
  title_aliases.example.json # Exemple de fichier d'alias (à copier en title_aliases.json)
Dockerfile
docker-compose.yml
requirements.txt
.env.example
```

