#!/usr/bin/env python3
"""ytstock — file de vidéos YouTube toujours prête. Voir docs/superpowers/specs/."""
import sys
from pathlib import Path
import re
import json
import os
import datetime
import tempfile
import shutil
import subprocess
import time

VIDEO_DIR    = Path.home() / "Downloads" / "videos"
STATE_DIR    = VIDEO_DIR / ".ytstock"
SEEN_FILE    = STATE_DIR / "seen.txt"
WATCH_FILE   = STATE_DIR / "watch.json"
LOG_FILE     = STATE_DIR / "ytstock.log"
THUMBS_DIR   = STATE_DIR / "thumbs"          # miniatures locales (offline), 1 par id
CANDIDATES_FILE = STATE_DIR / "candidates.json"   # cache des candidats classés
FAILS_FILE   = STATE_DIR / "fails.json"      # compteur d'échecs de download par id

BUDGET_BYTES = 15 * 1024**3
MAX_HEIGHT   = 720
WATCHED_SECS = 90
MIN_DURATION = 90
MAX_DURATION = 2 * 3600            # 0 = pas de limite
POLL_SECS    = 15
HISTORY_SECS = 30 * 60
THEMES       = ["philosophie", "ARTE", "psychologie", "documentaire", "NBA"]
PLAYERS      = ["VLC", "IINA", "QuickTime", "mpv"]
VIDEO_EXTS   = {".mp4", ".webm", ".mkv"}
COOKIES_BROWSER = "firefox"       # firefox = pas de prompt Trousseau (contrairement à chrome)
METADATA_CHUNK  = 25              # nb de candidats dont on fetch les métadonnées par passe
MIN_VIEWS       = 500             # plancher : sous ça le ratio d'engagement = bruit
MIN_LIKES       = 20             # idem
SERVE_PORT      = 8787            # interface web locale


# Task 1: ID extraction
_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.[^.]+$")


def id_from_name(name):
    m = _ID_RE.search(name)
    return m.group(1) if m else None


# Task 2: Filtering candidates
def is_wanted(cand, seen):
    if cand["id"] in seen:
        return False
    if cand.get("live_status") in ("is_live", "is_upcoming"):
        return False
    d = cand.get("duration")
    if d is not None:
        if d < MIN_DURATION:
            return False
        if MAX_DURATION and d > MAX_DURATION:
            return False
    return True


# Task 3: Budget math
def list_stock_files():
    if not VIDEO_DIR.exists():
        return []
    return [p for p in VIDEO_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS]


def dir_used_bytes():
    return sum(p.stat().st_size for p in list_stock_files())


def needs_more(used):
    return used < BUDGET_BYTES


# Task 4: Persistent state
def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_seen():
    if not SEEN_FILE.exists():
        return set()
    return {ln.strip() for ln in SEEN_FILE.read_text().splitlines() if ln.strip()}


def add_seen(video_id):
    seen = load_seen()
    if video_id in seen:
        return
    with open(SEEN_FILE, "a") as f:
        f.write(video_id + "\n")


def load_watch():
    if not WATCH_FILE.exists():
        return {}
    try:
        return json.loads(WATCH_FILE.read_text())
    except (ValueError, OSError):
        return {}


def save_watch(d):
    _atomic_write(WATCH_FILE, json.dumps(d))


# Task 5: Watcher state machine
def watcher_tick(open_ids, acc):
    new_acc = {}
    watched = set()
    for vid in open_ids:
        new_acc[vid] = acc.get(vid, 0) + POLL_SECS
    for vid, secs in acc.items():
        if vid not in open_ids and secs >= WATCHED_SECS:
            watched.add(vid)
    return watched, new_acc


# Task 6: Logging and file operations
def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    try:
        ensure_state_dir()
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def path_for_id(video_id):
    for p in list_stock_files():
        if id_from_name(p.name) == video_id:
            return p
    return None


def mark_watched(video_id, reason):
    add_seen(video_id)
    p = path_for_id(video_id)
    if p and p.exists():
        p.unlink()
        log(f"watched ({reason}) -> deleted {p.name}")
    else:
        log(f"watched ({reason}) {video_id} (no local file)")


# Task 7: Candidate listing via yt-dlp
def list_source(source, limit):
    cmd = [
        "yt-dlp", "--flat-playlist", "--ignore-errors",
        "--cookies-from-browser", COOKIES_BROWSER,
        "--playlist-end", str(limit),
        "--print", "%(id)s\t%(duration)s\t%(live_status)s",
        source,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (subprocess.SubprocessError, OSError) as e:
        log(f"list_source error {source}: {e}")
        return []
    cands = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or len(parts[0]) != 11:
            continue
        vid, dur, live = parts
        duration = int(dur) if dur.isdigit() else None
        cands.append({"id": vid, "duration": duration,
                      "live_status": None if live in ("NA", "None", "") else live})
    return cands


def gather_candidates(limit_per_source):
    sources = [":ytsubs", ":ytrec"]
    sources += [f'ytsearch{limit_per_source}:{t}' for t in THEMES]
    seen_ids, out = set(), []
    for src in sources:
        for c in list_source(src, limit_per_source):
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                out.append(c)
    return out


# Task 8: Download via yt-dlp + aria2c
def download(video_id):
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(VIDEO_DIR / "%(title)s [%(id)s].%(ext)s")
    thumb_tmpl = "thumbnail:" + str(THUMBS_DIR / "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]",
        "--external-downloader", "aria2c",
        "--external-downloader-args", "aria2c:-x16 -s16 -k1M",
        "--cookies-from-browser", COOKIES_BROWSER,
        "--no-playlist",
        # rejette lives/premieres : sinon un live à durée inconnue passe le filtre
        # candidat et bloque le démon mono-thread jusqu'au timeout (1h).
        "--match-filters", "!is_live & !is_upcoming",
        # miniature locale (offline) rangée par id dans .ytstock/thumbs/
        "--write-thumbnail", "--convert-thumbnails", "jpg",
        # pas de --no-part : les .part interrompus ne comptent pas dans le budget
        # (exclus par VIDEO_EXTS) et yt-dlp reprend/nettoie proprement.
        "-o", out_tmpl,
        "-o", thumb_tmpl,
        "--", video_id,
    ]
    try:
        r = subprocess.run(cmd, timeout=3600)
        ok = r.returncode == 0
    except (subprocess.SubprocessError, OSError) as e:
        log(f"download error {video_id}: {e}")
        ok = False
    log(f"download {'ok' if ok else 'FAIL'} {video_id}")
    return ok


def seed_seen_from_disk():
    """Marque comme 'seen' les vidéos déjà présentes, pour ne pas les
    re-télécharger si elles ressurgissent comme candidates."""
    for p in list_stock_files():
        vid = id_from_name(p.name)
        if vid:
            add_seen(vid)


def sweep_stale_partials():
    """Supprime les .part/.aria2 abandonnés (download interrompu). Ils sont hors
    budget (exclus par VIDEO_EXTS) donc rempliraient le disque sans limite.
    Seuil > timeout de download : un partial 'vieux' n'est jamais actif."""
    import time as _t
    cutoff = _t.time() - 3600
    for p in VIDEO_DIR.glob("*"):
        if p.suffix in (".part", ".aria2", ".ytdl") and p.is_file():
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    log(f"swept stale partial {p.name}")
            except OSError:
                pass


def fetch_metadata(ids):
    """Extraction complète (lente, ~5s/vidéo) : like/comment/view/duration/live.
    Indispensable pour classer par engagement — indisponible en flat-playlist."""
    if not ids:
        return []
    cmd = ["yt-dlp", "--no-download", "--ignore-errors",
           "--cookies-from-browser", COOKIES_BROWSER,
           "--print", "%(id)s\t%(like_count)s\t%(comment_count)s\t%(view_count)s\t%(duration)s\t%(live_status)s\t%(title)s"]
    cmd += [f"https://www.youtube.com/watch?v={i}" for i in ids]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * len(ids))
    except (subprocess.SubprocessError, OSError) as e:
        log(f"fetch_metadata error: {e}")
        return []
    metas = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 7 or len(parts[0]) != 11:
            continue
        vid, like, com, view, dur, live, title = parts
        metas.append({
            "id": vid,
            "like": int(like) if like.isdigit() else 0,
            "comment": int(com) if com.isdigit() else 0,
            "view": int(view) if view.isdigit() else 0,
            "duration": int(dur) if dur.isdigit() else None,
            "live_status": None if live in ("NA", "None", "") else live,
            "title": title,
        })
    return metas


def engagement_score(m):
    """Taux d'engagement, indépendant de la taille de la chaîne : une petite
    vidéo très likée/commentée bat un gros buzz tiède. 0 si vues inconnues."""
    v = m["view"]
    if v <= 0:
        return 0.0
    return (m["like"] / v) * 100 + (m["comment"] / v) * 500


def enough_signal(m):
    """Écarte les vidéos trop peu vues/likées : à 24 vues le ratio explose mais
    c'est du bruit, pas une bonne vidéo. Plancher à régler via MIN_VIEWS/MIN_LIKES."""
    return m["view"] >= MIN_VIEWS and m["like"] >= MIN_LIKES


def save_candidates(metas):
    """Écrit le top 50 (non vus, triés engagement) pour l'onglet 'à télécharger'."""
    seen = load_seen()
    ranked = sorted((m for m in metas if m["id"] not in seen),
                    key=engagement_score, reverse=True)[:50]
    _atomic_write(CANDIDATES_FILE, json.dumps(ranked))


def load_candidates():
    if not CANDIDATES_FILE.exists():
        return []
    try:
        return json.loads(CANDIDATES_FILE.read_text())
    except (ValueError, OSError):
        return []


MAX_FAILS = 3   # au-delà, on abandonne un id (vidéo cassée) au lieu de boucler


def load_fails():
    if not FAILS_FILE.exists():
        return {}
    try:
        return json.loads(FAILS_FILE.read_text())
    except (ValueError, OSError):
        return {}


def record_fail(vid):
    """Download raté : on réessaiera au prochain refill (utile sur réseau instable).
    Après MAX_FAILS on abandonne (marque seen) pour ne pas boucler sur un id cassé."""
    fails = load_fails()
    fails[vid] = fails.get(vid, 0) + 1
    if fails[vid] >= MAX_FAILS:
        add_seen(vid)
        fails.pop(vid, None)
        log(f"download abandon {vid} après {MAX_FAILS} échecs")
    _atomic_write(FAILS_FILE, json.dumps(fails))


def clear_fail(vid):
    fails = load_fails()
    if fails.pop(vid, None) is not None:
        _atomic_write(FAILS_FILE, json.dumps(fails))


def refill():
    ensure_state_dir()
    seed_seen_from_disk()
    sweep_stale_partials()
    log(f"refill: {dir_used_bytes() // 1024**2} MiB / {BUDGET_BYTES // 1024**2} MiB")
    # candidats en flat (rapide) -> métadonnées par chunks (coûteux) -> classement.
    pool = [c for c in gather_candidates(40) if is_wanted(c, load_seen())]
    leftovers, i = [], 0
    while i < len(pool):
        # stop dès que le budget est plein ET qu'on a de quoi alimenter l'UI
        if not needs_more(dir_used_bytes()) and leftovers:
            break
        chunk = [c["id"] for c in pool[i:i + METADATA_CHUNK]]
        i += METADATA_CHUNK
        seen = load_seen()
        good = [m for m in fetch_metadata(chunk)
                if is_wanted(m, seen) and enough_signal(m)]
        good.sort(key=engagement_score, reverse=True)
        for m in good:
            if needs_more(dir_used_bytes()):
                log(f"refill: pick {m['id']} score={engagement_score(m):.1f} "
                    f"(likes={m['like']} coms={m['comment']} vues={m['view']})")
                if download(m["id"]):
                    add_seen(m["id"])
                    clear_fail(m["id"])
                else:
                    record_fail(m["id"])  # réessai prochain refill (réseau instable)
            else:
                leftovers.append(m)  # pas téléchargé -> candidat pour l'UI
    if i > 0:                       # on a évalué qqch (pas offline) -> rafraîchit le cache
        save_candidates(leftovers)
    log(f"refill: terminé, {dir_used_bytes() // 1024**2} MiB, {len(leftovers)} candidats en cache")


def sync_history():
    try:
        watched_online = {c["id"] for c in list_source(":ythistory", 50)}
    except Exception as e:            # best-effort, jamais fatal
        log(f"sync_history error: {e}")
        return
    local_ids = {id_from_name(p.name) for p in list_stock_files()}
    local_ids.discard(None)
    for vid in watched_online & local_ids:
        mark_watched(vid, "online")


def open_video_ids():
    # -a ANDe (liste des lecteurs) AVEC (+D dossier) : on ne compte QUE les
    # fichiers ouverts par un vrai lecteur. Sans ça, Time Machine / Spotlight /
    # iCloud — et surtout aria2c pendant un download — feraient supprimer des
    # vidéos jamais regardées.
    cmd = ["lsof", "-a"]
    for player in PLAYERS:
        cmd += ["-c", player]
    cmd += ["+D", str(VIDEO_DIR), "-F", "n"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return set()
    ids = set()
    for line in out.stdout.splitlines():
        if line.startswith("n"):
            vid = id_from_name(line[1:])
            if vid:
                ids.add(vid)
    return ids


def daemon():
    ensure_state_dir()
    log("daemon start")
    seed_seen_from_disk()
    acc = load_watch()
    last_history = 0.0
    while True:
        try:
            watched, acc = watcher_tick(open_video_ids(), acc)
            # supprimer AVANT de sauver l'accumulateur : si save_watch échoue,
            # les vidéos vues sont déjà supprimées/marquées et pas perdues.
            did_delete = bool(watched)
            for vid in watched:
                mark_watched(vid, "local")
            save_watch(acc)

            # ponytail: refill()/download() bloquent la boucle (donc la détection
            # lsof) le temps d'un téléchargement — mono-thread assumé. Passer à un
            # thread de download séparé seulement si le blocage devient gênant.
            now = time.monotonic()
            if now - last_history >= HISTORY_SECS:
                sync_history()
                refill()
                last_history = now
            elif did_delete:
                refill()
        except Exception as e:                 # le démon ne meurt jamais
            log(f"daemon loop error: {e}")
        time.sleep(POLL_SECS)


def status():
    files = list_stock_files()
    used = dir_used_bytes()
    seen = load_seen()
    try:
        r = subprocess.run(["pgrep", "-f", "ytstock.py daemon"],
                           capture_output=True, text=True)
        running = "oui" if r.stdout.strip() else "non"
    except OSError:
        running = "?"
    print(f"stock      : {len(files)} vidéos")
    print(f"disque     : {used // 1024**2} MiB / {BUDGET_BYTES // 1024**2} MiB")
    print(f"vues (seen): {len(seen)} IDs")
    print(f"démon      : {running}")


# ---------------------------------------------------------------------------
# Interface web locale (stdlib http.server, marche hors ligne pour le stock)
# ---------------------------------------------------------------------------
def title_from_name(name):
    """'Titre [id].mp4' -> 'Titre'."""
    return re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]\.[^.]+$", "", name)


def stock_items():
    items = []
    for p in sorted(list_stock_files(), key=lambda x: x.stat().st_mtime, reverse=True):
        vid = id_from_name(p.name)
        if not vid:
            continue
        items.append({
            "id": vid,
            "title": title_from_name(p.name),
            "size_mib": p.stat().st_size // 1024**2,
            "thumb": (THUMBS_DIR / f"{vid}.jpg").exists(),
        })
    return items


def backfill_thumbs():
    """Récupère les miniatures manquantes du stock (best-effort, online)."""
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    missing = [it["id"] for it in stock_items() if not it["thumb"]]
    if not missing:
        return
    cmd = ["yt-dlp", "--skip-download", "--write-thumbnail",
           "--convert-thumbnails", "jpg", "--ignore-errors",
           "--cookies-from-browser", COOKIES_BROWSER,
           "-o", "thumbnail:" + str(THUMBS_DIR / "%(id)s.%(ext)s")]
    cmd += [f"https://www.youtube.com/watch?v={i}" for i in missing]
    try:
        subprocess.run(cmd, timeout=60 * len(missing) + 30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        pass


PAGE = """<!doctype html><html lang=fr><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ytstock</title><style>
*{box-sizing:border-box}body{margin:0;font:15px/1.4 -apple-system,system-ui,sans-serif;
background:#111;color:#eee}header{position:sticky;top:0;background:#181818;
padding:14px 20px;border-bottom:1px solid #2a2a2a;display:flex;gap:16px;align-items:center}
h1{font-size:17px;margin:0;font-weight:600}.bar{color:#9a9a9a;font-size:13px}
.bar b{color:#eee}main{padding:20px;max-width:1200px;margin:0 auto}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:#8a8a8a;
margin:26px 0 12px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));
gap:16px}.card{background:#1c1c1c;border:1px solid #2a2a2a;border-radius:10px;overflow:hidden;
display:flex;flex-direction:column}.thumb{aspect-ratio:16/9;background:#000;object-fit:cover;
width:100%;display:block}.ph{aspect-ratio:16/9;background:linear-gradient(135deg,#242424,#161616);
display:flex;align-items:center;justify-content:center;color:#444;font-size:32px}
.body{padding:10px 12px;flex:1;display:flex;flex-direction:column;gap:8px}
.t{font-size:13px;font-weight:500;line-height:1.35;max-height:3.4em;overflow:hidden}
.meta{font-size:12px;color:#8a8a8a}.row{display:flex;gap:8px;margin-top:auto}
button{flex:1;border:0;border-radius:7px;padding:8px;font-size:13px;font-weight:600;
cursor:pointer;color:#fff}.play{background:#2d6cdf}.del{background:#3a2020;color:#e88;flex:0 0 auto}
.dl{background:#227a4b}button:disabled{opacity:.4;cursor:not-allowed}
.empty{color:#666;padding:20px 0}.off{color:#c96;font-size:12px}
</style></head><body>
<header><h1>🎬 ytstock</h1><div class=bar id=bar>…</div></header>
<main>
<h2>En stock — à regarder</h2><div class=grid id=stock></div>
<h2>À télécharger — les plus engageantes <span class=off id=offnote></span></h2>
<div class=grid id=cand></div>
</main>
<script>
const online = navigator.onLine;
async function j(u,o){const r=await fetch(u,o);return r.json()}
function card(html){const d=document.createElement('div');d.className='card';d.innerHTML=html;return d}
function thumbStock(it){return it.thumb?`<img class=thumb src="/thumb/${it.id}">`:`<div class=ph>🎬</div>`}
async function post(u){try{const r=await j(u,{method:'POST'});return r}catch(e){return {ok:false,err:'offline'}}}
async function load(){
 const st=await j('/api/stock');
 const el=document.getElementById('stock');el.innerHTML='';
 let mib=0;
 st.forEach(it=>{mib+=it.size_mib;
  const c=card(`${thumbStock(it)}<div class=body><div class=t>${esc(it.title)}</div>
   <div class=meta>${it.size_mib} Mo</div>
   <div class=row><button class=play>▶ Lancer</button><button class=del>🗑</button></div></div>`);
  c.querySelector('.play').onclick=async e=>{e.target.textContent='…';await post('/api/open?id='+it.id);e.target.textContent='▶ Lancer'};
  c.querySelector('.del').onclick=async()=>{if(confirm('Supprimer « '+it.title+' » ?')){await post('/api/delete?id='+it.id);load()}};
  el.appendChild(c)});
 if(!st.length)el.innerHTML='<div class=empty>Rien en stock pour l\\'instant.</div>';
 document.getElementById('bar').innerHTML=`<b>${st.length}</b> vidéos · <b>${(mib/1024).toFixed(1)}</b> Go`;
 const cd=await j('/api/candidates');
 const ce=document.getElementById('cand');ce.innerHTML='';
 document.getElementById('offnote').textContent=online?'':'(hors ligne — téléchargement indispo)';
 cd.forEach(m=>{
  const c=card(`<img class=thumb src="https://i.ytimg.com/vi/${m.id}/mqdefault.jpg" onerror="this.outerHTML='<div class=ph>🎬</div>'">
   <div class=body><div class=t>${esc(m.title||m.id)}</div>
   <div class=meta>❤ ${m.like} · 💬 ${m.comment} · 👁 ${m.view}</div>
   <div class=row><button class=dl ${online?'':'disabled'}>⬇ Télécharger</button></div></div>`);
  const b=c.querySelector('.dl');
  if(online)b.onclick=async()=>{b.textContent='téléchargement…';b.disabled=true;const r=await post('/api/download?id='+m.id);if(r.ok){load()}else{b.textContent='échec';setTimeout(()=>{b.textContent='⬇ Télécharger';b.disabled=false},2000)}};
  ce.appendChild(c)});
 if(!cd.length)ce.innerHTML='<div class=empty>Pas encore de candidats (lance un refill en ligne).</div>';
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
load();
</script></body></html>"""


def serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    import threading
    ensure_state_dir()
    # backfill des miniatures en tâche de fond : ne bloque pas le démarrage du
    # serveur (sinon l'UI met 30-60s à répondre le temps de récupérer les images).
    threading.Thread(target=backfill_thumbs, daemon=True).start()

    def valid_id(v):
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", v or ""))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _bytes(self, data, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._bytes(json.dumps(obj).encode(), "application/json", code)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._bytes(PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/stock":
                self._json(stock_items())
            elif path == "/api/candidates":
                self._json(load_candidates())
            elif path.startswith("/thumb/"):
                vid = path[len("/thumb/"):]
                f = THUMBS_DIR / f"{vid}.jpg"
                if valid_id(vid) and f.exists():
                    self._bytes(f.read_bytes(), "image/jpeg")
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):
            # anti-CSRF simple : un autre site enverrait un Origin différent
            origin = self.headers.get("Origin")
            allowed = (None, f"http://127.0.0.1:{SERVE_PORT}",
                       f"http://localhost:{SERVE_PORT}")
            if origin not in allowed:
                return self.send_error(403)
            path = urlparse(self.path).path
            vid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            if not valid_id(vid):
                return self._json({"ok": False, "err": "bad id"}, 400)
            if path == "/api/open":
                p = path_for_id(vid)
                if p:
                    subprocess.Popen(["open", "-a", "VLC", str(p)])
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "err": "not found"}, 404)
            elif path == "/api/delete":
                mark_watched(vid, "ui")
                self._json({"ok": True})
            elif path == "/api/download":
                ok = download(vid)
                if ok:
                    add_seen(vid)
                    clear_fail(vid)
                else:
                    record_fail(vid)
                self._json({"ok": ok})
            else:
                self.send_error(404)

    srv = ThreadingHTTPServer(("127.0.0.1", SERVE_PORT), H)
    url = f"http://127.0.0.1:{SERVE_PORT}"
    log(f"serve: {url}")
    print(f"ytstock UI → {url}  (Ctrl-C pour arrêter)")
    try:
        subprocess.Popen(["open", url])
    except OSError:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def run_self_check():
    # Task 1: id_from_name
    assert id_from_name("History of the world [xuCn8ux2gbs].mp4") == "xuCn8ux2gbs"
    assert id_from_name("La honte ｜ ARTE [0z8W4XQ6KOo].webm") == "0z8W4XQ6KOo"
    assert id_from_name("no id here.mp4") is None
    assert id_from_name("[tooShort].mp4") is None
    assert id_from_name("weird [xuCn8ux2gbs] middle.mp4") is None  # ID not just before ext

    # Task 2: is_wanted
    seen = {"aaaaaaaaaaa"}
    assert is_wanted({"id": "bbbbbbbbbbb", "duration": 600, "live_status": None}, seen) is True
    assert is_wanted({"id": "aaaaaaaaaaa", "duration": 600, "live_status": None}, seen) is False   # duplicate
    assert is_wanted({"id": "ccccccccccc", "duration": 30, "live_status": None}, seen) is False     # short
    assert is_wanted({"id": "ddddddddddd", "duration": 99999, "live_status": None}, seen) is False   # too long
    assert is_wanted({"id": "eeeeeeeeeee", "duration": 600, "live_status": "is_live"}, seen) is False # live
    assert is_wanted({"id": "fffffffffff", "duration": None, "live_status": None}, seen) is True      # unknown duration

    # engagement_score : ratio, indépendant de la taille de la chaîne
    assert engagement_score({"like": 10, "comment": 0, "view": 0}) == 0.0  # pas de /0
    hot = engagement_score({"like": 125, "comment": 32, "view": 2570})     # petite très engageante
    meh = engagement_score({"like": 50000, "comment": 100, "view": 5000000})  # gros buzz tiède
    assert hot > meh
    # plancher de signal : écarte le bruit à ~24 vues, garde les vidéos crédibles
    assert enough_signal({"like": 125, "comment": 32, "view": 2570}) is True
    assert enough_signal({"like": 4, "comment": 1, "view": 24}) is False

    # title_from_name : retire le suffixe [id].ext pour l'UI
    assert title_from_name("La honte ｜ ARTE [0z8W4XQ6KOo].webm") == "La honte ｜ ARTE"
    assert title_from_name("no id here.mp4") == "no id here.mp4"

    # Task 3: needs_more
    assert needs_more(0) is True
    assert needs_more(BUDGET_BYTES - 1) is True
    assert needs_more(BUDGET_BYTES) is False
    assert needs_more(BUDGET_BYTES + 1) is False

    # Task 4 & 6: Persistent state and mark_watched (nested in temp dir)
    global STATE_DIR, SEEN_FILE, WATCH_FILE, VIDEO_DIR, FAILS_FILE
    _saved_state = (STATE_DIR, SEEN_FILE, WATCH_FILE, FAILS_FILE)
    _saved_vd = VIDEO_DIR
    _tmp = Path(tempfile.mkdtemp())
    STATE_DIR, SEEN_FILE, WATCH_FILE = _tmp, _tmp / "seen.txt", _tmp / "watch.json"
    FAILS_FILE = _tmp / "fails.json"
    VIDEO_DIR = _tmp
    try:
        ensure_state_dir()
        assert load_seen() == set()
        add_seen("aaaaaaaaaaa")
        add_seen("aaaaaaaaaaa")   # duplicate ignored
        add_seen("bbbbbbbbbbb")
        assert load_seen() == {"aaaaaaaaaaa", "bbbbbbbbbbb"}
        assert SEEN_FILE.read_text().count("aaaaaaaaaaa") == 1
        save_watch({"ccccccccccc": 45})
        assert load_watch() == {"ccccccccccc": 45}
        assert load_watch() != {}  # persists

        # Task 6: mark_watched
        f = _tmp / "Titre [ddddddddddd].mp4"
        f.write_text("x")
        assert path_for_id("ddddddddddd") == f
        mark_watched("ddddddddddd", "test")
        assert not f.exists()
        assert "ddddddddddd" in load_seen()

        # seed_seen_from_disk : un fichier présent est marqué seen
        (_tmp / "Autre [eeeeeeeeeee].mp4").write_text("x")
        seed_seen_from_disk()
        assert "eeeeeeeeeee" in load_seen()

        # retry : échec -> compte, pas seen ; MAX_FAILS -> abandon (seen)
        for _ in range(MAX_FAILS - 1):
            record_fail("ggggggggggg")
        assert "ggggggggggg" not in load_seen()       # encore réessayable
        assert load_fails().get("ggggggggggg") == MAX_FAILS - 1
        record_fail("ggggggggggg")                     # atteint le seuil
        assert "ggggggggggg" in load_seen()            # abandonné
        assert "ggggggggggg" not in load_fails()
        record_fail("hhhhhhhhhhh"); clear_fail("hhhhhhhhhhh")
        assert "hhhhhhhhhhh" not in load_fails()        # succès efface le compteur
    finally:
        STATE_DIR, SEEN_FILE, WATCH_FILE, FAILS_FILE = _saved_state
        VIDEO_DIR = _saved_vd
        shutil.rmtree(_tmp, ignore_errors=True)

    # Task 5: watcher_tick
    # opening progressively
    watched, acc = watcher_tick({"a"}, {})
    assert watched == set() and acc == {"a": POLL_SECS}
    # continue until threshold (POLL_SECS=15 → 6 ticks = 90s)
    for _ in range(5):
        watched, acc = watcher_tick({"a"}, acc)
    assert acc["a"] >= WATCHED_SECS and watched == set()
    # close after threshold reached => watched
    watched, acc = watcher_tick(set(), acc)
    assert watched == {"a"} and "a" not in acc
    # close before threshold => not watched, forgotten
    _, acc2 = watcher_tick({"b"}, {})           # b at 15s only
    watched, acc2 = watcher_tick(set(), acc2)
    assert watched == set() and "b" not in acc2

    print("self-check: OK")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "--self-check":
        run_self_check()
    elif cmd == "refill":
        refill()
    elif cmd == "daemon":
        daemon()
    elif cmd == "status":
        status()
    elif cmd == "serve":
        serve()
    else:
        print("usage: ytstock.py [daemon|refill|status|serve|--self-check]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
