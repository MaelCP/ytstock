# Serveur MCP ytstock (Claude Code) — design

## Problème
On veut dire à Claude Code « télécharge cette vidéo » ou « télécharge des
vidéos sur la guerre » et qu'il le fasse via un outil, sans copier-coller dans
l'UI. ytstock n'expose aujourd'hui qu'une UI web + une barre de menu.

## Approche
Un **serveur MCP stdio** séparé (`mcp/ytstock_mcp.py`), **stdlib uniquement**,
qui ne fait que taper sur l'**API HTTP locale déjà existante** (`:8787`). Aucune
modification de `ytstock.py` : 100 % additif, ne casse rien. Non installé = rien
ne change.

## Protocole
JSON-RPC 2.0 en lignes sur stdin/stdout (transport stdio MCP). Méthodes gérées :
`initialize` (renvoie le protocolVersion du client + `capabilities.tools`),
`notifications/initialized` (ignorée), `tools/list`, `tools/call`, `ping`.
Erreur d'outil → `content` texte avec `isError` (pas une erreur protocole).

## Outils
| Outil | Endpoint interne | Rôle |
|---|---|---|
| `download(url, quality?)` | `POST /api/download?url=&q=` | une URL/id → file |
| `search_and_download(query, count=5, quality?)` | `yt-dlp ytsearchN` puis `POST /api/download?id=` | thème → N liens en file |
| `queue_status()` | `GET /api/queue` | ce qui télécharge, le %, l'attente |
| `list_stock()` | `GET /api/stock` | vidéos prêtes (id + titre) |
| `play(id)` | `POST /api/open?id=` | ouvre dans VLC |

## Détails
- **Header `Origin: http://127.0.0.1:8787`** sur chaque requête → passe le garde
  anti-CSRF du serveur sans le modifier.
- **Auto-démarrage** : si le serveur est injoignable (download/play/search), on
  lance `open -a ytstock` et on attend que `/api/queue` réponde, puis on réessaie.
  Donc les commandes marchent même app fermée.
- **Recherche** via `yt-dlp "ytsearchN:query" --flat-playlist --print
  "%(id)s\t%(title)s"` (rapide, métadonnées seules). count borné [1, 20].
- **Qualité** validée contre `{max,1080,720,480,360,audio}` ; sinon défaut serveur.
- Config Claude Code : `claude mcp add ytstock -- python3 <repo>/mcp/ytstock_mcp.py`.
- `YTSTOCK_MCP_BASE` (env) surcharge l'URL de base pour les tests.

## Test
`python3 mcp/ytstock_mcp.py --self-check` : monte un faux serveur HTTP local,
vérifie que `download`/`queue_status` tapent le bon endpoint **avec le header
Origin canonique**, que le filtre qualité rejette une valeur inconnue, et que le
parseur de recherche (`id\ttitle`) rend les bons couples.

## Décisions (v1)
- stdlib fait-main plutôt que le SDK MCP (garde le zéro-dépendance du projet).
- `search_and_download` fait la recherche dans le MCP via yt-dlp (pas de nouvel
  endpoint serveur) — ytstock.py reste intouché.
- Pas de mock de yt-dlp au self-check (la logique risquée testée = HTTP/Origin).
