# 🎬 Movies Tracker

Application Docker qui suit les films à l'affiche au **Pathé Toulouse Wilson** (séances du jour et notes AlloCiné), les injecte dans **Jellyfin**, et expose le tout via une **API REST**, une **page web**, et un **flux calendrier** intégrable dans un tableau de bord type Homepage ou Homarr.

## Fonctionnalités

- **Films à l'affiche** : titre, affiche, genres, synopsis, séances du jour (VF/VOST, horaires), notes AlloCiné (presse et spectateurs) — récupérés en une seule requête vers la page salle AlloCiné du cinéma.
- **Page web** (`/`) : tout ce qui précède, présenté en cartes avec affiches, avec des boutons pour rafraîchir les données ou lancer la synchro Jellyfin.
- **Synchro Jellyfin** : injecte la note AlloCiné de chaque film/série de votre bibliothèque dans `CommunityRating` (spectateurs) et `CriticRating` (presse) — visible directement dans les clients Jellyfin, comme des notes IMDb/Rotten Tomatoes.
- **Calendrier iCal** (`/calendar.ics`) : les films à l'affiche dont le titre correspond à un titre déjà présent dans votre bibliothèque Jellyfin, intégrable dans Homepage ou Homarr.
- **API REST** complète (voir [Endpoints](#endpoints)), documentée automatiquement sur `/docs`.

## Pourquoi AlloCiné plutôt que pathe.fr ?

pathe.fr est protégé par **Akamai Bot Manager**, qui a résisté à toutes les tentatives de contournement testées (en-têtes réalistes, empreinte TLS type navigateur via `curl_cffi`, navigateur headless via Playwright, cookies de session persistants, limitation du volume de requêtes). AlloCiné héberge une page dédiée à ce cinéma qui liste films, séances et notes en une seule page, sans blocage rencontré — c'est donc la seule source utilisée par l'application.

## Démarrage rapide

```bash
cp .env.example .env
# éditez .env : JELLYFIN_URL et JELLYFIN_API_KEY (Dashboard Jellyfin > Clés API)
docker compose up --build
# optionnel : fichier d'alias de titres, à copier dans le volume (voir « Volume de données » plus bas)
```

L'application est disponible sur `http://localhost:8095` (page web) et `http://localhost:8095/docs` (documentation API interactive).

Si vous ne voulez utiliser que le scraping AlloCiné sans Jellyfin, ne renseignez simplement pas `JELLYFIN_URL`/`JELLYFIN_API_KEY` : la synchro et le calendrier filtré seront juste désactivés (log d'information au démarrage), le reste de l'application fonctionne normalement.

### Image Docker

Le `docker-compose.yml` fourni construit l'image localement (`build: .`) et la tague `tropicfront/movies_tracker:latest`. Pour la publier sur Docker Hub après un premier build réussi :

```bash
docker compose build
docker push tropicfront/movies_tracker:latest
```

D'autres machines peuvent alors utiliser directement l'image publiée sans avoir le code source, en retirant la ligne `build: .` de leur `docker-compose.yml`.

## Endpoints

| Méthode | Route | Description |
|---|---|---|
| GET | `/` | **Page web** : films, affiches, notes, séances, statuts, boutons d'action |
| GET | `/health` | Vérifie que le service tourne |
| GET | `/statut` | Date du dernier scraping, nb de films, erreurs |
| POST | `/refresh` | Relance manuellement le scraping AlloCiné |
| GET | `/films` | Liste des films à l'affiche (+ note AlloCiné) |
| GET | `/films/{slug}` | Détail d'un film (séances, synopsis, note...) |
| GET | `/allocine/note?titre=...&type=film\|serie` | Note AlloCiné pour n'importe quel titre |
| POST | `/jellyfin/sync-notes` | Relance la synchro des notes AlloCiné → Jellyfin |
| GET | `/jellyfin/statut` | Statut de la dernière synchro Jellyfin |
| GET | `/calendar.ics` | Flux iCal des films à l'affiche présents dans votre bibliothèque Jellyfin |

## Intégration Jellyfin

Compatible Jellyfin 10.x et 12.x (12.0, sorti le 7 septembre 2026, a durci le format de l'en-tête
d'authentification — ce client utilise le format strict requis, avec valeurs entre guillemets).

1. Dans Jellyfin : **Dashboard → Clés API → Ajouter**, copiez la clé.
2. Renseignez `JELLYFIN_URL` (ex: `http://192.168.1.10:8096`) et `JELLYFIN_API_KEY` dans `.env`.
3. Au démarrage du conteneur (ou via `POST /jellyfin/sync-notes`), chaque film/série de votre
   bibliothèque est recherché sur AlloCiné par titre, et :
   - la **note spectateurs** (/5) est convertie en `CommunityRating` (/10, ×2),
   - la **note presse** (/5) est convertie en `CriticRating` (/100, ×20).
4. Ces champs s'affichent nativement dans les clients Jellyfin (web, apps mobiles/TV).

La mise à jour récupère toujours l'item Jellyfin complet avant de le renvoyer avec uniquement les
deux champs de note modifiés (pour éviter un bug connu de Jellyfin où un envoi partiel peut
corrompre les métadonnées d'un item).

> **Synchro Jellyfin en arrière-plan** : elle dure plusieurs minutes (une requête AlloCiné par titre, avec pause). Elle ne bloque donc ni le démarrage ni la page web : l'API et le dashboard répondent immédiatement, et l'avancement est visible sur le dashboard ou via `GET /jellyfin/statut` (`en_cours`, `nb_traites`, `nb_items_bibliotheque`). `POST /jellyfin/sync-notes` rend la main tout de suite et ne lance pas de seconde synchro si une tourne déjà.

### Déboguer Jellyfin

- Erreurs 401/403 sur `POST /jellyfin/sync-notes` : vérifiez la clé API.
- Bibliothèque vide ou incomplète : certaines versions de Jellyfin exigent un identifiant
  utilisateur pour lister les items. Renseignez alors `JELLYFIN_USER_ID` (visible dans
  Dashboard → Utilisateurs, dans l'URL du profil) dans `.env`.
- `GET /jellyfin/statut` donne le détail des erreurs (par titre) après une synchro.

## Calendrier pour Homepage / Homarr

L'endpoint `GET /calendar.ics` renvoie un flux iCal standard contenant uniquement les films
actuellement à l'affiche dont le titre correspond à un film/série déjà présent dans votre
bibliothèque Jellyfin.

**Homepage** (`services.yaml`) :
```yaml
- Cinéma:
    widget:
      type: calendar
      integrations:
        - type: ical
          url: http://<adresse-de-ce-conteneur>:8095/calendar.ics
          name: Pathé Toulouse Wilson
```

**Homarr** : widget **Calendar** → intégration **iCal générique** → renseignez
`http://<adresse-de-ce-conteneur>:8095/calendar.ics`.

### Correspondance des titres (AlloCiné en français vs Jellyfin)

Le rapprochement se fait automatiquement pour les titres identiques ou très proches (accents,
casse, ponctuation ignorés). Pour les titres réellement différents d'une langue à l'autre
(ex. "Dune : Deuxième Partie" vs "Dune: Part Two"), utilisez le fichier d'alias éditable à chaud :

```bash
# le dossier /app/data du conteneur est un volume nommé : on y copie le fichier avec docker cp
cp data/title_aliases.example.json title_aliases.json   # puis éditez-le
docker cp title_aliases.json movies_tracker:/app/data/title_aliases.json
```
```json
{
  "Dune : Deuxième Partie": "Dune: Part Two",
  "Vice-Versa 2": "Inside Out 2"
}
```
Ce fichier est relu à chaque génération du calendrier : pas besoin de redémarrer le conteneur.

## ⚠️ Avertissement

Le scraping AlloCiné a été testé avec des données réalistes reconstruites à partir de la vraie
page, mais **pas contre le site en conditions réelles prolongées**. Si `/films` renvoie une liste
vide ou incomplète, activez `DEBUG_SAVE_HTML=true` (déjà activé par défaut) et inspectez le fichier
HTML sauvegardé dans le volume `movies_tracker_data` (voir « Volume de données »).

Consultez les conditions d'utilisation d'AlloCiné : ce projet est prévu pour un usage
personnel/non commercial, avec une fréquence de scraping raisonnable (une fois au démarrage par défaut).

**Limite connue** : la page salle AlloCiné n'affiche que les séances du jour actuellement
sélectionné (aujourd'hui par défaut). Un sélecteur de date existe sur le site, mais son paramètre
d'URL exact n'a pas été identifié.

## Débit vers AlloCiné et durée de la synchro

Deux réglages complémentaires (dans `docker-compose.yml`, surchargeables via `.env` ou les variables
de la stack Portainer) :

- `ALLOCINE_REQUESTS_PER_SECOND` (défaut `1`) : plafond global, appliqué à **toutes** les requêtes
  vers AlloCiné, y compris les nouvelles tentatives après un 429.
- `ALLOCINE_SYNC_DELAY_SECONDS` (défaut `2`) : pause en plus entre deux titres de la bibliothèque.

Un titre = 2 requêtes (recherche + fiche). Avec les défauts, comptez donc **environ 4 s par titre**
(2 s pour les 2 requêtes + 2 s de pause) : ~20 minutes pour 300 titres. Le log de démarrage de la
synchro affiche cette estimation minimale. La synchro tourne en arrière-plan : la page reste utilisable.
Une valeur invalide (texte, négatif) est ignorée avec un avertissement dans les logs et le défaut est utilisé.

## Volume de données

Les données du conteneur (`/app/data` : HTML de debug, fichier d'alias de titres) sont stockées dans un
**volume Docker nommé** `movies_tracker_data`, visible dans Portainer > Volumes (le nom est fixé pour ne pas
être préfixé par le nom de la stack).

Contrairement à un dossier `./data` monté depuis l'hôte, ce volume n'est pas directement éditable depuis
votre machine. Pour y déposer un fichier d'alias, ou en récupérer un :

```bash
docker cp title_aliases.json movies_tracker:/app/data/title_aliases.json   # déposer
docker cp movies_tracker:/app/data/title_aliases.json .                    # récupérer
```

Le volume survit à `docker compose down` et aux reconstructions d'image ; seul `docker compose down -v`
(ou sa suppression dans Portainer) l'efface.

## Déboguer le scraping AlloCiné

Si `/films` renvoie une liste vide ou incomplète :

1. Le HTML brut récupéré est automatiquement sauvegardé dans le volume `movies_tracker_data` (`/app/data` dans le conteneur).
2. Ouvrez le fichier `allocine_salle_pathe_toulouse_wilson_*.html` pour identifier la structure
   réelle si elle a changé.
3. Ajustez les expressions régulières et sélecteurs dans
   `app/scrapers/allocine_theater_scraper.py` (l'extraction se base sur le flux de texte après
   chaque titre de film, pas sur des classes CSS précises).
4. Reconstruisez l'image : `docker compose up --build`

## Configuration (variables d'environnement)

| Variable | Défaut | Description |
|---|---|---|
| `ALLOCINE_SALLE_CODE` | `P0057` | Code salle AlloCiné du Pathé Toulouse Wilson |
| `DEBUG_SAVE_HTML` | `true` | Sauvegarde le HTML brut récupéré pour debug |
| `DEBUG_DATA_DIR` | `/app/data` | Dossier de sauvegarde du HTML de debug |
| `JELLYFIN_URL` | *(vide)* | URL du serveur Jellyfin, sans slash final |
| `JELLYFIN_API_KEY` | *(vide)* | Clé API Jellyfin |
| `JELLYFIN_USER_ID` | *(vide)* | Optionnel, voir [Déboguer Jellyfin](#déboguer-jellyfin) |
| `TITLE_ALIASES_PATH` | `/app/data/title_aliases.json` | Fichier d'alias de titres AlloCiné ↔ Jellyfin |
| `TITLE_MATCH_THRESHOLD` | `0.85` | Seuil de similarité (0-1) pour le rapprochement approximatif de titres |
| `ALLOCINE_REQUESTS_PER_SECOND` | `1` | Nombre **maximal** de requêtes par seconde vers AlloCiné, toutes requêtes confondues (recherche, fiche, page salle). Décimales acceptées : `0.5` = une requête toutes les 2 s. `0` = pas de limite |
| `ALLOCINE_SYNC_DELAY_SECONDS` | `2` | Pause supplémentaire entre chaque **titre** lors de la synchro Jellyfin, en plus de la limite ci-dessus (évite les 429 AlloCiné sur les grosses bibliothèques) |
| `JELLYFIN_IGNORE_REGEX` | `\s[-–—]\s*Saga\s*$` | Éléments Jellyfin ignorés (regex, insensible à la casse, appliquée au nom). Par défaut les regroupements « … - Saga » ; les collections (BoxSet) sont toujours exclues. Vide = désactivé |
| `CALENDAR_NAME` | `Pathé Toulouse Wilson (dans ma bibliothèque Jellyfin)` | Nom affiché du calendrier (X-WR-CALNAME) |

## Structure du projet

```
app/
  main.py                        # Endpoints FastAPI + page web
  config.py                      # URLs, headers HTTP, config debug/Jellyfin/calendrier
  models.py                      # Modèles Pydantic (Film, Seance, NoteAlloCine, StatutSyncJellyfin...)
  storage.py                     # Stockage en mémoire + orchestration des pipelines
  matching.py                    # Normalisation et rapprochement de titres (+ alias)
  jellyfin_client.py             # Client API Jellyfin (lecture bibliothèque + mise à jour notes)
  calendar_builder.py            # Génération du flux ICS filtré par bibliothèque Jellyfin
  scrapers/
    allocine_theater_scraper.py  # Films + séances + notes AlloCiné (source unique)
    allocine_scraper.py          # Recherche de note AlloCiné par titre (endpoint générique + sync Jellyfin)
  static/
    dashboard.html               # Page web (route GET /) — lu à chaque requête, éditable sans rebuild
data/
  title_aliases.example.json     # Exemple de fichier d'alias (à copier en title_aliases.json)
Dockerfile
docker-compose.yml
requirements.txt
.env.example
```
