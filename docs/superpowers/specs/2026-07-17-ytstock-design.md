# ytstock — file de vidéos YouTube toujours prête

**Date :** 2026-07-17
**Statut :** design validé

## Objectif

Maintenir en permanence un stock de vidéos YouTube non vues, téléchargées
localement dans `~/Downloads/videos`, tirées de mes abonnements, de mes
recommandations et de thèmes que je définis. Quand je regarde une vidéo
(localement ou sur youtube.com), elle est supprimée et le stock se recomplète
tout seul. Piloté par un **budget disque de 15 Go** (pas un nombre fixe de
vidéos), 720p max, en arrière-plan sans intervention.

## Contraintes & décisions

| Sujet | Décision |
|---|---|
| Budget | **15 Go** de vidéos en stock (refill dès qu'on passe sous le seuil) |
| Qualité | **720p max** |
| Sources | Abonnements (`:ytsubs`) + recommandations (`:ytrec`) + recherche par thème (`ytsearch`) |
| Thèmes | philosophie, ARTE, psychologie, documentaire, NBA |
| Sélection | Classement par **taux d'engagement** = (likes/vues)·100 + (coms/vues)·500, plancher MIN_VIEWS=500 / MIN_LIKES=20 pour écarter le bruit ; fetch métadonnées par chunks de 25 |
| « Vue » locale | Auto via `lsof` — fichier ouvert ≥ 90s par un lecteur puis refermé |
| « Vue » online | Bonus best-effort via `yt-dlp :ythistory` |
| Automatisme | Full auto en arrière-plan via `launchd` (survit au reboot) |
| Cookies | `--cookies-from-browser firefox` (chrome = prompt Trousseau à chaque accès) |
| Téléchargeur | `yt-dlp` + `--external-downloader aria2c` |
| Filtres | Exclure Shorts (< 90s), lives (`--match-filters !is_live`), doublons déjà vus |

## Environnement (déjà en place)

- `yt-dlp` 2026.07.04, `aria2c` 1.37.0, `jq` — installés via Homebrew.
- Dossier cible : `/Users/satoshi/Downloads/videos` (contient déjà des `.mp4`/`.webm`
  nommés `Titre [VIDEOID].ext` — convention yt-dlp qu'on réutilise).

## Architecture

**Un seul script Python 3 (stdlib uniquement), `ytstock.py`**, qui orchestre les
binaires externes. Aucune dépendance pip. **Un fichier launchd** pour le démon.

### État persistant

- **Le dossier `videos/` EST le stock.** L'ID de chaque vidéo se lit dans le nom
  de fichier (`… [VIDEOID].ext`, regex `\[([A-Za-z0-9_-]{11})\]\.[^.]+$`).
- **`.ytstock/seen.txt`** — un ID YouTube par ligne. Contient tout ce qui a déjà
  été **téléchargé OU vu** → sert d'anti-doublon permanent. On n'y retire jamais
  rien (sauf purge manuelle).
- **`.ytstock/watch.json`** — état volatil du watcher : `{videoid: seconds_ouvert_cumulés}`
  pour les fichiers en cours de visionnage entre deux tics `lsof`.
- **`.ytstock/ytstock.log`** — log d'activité (téléchargements, suppressions, erreurs).

### Constantes réglables (en tête de script)

```python
VIDEO_DIR      = Path.home() / "Downloads" / "videos"
BUDGET_BYTES   = 15 * 1024**3      # 15 Go
MAX_HEIGHT     = 720
WATCHED_SECS   = 90                # ouverture cumulée => "vue"
MIN_DURATION   = 90                # < 90s = Short, ignoré
MAX_DURATION   = 2 * 3600          # > 2h ignoré (0 = pas de limite)
POLL_SECS      = 15                # fréquence lsof
HISTORY_SECS   = 30 * 60           # fréquence sync online
THEMES         = ["philosophie", "ARTE", "psychologie", "documentaire", "NBA"]
PLAYERS        = ["VLC", "IINA", "QuickTime", "mpv"]   # noms de process lecteurs
```

## Sous-commandes

### `ytstock daemon`
Processus permanent (lancé par launchd). Boucle infinie, tic toutes les `POLL_SECS` :
1. **Watch local** — voir « Détection vue locale ».
2. **Sync online** — toutes les `HISTORY_SECS`, voir « Détection vue online ».
3. **Budget** — après toute suppression, et au moins toutes les `HISTORY_SECS`,
   appeler `refill()` si la taille du dossier < `BUDGET_BYTES`.

### `ytstock refill`
Idempotent, appelable à la main :
1. `used = somme des tailles des vidéos du dossier`. Si `used >= BUDGET_BYTES` → stop.
2. Construit la liste de candidats **sans télécharger**, par priorité :
   `:ytsubs` → `:ytrec` → `ytsearch<N>:"<thème>"` pour chaque thème.
   Commande type : `yt-dlp --flat-playlist --print "%(id)s %(duration)s %(live_status)s" <source>`
   avec `--cookies-from-browser chrome`.
3. Filtre chaque candidat : rejette si ID ∈ `seen.txt`, si Short (`duration < MIN_DURATION`),
   si live (`live_status` ∈ {`is_live`, `is_upcoming`}), si `duration > MAX_DURATION`.
4. Télécharge les candidats retenus un par un jusqu'à `used >= BUDGET_BYTES` :
   ```
   yt-dlp \
     -f "bv*[height<=720]+ba/b[height<=720]" \
     --external-downloader aria2c \
     --external-downloader-args "aria2c:-x16 -s16 -k1M" \
     --cookies-from-browser chrome \
     -o "<VIDEO_DIR>/%(title)s [%(id)s].%(ext)s" \
     -- "<id>"
   ```
   Après chaque téléchargement réussi : ajoute l'ID à `seen.txt`, recompute `used`.
   En cas d'échec d'un ID : log + ajoute quand même à `seen.txt` (pour ne pas boucler
   dessus indéfiniment) + passe au suivant.

### `ytstock status`
Affiche : nb de vidéos en stock, Go utilisés / budget, nb d'IDs dans `seen.txt`,
et si le démon tourne.

## Détection « vue » locale (via `lsof`)

À chaque tic (`POLL_SECS`) :
1. `lsof -F n +D <VIDEO_DIR>` (ou `lsof` filtré sur les process de `PLAYERS`) →
   ensemble des fichiers vidéo actuellement **ouverts**.
2. Pour chaque fichier ouvert : `watch.json[id] += POLL_SECS`.
3. Pour chaque fichier qui **était** ouvert au tic précédent mais **ne l'est plus** :
   - si `watch.json[id] >= WATCHED_SECS` → **vue** : ajoute l'ID à `seen.txt`,
     supprime le fichier, log, déclenche `refill()`.
   - sinon (regardé < 90s) → on considère « pas vraiment vue » : on garde le fichier,
     on remet son compteur à 0.

Marche avec n'importe quel lecteur, sans commande spéciale. Le seuil `WATCHED_SECS`
évite qu'un simple aperçu de 3s supprime la vidéo.

## Détection « vue » online (bonus best-effort)

Toutes les `HISTORY_SECS` :
1. `yt-dlp :ythistory --flat-playlist --print id --playlist-end 50 --cookies-from-browser chrome`
   → IDs récemment vus sur youtube.com.
2. Pour chaque vidéo locale dont l'ID est dans cette liste → **vue** : `seen.txt` +
   suppression + `refill()`.
3. Toute erreur (cookies invalides, format YouTube changé) → log en warning et on
   continue. Ne doit **jamais** faire planter le démon ni bloquer la détection locale.

## Lancement auto (launchd) — 2 agents

- **`com.ytstock.plist`** (démon) : `daemon`, `RunAtLoad`+`KeepAlive`. Surveille les
  visionnages (lsof) + refill périodique.
- **`com.ytstock.netrefill.plist`** : `refill` déclenché par `WatchPaths` sur les
  fichiers de config réseau → **télécharge à chaque connexion internet**.
  `ThrottleInterval=120`. Pour l'usage nomade (télécharge quand on capte du WiFi).

Installation : `launchctl load -w` sur les deux.

### ⚠️ Pièges launchd rencontrés en vrai (corrigés) — indispensables pour réinstaller

1. **PATH minimal.** launchd lance avec `PATH=/usr/bin:/bin:/usr/sbin:/sbin` → `yt-dlp`
   et `aria2c` (dans `/opt/homebrew/bin`) sont **introuvables**. → `EnvironmentVariables`
   `PATH` incluant `/opt/homebrew/bin` dans les deux plists.
2. **`~/Downloads` protégé (TCC).** launchd ne peut pas y écrire son `StandardOutPath`
   → l'agent échoue en **`EX_CONFIG (78)`** sans rien logger. → logs redirigés vers
   `~/Library/Logs/ytstock.log`. (Le process Python, lui, accède bien aux vidéos.)

## Gestion des erreurs (principes)

- Le démon ne doit **jamais** mourir sur une erreur d'une source : chaque étape
  (subs, rec, search, history, download) est isolée en try/except → log + continue.
- `refill` est **idempotent** : le relancer après un crash ne crée pas de doublons
  (anti-doublon via `seen.txt` + fichiers déjà présents).
- Un ID qui échoue au download va dans `seen.txt` pour ne pas être re-tenté en boucle.
- Écritures dans `seen.txt` / `watch.json` : append/rewrite atomique (fichier temp + rename).

## Risques connus (documentés, acceptés)

1. **Cookies Chrome (macOS)** — déchiffrement via Trousseau ; Chrome parfois à fermer,
   prompt Keychain possible, YouTube peut invalider la session. Point le plus fragile.
   *Mitigation :* log clair quand les cookies échouent ; le stock local existant reste
   consultable même si le refill est en panne.
2. **Scraping `:ytrec` / `:ythistory`** — peut casser si YouTube change son HTML/API.
   Les abonnements (`:ytsubs`) et la recherche par thème sont plus stables → le système
   reste utile même si rec/history tombent.
3. **`lsof` + macOS** — certains lecteurs mappent le fichier en mémoire différemment ;
   à valider avec VLC/IINA/QuickTime réels pendant l'implémentation.

## Test (self-check)

Un `demo()` / `__main__ --self-check` à base d'`assert`, sans framework, couvrant la
logique non triviale et pure :
- extraction d'ID depuis un nom de fichier (cas valides/invalides) ;
- filtre candidats (Short, live, doublon, durée max) ;
- math du budget (savoir quand s'arrêter de télécharger) ;
- transition d'état du watcher (ouvert→fermé avec/ sans seuil atteint).

Les parties I/O réelles (yt-dlp, lsof, launchd) sont validées manuellement à
l'implémentation, pas mockées.

## Hors périmètre (YAGNI)

- Pas d'interface graphique / web.
- Pas de multi-utilisateur, pas de sync multi-machines.
- Pas de base de données (fichiers plats suffisent).
- Pas de reprise de lecture / position de visionnage (juste vue / pas vue).
- Pas de classement par intérêt / scoring ML des recommandations.
