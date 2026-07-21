# 🎬 ytstock

Une file de vidéos YouTube toujours prête à regarder, **hors ligne**. Un démon
maintient en permanence ~15 Go de vidéos téléchargées, classées par engagement,
et les supprime automatiquement une fois vues. Interface web locale pour lancer,
supprimer, liker et télécharger.

Un seul fichier Python, **stdlib uniquement** — aucune dépendance PyPI. Les seuls
prérequis sont des outils système (`yt-dlp`, `aria2`, `deno`).

## Fonctionnalités

- **Remplissage automatique** : le stock se recharge dès qu'il descend sous le budget.
- **Classement par engagement** : likes/vues + commentaires/vues, indépendant de la
  taille de la chaîne — une petite vidéo très aimée passe devant un gros buzz tiède.
- **Reprise + suppression quand finie** : les vidéos ne sont **jamais** supprimées
  à la fermeture. VLC reprend au bon timecode (sa position de reprise), et une
  vidéo n'est supprimée + rangée dans « Vues récemment » que lorsqu'elle est
  **finie** (position VLC à ≤ 20 s de la fin, durée via `ffprobe`).
- **👍 local** : liker une vidéo vue oriente les prochains téléchargements vers la
  **même chaîne** (rien n'est envoyé à YouTube, aucun compte modifié).
- **File d'attente + progression** : coller plusieurs liens d'affilée les
  **empile** ; un worker les télécharge un par un. Le `%` d'avancement s'affiche
  dans la fenêtre (mini-barre par vidéo) **et** dans l'icône de la barre de menu
  (`52% (2)` = 52 % sur la vidéo en cours, 2 en attente). Plus de refus « occupé ».
- **Interface web** : coller une URL pour télécharger, relancer un cycle,
  gérer le stock, suivre la file d'attente. Marche hors ligne pour tout ce qui
  est déjà téléchargé.
- **Application macOS** : un clic lance démon + serveur + fenêtre dédiée.
- **Barre de menu** : un « Y » en haut de l'écran (`ytstock-menu.app`). Copie un
  lien YouTube, clique, il l'ajoute à la file (avec choix de qualité) — sans
  ouvrir la fenêtre. Client léger qui réutilise le serveur.

## Prérequis

- **macOS** (l'app, la détection `lsof` et l'ouverture des lecteurs sont macOS).
  Le cœur (`serve`, `daemon`) tourne partout où il y a Python 3 + yt-dlp.
- **Python 3.9+** (fourni avec macOS).
- **Homebrew** — https://brew.sh
- Un navigateur connecté à YouTube (Firefox par défaut) pour les cookies.
- **VLC** comme lecteur : c'est sa position de reprise qui pilote « En cours » et
  la suppression automatique (règle VLC sur « continuer la lecture » pour éviter
  le pop-up de reprise). `ffmpeg`/`ffprobe` pour les durées.

## Installation

```sh
git clone https://github.com/MaelCP/ytstock.git
cd ytstock
./install.sh
```

`install.sh` installe `yt-dlp`, `aria2`, `deno` (et propose VLC/Firefox/Chrome),
puis construit `ytstock.app` dans `/Applications`. Relançable sans risque.

### ⚠️ Accès disque (une seule fois)

Si le dossier des vidéos est dans un emplacement protégé par macOS
(`~/Downloads`, Bureau, Documents), l'app lancée depuis le Finder est bloquée
(`Operation not permitted`). Autorise-la une fois :

**Réglages Système ▸ Confidentialité et sécurité ▸ Accès complet au disque ▸ +**
→ ajoute `/Applications/ytstock.app`, puis relance l'app.

Pour éviter cette étape, place les vidéos hors zone protégée :
`export YTSTOCK_DIR="$HOME/Movies/ytstock"`.

## Utilisation

Le plus simple, `open -a ytstock`, lance tout d'un coup. Sinon, chaque
sous-commande fait une chose :

```sh
open -a ytstock                 # tout se lance : démon + serveur + fenêtre
# ou, à la main :
python3 ytstock.py serve        # interface web → http://127.0.0.1:8787
python3 ytstock.py daemon       # remplissage/nettoyage automatique en continu
python3 ytstock.py refill       # un seul cycle de remplissage, puis s'arrête
python3 ytstock.py status       # état du stock (nombre de vidéos, Go utilisés)
python3 ytstock.py --self-check # tests intégrés
```

### `serve` vs `daemon` — qui fait quoi

Ce sont **deux processus séparés**, faits pour tourner en même temps :

- **`serve`** = l'**interface** (serveur web sur `127.0.0.1:8787`). Il sert la
  page, répond aux clics et à la barre de menu, et contient le **worker de la
  file d'attente** : c'est lui qui télécharge ce que *tu* demandes, une vidéo à
  la fois, en publiant la progression sur `GET /api/queue`.
- **`daemon`** = l'**automate de fond**. Il remplit le stock tout seul (jusqu'au
  budget disque), lit la position de reprise VLC et supprime les vidéos finies.
  Il n'a pas besoin de `serve` pour tourner.

Les deux téléchargent, donc ils se partagent un **verrou fichier** (`flock`) :
jamais deux `yt-dlp` en même temps. Si le démon télécharge, ta file attend
sagement son tour (et inversement) — aucun conflit, aucun doublon.

Pour l'usage courant tu ne lances ni l'un ni l'autre à la main : `open -a
ytstock` démarre les deux (plus la fenêtre) et ne relance que ce qui manque.

## Configuration

Deux variables d'environnement :

| Variable | Défaut | Rôle |
|---|---|---|
| `YTSTOCK_DIR` | `~/Downloads/videos` | Où stocker les vidéos et l'état |
| `YTSTOCK_COOKIES_BROWSER` | `firefox` | Navigateur source des cookies YouTube |

Les réglages plus fins sont des constantes en haut de `ytstock.py` :
budget disque (`BUDGET_BYTES`), résolution max (`MAX_HEIGHT`), durée min/max,
thèmes de recherche (`THEMES`), bonus des chaînes likées (`LIKED_BOOST`).

## Comment ça marche

1. `gather_candidates` liste des candidats (abonnements, recommandations, chaînes
   likées, recherches thématiques) via `yt-dlp --flat-playlist` (rapide).
2. `fetch_metadata` récupère like/vues/commentaires par lots et les classe.
3. `download` télécharge en ≤720p via `aria2c` tant que le budget n'est pas atteint.
   Il lit la sortie de `yt-dlp`/`aria2c` ligne par ligne pour en extraire le `%`.
4. Le démon lit la position de reprise que VLC enregistre par fichier
   (`org.videolan.vlc.plist`) : une vidéo commencée reste en stock (onglet
   « En cours »), et n'est supprimée que quand sa position atteint la fin.

### File d'attente

Quand tu colles un lien (fenêtre ou barre de menu), `POST /api/download`
**n'attend pas** : il ajoute un élément à `.ytstock/queue.json`
(`{id, quality, state, pct}`) et répond aussitôt. Le worker de `serve` prend le
prochain `pending`, le passe en `downloading` en écrivant le `%` au fil de l'eau,
puis `done`/`failed`. La fenêtre et le menu lisent `GET /api/queue` toutes les
secondes pour afficher la file et la progression. Un même lien déjà en file n'est
pas ré-empilé (dédoublonnage).

Le tout est sérialisé par un verrou fichier (`flock`) : démon, worker et
interface ne téléchargent jamais le même fichier en même temps.

## Licence

MIT — voir [LICENSE](LICENSE).
