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
docker build -t tropicfront/movies_tracker:latest .   # construit l'image (le compose ne construit pas)
docker compose up -d
# optionnel : fichier d'alias de titres, à copier dans le volume (voir « Volume de données » plus bas)
```

L'application est disponible sur `http://localhost:8095` (page web) et `http://localhost:8095/docs` (documentation API interactive).

Si vous ne voulez utiliser que le scraping AlloCiné sans Jellyfin, ne renseignez simplement pas `JELLYFIN_URL`/`JELLYFIN_API_KEY` : la synchro et le calendrier filtré seront juste désactivés (log d'information au démarrage), le reste de l'application fonctionne normalement.

### Image Docker

Le `docker-compose.yml` **ne construit pas** l'image : il utilise `tropicfront/movies_tracker:latest`, qui doit
déjà exister sur l'hôte Docker (ou sur un registre). Construisez-la depuis le dossier du projet, celui qui
contient le `Dockerfile` :

```bash
docker build -t tropicfront/movies_tracker:latest .
```

Pour la publier sur Docker Hub (utile si Docker/Portainer tourne sur une autre machine) :

```bash
docker push tropicfront/movies_tracker:latest
```

Après **chaque mise à jour du code**, reconstruisez l'image puis recréez le conteneur
(`docker compose up -d --force-recreate`) : `docker compose up` ne reconstruit jamais une image déjà présente.

## Endpoints

| Méthode | Route | Description |
|---|---|---|
| GET | `/` | **Page web** : films, affiches, notes, séances, statuts, boutons d'action |
| GET | `/bibliotheque` | **Page web** : films et séries de votre bibliothèque Jellyfin, avec affiches et notes AlloCiné |
| GET | `/health` | Vérifie que le service tourne |
| GET | `/statut` | Date du dernier scraping, nb de films, erreurs |
| POST | `/refresh` | Relance manuellement le scraping AlloCiné |
| GET | `/films` | Liste des films à l'affiche (+ note AlloCiné) |
| GET | `/films/{slug}` | Détail d'un film (séances, synopsis, note...) |
| GET | `/allocine/note?titre=...&type=film\|serie[&annee=...][&force_refresh=true]` | Note AlloCiné pour n'importe quel titre |
| DELETE | `/allocine/cache` | Vide le cache des résultats AlloCiné |
| POST | `/jellyfin/sync-notes[?force_refresh=true]` | Relance la synchro (rescan complet AlloCiné + Jellyfin si `force_refresh=true`) |
| POST | `/jellyfin/push-cached-notes` | Écrit vers Jellyfin les notes déjà en cache, sans aucune requête AlloCiné |
| GET | `/jellyfin/statut` | Statut de la dernière synchro Jellyfin |
| GET | `/jellyfin/library` | Films/séries Jellyfin + note AlloCiné en cache (JSON, alimente `/bibliotheque`) |
| GET | `/jellyfin/image/{id}[?w=400][&tag=…]` | Affiche d'un item Jellyfin (proxy authentifié, redimensionnée, **gardée sur disque**) |
| GET / DELETE | `/jellyfin/image-cache` | Nombre d'affiches en cache et espace disque / vider ce cache |
| GET | `/calendar.ics` | Flux iCal des films à l'affiche présents dans votre bibliothèque Jellyfin |

## Page « Bibliothèque Jellyfin »

`http://<hôte>:8095/bibliotheque` (bouton **📚 Bibliothèque Jellyfin** sur la page d'accueil) affiche vos
films et séries Jellyfin comme la page des films à l'affiche : affiche, titre (cliquable vers la fiche
AlloCiné quand elle est connue), type, année, genres, notes **Presse** / **Spectateurs** sur 5, synopsis.

- **Filtres** : recherche par titre (insensible à la casse **et aux accents** : `amelie` trouve « Amélie »),
  films / séries, avec ou sans note AlloCiné (pratique pour repérer les titres non trouvés).
- **Tri** : titre, plus récents / plus anciens, meilleure note spectateurs, meilleure note presse. Les
  éléments sans valeur (année ou note inconnue) sont toujours placés en dernier.
- **Les notes viennent du cache local**, jamais d'une recherche en direct : ouvrir la page ne déclenche
  aucune requête vers AlloCiné. Un titre jamais résolu s'affiche « Note inconnue » ; lancez une synchro
  (🔄 / 🔁 sur la page d'accueil) pour les récupérer.
- **Affiches** : servies par l'application (`/jellyfin/image/{id}`), qui les récupère avec la clé API et les
  fait **redimensionner par Jellyfin** (`?w=`, 400 px par défaut, borné entre 100 et 1000) : les afficher en
  taille d'origine pour des centaines de titres serait très lourd. Le navigateur n'a ainsi jamais besoin
  d'accéder directement à Jellyfin ni de connaître la clé API.
- Si Jellyfin n'est pas configuré, la page l'indique au lieu d'afficher une erreur technique.
- Le titre affiché est celui de Jellyfin **sans** l'année entre parenthèses (`Macross (1982)` → `Macross`,
  année 1982 dans sa propre colonne).

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

**Homonymes** (plusieurs films/séries portant le même titre côté AlloCiné, ex. deux "Macross" ou
deux "Apocalypse Now" d'années différentes) : départagés par année. La source privilégiée est
`ProductionYear` tel que renvoyé par Jellyfin ; si le titre lui-même se termine par une année entre
parenthèses (convention fréquente pour les animes/séries, ex. `Macross (1982)`), cette année sert de
repli — la parenthèse est retirée du titre avant la recherche (elle gênerait une recherche en texte
libre). Si les deux sources existent et diffèrent, `ProductionYear` est prioritaire.

> **Synchro Jellyfin en arrière-plan** : elle dure plusieurs minutes (une requête AlloCiné par titre, avec pause). Elle ne bloque donc ni le démarrage ni la page web : l'API et le dashboard répondent immédiatement, et l'avancement est visible sur le dashboard ou via `GET /jellyfin/statut` (`en_cours`, `nb_traites`, `nb_items_bibliotheque`). `POST /jellyfin/sync-notes` rend la main tout de suite et ne lance pas de seconde synchro si une tourne déjà.

### Déboguer Jellyfin

- Erreurs 401/403 sur `POST /jellyfin/sync-notes` : vérifiez la clé API.
- Bibliothèque vide ou incomplète : certaines versions de Jellyfin exigent un identifiant
  utilisateur pour lister les items. Renseignez alors `JELLYFIN_USER_ID` (visible dans
  Dashboard → Utilisateurs, dans l'URL du profil) dans `.env`.
- **Bibliothèque tronquée** (ex. 445 items traités alors que Jellyfin en affiche 686) : corrigé.
  `get_library_items()` paginait implicitement en se fiant au comportement par défaut de l'API
  quand `StartIndex`/`Limit` ne sont pas fournis — ce comportement varie selon les versions de
  Jellyfin. La pagination est désormais explicite (boucle jusqu'à `TotalRecordCount`, triée par nom pour qu'aucune page ne chevauche la suivante), donc plus
  aucune bibliothèque ne devrait être tronquée quelle que soit sa taille. Le log de démarrage
  indique désormais le nombre total récupéré (`GET /jellyfin/statut` après le prochain scan, ou
  directement dans les logs du conteneur : `Bibliothèque Jellyfin : N film(s)/série(s) récupéré(s)`).
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

## Déploiement avec Portainer

Le `docker-compose.yml` ne contient pas de `build` : Portainer (Web editor, Upload ou Repository) n'a donc
besoin d'aucun `Dockerfile`, seulement de l'image. Sans cela, on obtenait l'erreur
`failed to read dockerfile: open Dockerfile: no such file or directory` (le mode Web editor/Upload n'envoie
que le fichier compose, sans `Dockerfile` ni dossier `app/`).

1. Construire l'image (voir « Image Docker » ci-dessus). Si Portainer tourne sur **une autre machine** que
   celle du build, passez par un registre : `docker login` puis `docker push tropicfront/movies_tracker:latest`
   (image privée : ajouter le registre dans Portainer > Registries).
2. Créer la stack avec le contenu de `docker-compose.yml`.
3. Renseigner `JELLYFIN_URL` et `JELLYFIN_API_KEY` dans **Environment variables** de la stack (les modes
   Web editor et Upload n'ont pas de fichier `.env`).

Sans registre, à ma connaissance Portainer sait aussi construire une image : *Images > Build a new image >
Upload*, avec une archive dont le `Dockerfile` est **à la racine** (pas dans un sous-dossier) :

```bash
cd allocine-pathe-app && tar -czf ../movies_tracker.tar.gz .
```
Nom de l'image : `tropicfront/movies_tracker:latest`.

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

### Cache des résultats AlloCiné

Seuls les titres **trouvés** sont mis en cache sur disque (`ALLOCINE_CACHE_PATH`, dans le volume de
données, persiste entre redémarrages). Une synchro répétée ne refait donc **aucune requête** pour un
titre déjà résolu. Un titre **non trouvé**, en revanche, est toujours recherché à nouveau à chaque
synchro, sans limite de temps : AlloCiné peut publier sa fiche entre-temps (sortie récente, série en
cours de diffusion...), et il serait dommage de rester bloqué sur un "non trouvé" indéfiniment.

- `ALLOCINE_CACHE_TTL_DAYS` (défaut `30`) : durée de validité d'une entrée **trouvée**, en jours. `0`
  ou moins désactive complètement le cache (chaque synchro refait tout, y compris les titres trouvés).
Trois façons de forcer une mise à jour, du plus complet au plus rapide :

- Bouton **🔁 Rescan complet (AlloCiné + Jellyfin)** sur le dashboard (ou
  `POST /jellyfin/sync-notes?force_refresh=true`) : recherche TOUS les titres à nouveau sur AlloCiné (le
  cache est ignoré, pas juste complété) ET réécrit TOUS les items Jellyfin correspondants, même ceux qui
  ont déjà une note à jour. L'opération la plus lente et la plus complète des trois — utile en cas de
  doute sur des notes existantes (ex. après une correction de la logique de correspondance). Une
  confirmation est demandée avant de lancer depuis le dashboard.
- Bouton **📤 Appliquer le cache à Jellyfin** sur le dashboard (ou `POST /jellyfin/push-cached-notes`) :
  n'interroge **jamais** AlloCiné. Écrit vers Jellyfin les notes déjà présentes dans le cache ; les
  titres sans entrée de cache sont ignorés (ni recherchés, ni modifiés). Quasi instantané. Utile par
  exemple juste après avoir corrigé un problème côté Jellyfin (comme le bug de verrouillage plus bas) :
  les notes déjà connues sont réappliquées sans attendre une nouvelle synchro complète.
- `DELETE /allocine/cache` : vide le cache entièrement.
- `GET /allocine/note?...&force_refresh=true` : force_refresh pour un seul titre, sans passer par Jellyfin.

Les trois opérations Jellyfin (`sync-notes`, `push-cached-notes`, et la synchro automatique au démarrage)
partagent le même verrou : une seule à la fois peut tourner, les autres sont ignorées tant qu'elle n'est
pas terminée (suivre l'avancement via `GET /jellyfin/statut`).

## Diagnostic Jellyfin : titres manquants et notes absentes

Au démarrage de la synchro, le log donne le détail de ce qui a été lu dans Jellyfin. Comparez avec les
chiffres de votre tableau de bord Jellyfin :

```
Bibliothèque Jellyfin : 527 film(s) + 160 série(s) = 687 titre(s) retenu(s).
Recoupement OK : Jellyfin annonce 527 film(s) et 160 série(s).
```

### Il manque des films

**Cause constatée** : par défaut, Jellyfin peut **masquer les films d'une collection derrière la collection
elle-même** (paramètre `collapseBoxSetItems`). Une bibliothèque rangée en sagas (« Alien - Saga »…) perd alors
tous les films de ces sagas dans la liste. Symptôme dans les logs : des `BoxSet` renvoyés alors qu'on ne demande
que des films et des séries. L'application désactive ce repliage (`CollapseBoxSetItems=false`) et, si le
serveur renvoie malgré tout des collections, **ouvre chacune** pour en récupérer les membres.

Si le log affiche encore `Jellyfin annonce X film(s)… mais seuls Y ont pu être récupérés`, l'écart vient d'ailleurs :
`JELLYFIN_USER_ID` aux droits restreints (essayez sans), contrôle parental, ou bibliothèque non accessible à la clé
API. Le compte annoncé par Jellyfin est **indicatif** : il peut différer de celui de son interface.

### Les logs disent « mis à jour » mais rien n'apparaît dans Jellyfin

Une réponse HTTP positive ne prouve pas que la note est enregistrée. Après chaque écriture, l'application
**relit l'item** et compare. Trois cas, chacun avec son message :

- **`redirection HTTP 301 vers …`** : `JELLYFIN_URL` est redirigé (typiquement `http://` vers `https://`, ou un
  proxy qui impose un chemin). Un client HTTP qui suit la redirection transforme le POST en GET et abandonne le
  corps : réponse « OK », **rien d'écrit**. Les lectures fonctionnent malgré tout, d'où une liste correcte et des
  notes absentes. Mettez l'URL **finale** (celle que vous tapez dans votre navigateur) dans `JELLYFIN_URL`.
- **`Jellyfin NE CONSERVE PAS la note … envoyé 7.0, relu None`** : Jellyfin a accepté puis ignoré ou écrasé la
  valeur. Compté dans `nb_non_persistees` (`/jellyfin/statut`, et un bandeau rouge sur la page d'accueil).
- **`relecture impossible`** : l'écriture a été acceptée mais n'a pas pu être vérifiée.

La page `/bibliotheque` affiche pour chaque titre ce que Jellyfin contient **réellement** (et non ce qu'on lui a
envoyé) : `✓ Dans Jellyfin : …`, `⚠ Absente de Jellyfin (attendu …)`, ou `⚠ Jellyfin contient … (attendu …)` (Jellyfin a mis une autre note par-dessus).
Dans l'interface web de Jellyfin, la note communautaire (★) et la note critique s'affichent sur la **page de
détail** du film, pas sur les vignettes de la grille.

**Garde-fous** : une écriture n'est jamais envoyée si Jellyfin renvoie, pour un film, une autre fiche que celle
demandée (par exemple la collection qui le contient) : ses données écraseraient celles du film.

### Affiches

Les affiches sont servies par l'application (`/jellyfin/image/{id}`) et **gardées sur disque** dans le volume de
données : une affiche déjà vue n'entraîne **aucune requête** vers Jellyfin, même s'il est éteint. La clé de cache
inclut le `tag` d'image de Jellyfin, qui change quand l'affiche change : l'entrée se renouvelle d'elle-même et
l'ancienne version est supprimée. La largeur demandée est arrondie à un palier (200, 300, 400, 600, 800, 1000 px)
pour que l'espace disque reste borné. `GET /jellyfin/image-cache` donne le nombre de fichiers et la taille.

## Les notes n'apparaissent pas dans Jellyfin malgré des logs "mis à jour"

Comportement Jellyfin connu et documenté (pas propre à ce projet) : une mise à jour de champ envoyée
sans verrouiller l'item (`LockData`) est traitée comme une valeur "gérée par les fournisseurs de
métadonnées", et écrasée au prochain scan de bibliothèque ou rafraîchissement — la requête répond 200
(d'où le "mis à jour" dans les logs), mais la valeur ne tient pas dans le temps.

Une première version de ce correctif ajoutait aussi `CommunityRating`/`CriticRating` à `LockedFields`
(verrou par champ, plus précis). **Un vrai serveur Jellyfin (12.x) a rejeté cette requête avec une
erreur 400** (`$.LockedFields[0]` invalide) : ces deux noms ne font pas partie de l'énumération que le
serveur accepte pour ce champ. Corrigé : `app/jellyfin_client.py` verrouille désormais l'**item entier**
(`LockData=true`) sans toucher à `LockedFields`, sans jamais y écrire ces deux noms — plus large (ça
protège aussi les autres métadonnées de l'item contre un rafraîchissement automatique), mais c'est la
seule approche qui fonctionne réellement contre cette version de l'API. Un verrou déjà posé par vous sur
d'autres champs (`LockedFields` existant) est conservé tel quel, sans y toucher.

**Si vous avez utilisé une version antérieure de ce projet**, relancez une synchro (le bouton
🔁 Rescan complet, ou `force_refresh=true`) : les items dont la mise à jour a échoué en 400 n'ont reçu
aucune note. Si le problème persiste malgré cette version :
1. Vérifiez sur la fiche du film dans Jellyfin (icône de verrou / Édition des métadonnées) qu'il
   apparaît bien comme verrouillé dans son ensemble.
2. Si le verrou est bien posé mais saute tout seul après un moment : bug Jellyfin connu sur certaines
   versions 10.11.x (`LockData`/`LockedFields` peuvent se réinitialiser au redémarrage du serveur, ou ne
   pas persister via l'API) — indépendant de ce projet, à signaler côté Jellyfin.

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
4. Reconstruisez l'image (`docker build -t tropicfront/movies_tracker:latest .`) puis recréez le
   conteneur (`docker compose up -d --force-recreate`).

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
| `ALLOCINE_CACHE_PATH` | `/app/data/allocine_cache.json` | Fichier de cache des résultats AlloCiné (recherche + note par titre) |
| `ALLOCINE_CACHE_TTL_DAYS` | `30` | Durée de validité d'une entrée du cache, en jours. `<= 0` désactive le cache |
| `JELLYFIN_IGNORE_REGEX` | `\s[-–—]\s*Saga\s*$` | Éléments Jellyfin ignorés (regex, insensible à la casse, appliquée au nom). Par défaut les regroupements « … - Saga » ; les collections (BoxSet) sont toujours exclues. Vide = désactivé |
| `POSTER_CACHE_DIR` | `/app/data/posters` | Dossier du cache disque des affiches Jellyfin (dans le volume de données). Vide = cache désactivé |
| `POSTER_CACHE_TTL_DAYS` | `7` | Durée de validité (jours) d'une affiche demandée **sans** `tag`. La page `/bibliotheque` envoie le `tag` de Jellyfin : l'entrée est alors valable indéfiniment et se renouvelle d'elle-même si l'affiche change |
| `CALENDAR_NAME` | `Pathé Toulouse Wilson (dans ma bibliothèque Jellyfin)` | Nom affiché du calendrier (X-WR-CALNAME) |

## Structure du projet

```
app/
  main.py                        # Endpoints FastAPI + page web
  config.py                      # URLs, headers HTTP, config debug/Jellyfin/calendrier
  models.py                      # Modèles Pydantic (Film, Seance, NoteAlloCine, StatutSyncJellyfin...)
  storage.py                     # Stockage en mémoire + orchestration des pipelines
  allocine_cache.py              # Cache disque des résultats AlloCiné (par titre)
  ratelimit.py                   # Limiteur de débit global vers AlloCiné
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
