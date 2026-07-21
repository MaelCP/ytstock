# File d'attente + progression — design

## Problème
Coller une URL pendant un téléchargement renvoie ⏳ « occupé » : le clic est
refusé. Et aucun retour de progression : l'icône du menu clignote 2 s, l'UI web
ne montre rien. On veut empiler les URLs et voir l'avancement (menu + UI web).

## Approche
Une file d'attente FIFO qui **absorbe** la progression : le `%` devient un champ
de l'élément en cours. `/api/download` n'attend plus — il empile et répond tout
de suite. Un worker draine la file une vidéo à la fois.

## Composants

### Store — `.ytstock/queue.json`
Liste d'items `{id, quality, title, state, pct}`, `state` ∈
`pending|downloading|done|failed`. Fichier (pas variable en mémoire) pour
survivre à un redémarrage et rester inspectable. Écriture atomique
(`_atomic_write` existant), accès sérialisé par un `threading.Lock`.
Chemin calculé depuis `STATE_DIR` à l'appel (compatible self-check).

### Worker — 1 thread dans le processus `serve`
Boucle : prend le prochain `pending` → tente `download_lock` (flock, non
bloquant ; si le démon l'a, réessaie dans 2 s) → passe l'item `downloading` →
`download(id, quality, on_pct=...)` qui met à jour `pct` → `done`/`failed` →
suivant. File vide → `sleep(1)`. Le flock (par-fd) sérialise worker, refill de
`serve` et démon : jamais deux yt-dlp.

### `download(video_id, quality=None, on_pct=None)`
`subprocess.run` → `Popen(stdout=PIPE, stderr=STDOUT)`, lecture ligne par ligne,
`_parse_pct(line)` (regex `(\d{1,3})(\.\d+)?%`, attrape aria2c `(98%)` comme
yt-dlp `[download] 98%`), rappel `on_pct` throttlé (~2×/s). Commande yt-dlp +
aria2c **inchangée**. `on_pct=None` (démon) → aucun effet.

### Endpoints
- `POST /api/download?url=&q=` : valide l'id → empile → `{ok, queued, n}`. Plus
  jamais de refus « busy ».
- `GET /api/queue` : renvoie la liste. Source unique file + progression.

### UI web
Section « File d'attente » sous le header, visible seulement s'il y a des items
actifs. Mini-barre sur celui en cours. Poll `/api/queue` toutes les 1 s ; quand
le nombre d'actifs baisse, `load()` (la vidéo finie apparaît dans le stock).

### Menu bar (`ytstock-menu.swift`)
Le POST empile instantané. Un `Timer` (1 s) interroge `/api/queue` et met
`52% (2)` dans le titre = % courant + nb en attente. File vide → `Y`.

## Décisions (v1)
- Titre d'item = l'id brut tant qu'on n'a pas mieux (pas d'appel yt-dlp
  supplémentaire pour le titre). Nicety future.
- Pas de bouton « retirer de la file » (YAGNI, +5 lignes plus tard).
- Les téléchargements auto du démon n'apparaissent pas dans la file (elle ne
  montre que les ajouts utilisateur) ; s'ils tournent, la file attend le verrou.
- Dédoublonnage : un id déjà `pending`/`downloading` n'est pas ré-empilé.

## Test
Dans `run_self_check` : asserts sur `_parse_pct` (aria2c, yt-dlp, ligne sans %)
et un round-trip `queue_add`/`load_queue`/`queue_update` sur un `STATE_DIR` tmp.
