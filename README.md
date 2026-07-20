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
- **Suppression après visionnage** : détection via `lsof` (VLC/IINA/QuickTime/mpv),
  90 s de lecture = vue → supprimée, l'espace se recycle.
- **👍 local** : liker une vidéo vue oriente les prochains téléchargements vers la
  **même chaîne** (rien n'est envoyé à YouTube, aucun compte modifié).
- **Interface web** : coller une URL pour télécharger direct, relancer un cycle,
  gérer le stock. Marche hors ligne pour tout ce qui est déjà téléchargé.
- **Application macOS** : un clic lance démon + serveur + fenêtre dédiée.

## Prérequis

- **macOS** (l'app, la détection `lsof` et l'ouverture des lecteurs sont macOS).
  Le cœur (`serve`, `daemon`) tourne partout où il y a Python 3 + yt-dlp.
- **Python 3.9+** (fourni avec macOS).
- **Homebrew** — https://brew.sh
- Un navigateur connecté à YouTube (Firefox par défaut) pour les cookies.

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

```sh
open -a ytstock                 # tout se lance : démon + serveur + fenêtre
# ou, sans l'app :
python3 ytstock.py serve        # interface web → http://127.0.0.1:8787
python3 ytstock.py daemon       # remplissage/nettoyage automatique en continu
python3 ytstock.py refill       # un seul cycle de remplissage
python3 ytstock.py status       # état du stock
python3 ytstock.py --self-check # tests intégrés
```

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
4. Le démon surveille les fichiers ouverts : une vidéo regardée puis fermée est
   marquée vue, supprimée, et son visionnage libère de la place pour la suivante.

Le tout est sérialisé par un verrou fichier (`flock`) : démon et interface ne
téléchargent jamais le même fichier en même temps.

## Licence

MIT — voir [LICENSE](LICENSE).
