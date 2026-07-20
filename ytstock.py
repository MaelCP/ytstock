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
import fcntl
import contextlib

VIDEO_DIR    = Path(os.environ.get("YTSTOCK_DIR", str(Path.home() / "Downloads" / "videos")))
STATE_DIR    = VIDEO_DIR / ".ytstock"
SEEN_FILE    = STATE_DIR / "seen.txt"
WATCH_FILE   = STATE_DIR / "watch.json"
LOG_FILE     = STATE_DIR / "ytstock.log"
THUMBS_DIR   = STATE_DIR / "thumbs"          # miniatures locales (offline), 1 par id
CANDIDATES_FILE = STATE_DIR / "candidates.json"   # cache des candidats classés
FAILS_FILE   = STATE_DIR / "fails.json"      # compteur d'échecs de download par id
HISTORY_FILE = STATE_DIR / "history.json"    # dernières vues : notables après suppression
LIKES_FILE   = STATE_DIR / "likes.json"      # 👍 LOCAUX -> privilégie ces chaînes
DISLIKES_FILE = STATE_DIR / "dislikes.json"  # 👎 LOCAUX -> évite ces chaînes

BUDGET_BYTES = 15 * 1024**3
MAX_HEIGHT   = 720
WATCHED_SECS = 90
MIN_DURATION = 90
MAX_DURATION = 2 * 3600            # 0 = pas de limite
POLL_SECS    = 15
HISTORY_SECS = 30 * 60
THEMES       = ["philosophie", "ARTE", "psychologie", "documentaire", "NBA"]
PLAYERS      = ["VLC", "IINA", "QuickTime", "mpv"]
VIDEO_EXTS   = {".mp4", ".webm", ".mkv", ".m4a"}   # .m4a = téléchargements audio-only
COOKIES_BROWSER = os.environ.get("YTSTOCK_COOKIES_BROWSER", "firefox")  # firefox = pas de prompt Trousseau (contrairement à chrome)
METADATA_CHUNK  = 25              # nb de candidats dont on fetch les métadonnées par passe
MIN_VIEWS       = 500             # plancher : sous ça le ratio d'engagement = bruit
MIN_LIKES       = 20             # idem
SERVE_PORT      = 8787            # interface web locale
HISTORY_MAX     = 30              # nb de vidéos vues gardées en historique
LIKED_BOOST     = 1.6             # bonus de score pour une chaîne likée — à régler à l'usage
LIKED_CHANNELS_MAX = 10           # nb de chaînes likées utilisées comme sources


# Task 1: ID extraction
_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.[^.]+$")


def id_from_name(name):
    m = _ID_RE.search(name)
    return m.group(1) if m else None


def is_valid_id(v):
    """Vrai id YouTube : 11 chars du charset attendu. Bloque tout id hostile
    (injection HTML/URL) dès l'ingestion des métadonnées."""
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", v or ""))


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
    # tmp unique (mkstemp) : deux écrivains concurrents ne partagent pas le même
    # fichier temp -> pas de FileNotFoundError sur os.replace, pas de perte.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def _load_json(path, default):
    """Lecture tolérante d'un fichier d'état : absent ou corrompu -> valeur par
    défaut. Un état abîmé ne doit jamais tuer le démon."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return default


def load_watch():
    return _load_json(WATCH_FILE, {})


def save_watch(d):
    _atomic_write(WATCH_FILE, json.dumps(d))


# Task 5: Watcher state machine
def watcher_tick(open_ids, acc):
    """Accumule le temps de visionnage PAR vidéo, persistant entre ouvertures.
    Une vidéo est 'vue' quand elle a cumulé WATCHED_SECS ET qu'elle est refermée
    (jamais pendant la lecture). Un visionnage partiel reste dans acc -> 'en cours',
    et le temps s'additionne à la reprise."""
    new_acc = dict(acc)
    for vid in open_ids:
        new_acc[vid] = new_acc.get(vid, 0) + POLL_SECS
    watched = {vid for vid, secs in new_acc.items()
               if vid not in open_ids and secs >= WATCHED_SECS}
    for vid in watched:
        new_acc.pop(vid, None)
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


def load_history():
    return _load_json(HISTORY_FILE, [])


def record_history(video_id, title):
    """Trace des vidéos vues. Le fichier vidéo est supprimé juste après, mais on
    doit pouvoir liker la vidéo APRÈS coup (la miniature, elle, survit)."""
    hist = [h for h in load_history() if h.get("id") != video_id]
    hist.insert(0, {"id": video_id, "title": title,
                    "ts": datetime.datetime.now().isoformat(timespec="seconds")})
    _atomic_write(HISTORY_FILE, json.dumps(hist[:HISTORY_MAX]))


def mark_watched(video_id, reason):
    add_seen(video_id)
    p = path_for_id(video_id)
    # titre relevé AVANT l'unlink : c'est la dernière occasion de le connaître
    record_history(video_id, title_from_name(p.name) if p else video_id)
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
        if len(parts) != 3 or not is_valid_id(parts[0]):
            continue
        vid, dur, live = parts
        duration = int(dur) if dur.isdigit() else None
        cands.append({"id": vid, "duration": duration,
                      "live_status": None if live in ("NA", "None", "") else live})
    return cands


def gather_candidates(limit_per_source):
    sources = [":ytsubs", ":ytrec"]
    # ponytail: "similaire à ce que j'aime" = même chaîne que les vidéos likées.
    # C'est le seul signal de similarité que YouTube expose déjà sous forme de
    # liste, sans modèle ni dépendance. Passer aux mots-clés du titre seulement
    # si ça se révèle trop étroit.
    sources += [f"https://www.youtube.com/channel/{c}/videos" for c in liked_channels()]
    sources += [f'ytsearch{limit_per_source}:{t}' for t in THEMES]
    seen_ids, out = set(), []
    for src in sources:
        for c in list_source(src, limit_per_source):
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                out.append(c)
    return out


# Qualités proposées dans l'onglet "télécharger par URL" (façon convertisseur).
# Clé -> arguments de sélection de format yt-dlp. "audio" = extraction m4a.
QUALITIES = {
    "max":   ["-f", "bv*+ba/b"],
    "1080":  ["-f", "bv*[height<=1080]+ba/b[height<=1080]"],
    "720":   ["-f", "bv*[height<=720]+ba/b[height<=720]"],
    "480":   ["-f", "bv*[height<=480]+ba/b[height<=480]"],
    "360":   ["-f", "bv*[height<=360]+ba/b[height<=360]"],
    "audio": ["-f", "ba/b", "-x", "--audio-format", "m4a"],
}


# Task 8: Download via yt-dlp + aria2c
def download(video_id, quality=None):
    """quality=None -> plafond démon (MAX_HEIGHT, pour le budget). Sinon un choix
    utilisateur de QUALITIES (résolution ou audio)."""
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(VIDEO_DIR / "%(title)s [%(id)s].%(ext)s")
    thumb_tmpl = "thumbnail:" + str(THUMBS_DIR / "%(id)s.%(ext)s")
    fmt = QUALITIES.get(quality) or \
        ["-f", f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]"]
    cmd = [
        "yt-dlp",
        *fmt,
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
           "--print", "%(id)s\t%(like_count)s\t%(comment_count)s\t%(view_count)s\t%(duration)s\t%(live_status)s\t%(channel_id)s\t%(title)s"]
    cmd += [f"https://www.youtube.com/watch?v={i}" for i in ids]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * len(ids))
    except (subprocess.SubprocessError, OSError) as e:
        log(f"fetch_metadata error: {e}")
        return []
    metas = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8 or not is_valid_id(parts[0]):
            continue
        vid, like, com, view, dur, live, chan, title = parts
        metas.append({
            "id": vid,
            "like": int(like) if like.isdigit() else 0,
            "comment": int(com) if com.isdigit() else 0,
            "view": int(view) if view.isdigit() else 0,
            "duration": int(dur) if dur.isdigit() else None,
            "live_status": None if live in ("NA", "None", "") else live,
            "channel": chan if is_valid_channel(chan) else None,
            "title": title,
        })
    return metas


_CHANNEL_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")


def is_valid_channel(c):
    """Un id de chaîne part dans une URL construite : on le valide comme un id
    vidéo, à l'entrée."""
    return bool(_CHANNEL_RE.fullmatch(c or ""))


def load_likes():
    return _load_json(LIKES_FILE, {})


def load_dislikes():
    return _load_json(DISLIKES_FILE, {})


# Notes LOCALES (👍/👎) : rien n'est envoyé à YouTube. Elles orientent seulement
# les prochains téléchargements — like privilégie une chaîne, dislike l'évite.
def _add_rating(path, video_id, title):
    d = _load_json(path, {})
    if video_id in d:
        return
    d[video_id] = {"title": title, "channel": None,
                   "ts": datetime.datetime.now().isoformat(timespec="seconds")}
    _atomic_write(path, json.dumps(d))


def add_like(video_id, title):
    _add_rating(LIKES_FILE, video_id, title)


def add_dislike(video_id, title):
    _add_rating(DISLIKES_FILE, video_id, title)


def _resolve_channel(path, video_id):
    """Récupère la chaîne d'une vidéo notée — le signal de similarité. Lent
    (~5 s) donc appelé hors du chemin HTTP. Hors ligne -> None, réessayé au
    refill suivant."""
    cmd = ["yt-dlp", "--no-download", "--ignore-errors",
           "--cookies-from-browser", COOKIES_BROWSER,
           "--print", "%(channel_id)s",
           f"https://www.youtube.com/watch?v={video_id}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as e:
        log(f"resolve channel error {video_id}: {e}")
        return
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    cid = lines[0] if lines else ""
    if not is_valid_channel(cid):
        return
    d = _load_json(path, {})
    if video_id in d:
        d[video_id]["channel"] = cid
        _atomic_write(path, json.dumps(d))
        log(f"note: {video_id} -> chaîne {cid}")


def resolve_rating_channel(kind, video_id):
    _resolve_channel(LIKES_FILE if kind == "like" else DISLIKES_FILE, video_id)


def _rated_channels(path):
    """Chaînes notées, les plus récentes d'abord, dédupliquées."""
    entries = sorted(_load_json(path, {}).values(),
                     key=lambda l: l.get("ts") or "", reverse=True)
    out = []
    for e in entries:
        c = e.get("channel")
        if c and c not in out:
            out.append(c)
        if len(out) >= LIKED_CHANNELS_MAX:
            break
    return out


def liked_channels():
    return _rated_channels(LIKES_FILE)


def disliked_channels():
    return _rated_channels(DISLIKES_FILE)


def backfill_rating_channels():
    """Résout les chaînes des notes faites hors ligne (best-effort)."""
    for path in (LIKES_FILE, DISLIKES_FILE):
        for vid, e in _load_json(path, {}).items():
            if not e.get("channel"):
                _resolve_channel(path, vid)


def engagement_score(m):
    """Taux d'engagement, indépendant de la taille de la chaîne : une petite
    vidéo très likée/commentée bat un gros buzz tiède. 0 si vues inconnues."""
    v = m["view"]
    if v <= 0:
        return 0.0
    return (m["like"] / v) * 100 + (m["comment"] / v) * 500


def ranked_score(m, liked):
    """Score de classement = engagement + bonus si la vidéo vient d'une chaîne
    likée. engagement_score reste pur (ratio seul) pour rester testable."""
    s = engagement_score(m)
    return s * LIKED_BOOST if m.get("channel") in liked else s


def enough_signal(m):
    """Écarte les vidéos trop peu vues/likées : à 24 vues le ratio explose mais
    c'est du bruit, pas une bonne vidéo. Plancher à régler via MIN_VIEWS/MIN_LIKES."""
    return m["view"] >= MIN_VIEWS and m["like"] >= MIN_LIKES


def save_candidates(metas):
    """Écrit le top 50 (non vus, triés engagement) pour l'onglet 'à télécharger'."""
    seen = load_seen()
    liked = liked_channels()
    disliked = disliked_channels()
    ranked = sorted((m for m in metas
                     if m["id"] not in seen and m.get("channel") not in disliked),
                    key=lambda m: ranked_score(m, liked), reverse=True)[:50]
    _atomic_write(CANDIDATES_FILE, json.dumps(ranked))


def load_candidates():
    return _load_json(CANDIDATES_FILE, [])


MAX_FAILS = 3   # au-delà, on abandonne un id (vidéo cassée) au lieu de boucler


def load_fails():
    return _load_json(FAILS_FILE, {})


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


@contextlib.contextmanager
def download_lock():
    """Sérialise TOUT ce qui télécharge (refill démon/netrefill ET download UI),
    sinon deux yt-dlp écrivent le même fichier. Non-bloquant : yield False si occupé."""
    ensure_state_dir()
    lock = open(STATE_DIR / "refill.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def refill():
    with download_lock() as got:
        if not got:
            log("refill: déjà en cours ailleurs, skip")
            return
        _refill()


def _refill():
    seed_seen_from_disk()
    sweep_stale_partials()
    backfill_rating_channels()   # notes faites hors ligne : chaîne encore inconnue
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
        liked = liked_channels()
        disliked = disliked_channels()
        good = [m for m in fetch_metadata(chunk)
                if is_wanted(m, seen) and enough_signal(m)
                and m.get("channel") not in disliked]
        good.sort(key=lambda m: ranked_score(m, liked), reverse=True)
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
    if leftovers:                   # ne vide pas le cache pendant le remplissage / offline
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
            # purge des vidéos disparues du disque (supprimées via l'UI, etc.),
            # sinon acc accumule des 'en cours' fantômes.
            on_disk = {id_from_name(p.name) for p in list_stock_files()}
            acc = {v: s for v, s in acc.items() if v in on_disk}
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


_URL_ID_RE = re.compile(r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})")
_YT_HOST_RE = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/")


def id_from_url(text):
    """Id d'une URL YouTube collée (watch?v=, youtu.be, /shorts/, /live/) ou id
    brut. None si rien de valide -> le serveur refuse. On exige un hôte YouTube :
    sinon n'importe quel '?v=' de 11 caractères passerait."""
    text = (text or "").strip()
    if is_valid_id(text):
        return text
    if not _YT_HOST_RE.match(text):
        return None
    m = _URL_ID_RE.search(text)
    return m.group(1) if m else None


def is_busy():
    """Vrai si un refill/download tient le verrou. Sonde non bloquante : on prend
    le verrou et on le rend aussitôt."""
    with download_lock() as got:
        return not got


def history_items():
    likes = load_likes()
    dislikes = load_dislikes()
    return [{**h,
             "liked": h.get("id") in likes,
             "disliked": h.get("id") in dislikes,
             "thumb": (THUMBS_DIR / f"{h.get('id')}.jpg").exists()}
            for h in load_history()]


def stock_items():
    acc = load_watch()   # temps de visionnage cumulé -> distingue "en cours"
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
            "watched_secs": int(acc.get(vid, 0)),   # >0 = commencée, non finie
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
*{box-sizing:border-box}body{margin:0;font:15px/1.4 -apple-system,system-ui,sans-serif;background:#111;color:#eee}
header{position:sticky;top:0;z-index:20;background:#181818;padding:12px 18px;border-bottom:1px solid #2a2a2a;display:flex;gap:14px;align-items:center}
h1{font-size:17px;margin:0;font-weight:600}
.burger{background:none;border:0;color:#eee;font-size:22px;cursor:pointer;padding:2px 6px;line-height:1;flex:0 0 auto}
.bar{color:#9a9a9a;font-size:13px;margin-left:auto}.bar b{color:#eee}
main{padding:20px;max-width:1200px;margin:0 auto}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.05em;color:#8a8a8a;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:16px}
.card{background:#1c1c1c;border:1px solid #2a2a2a;border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.thumb{aspect-ratio:16/9;background:#000;object-fit:cover;width:100%;display:block}
.ph{aspect-ratio:16/9;background:linear-gradient(135deg,#242424,#161616);display:flex;align-items:center;justify-content:center;color:#444;font-size:32px}
.body{padding:10px 12px;flex:1;display:flex;flex-direction:column;gap:8px}
.t{font-size:13px;font-weight:500;line-height:1.35;max-height:3.4em;overflow:hidden}
.meta{font-size:12px;color:#8a8a8a}.row{display:flex;gap:8px;margin-top:auto}
button{font-family:inherit;border:0;cursor:pointer}
.act{flex:1;border-radius:7px;padding:8px;font-size:13px;font-weight:600;color:#fff}
.play{background:#2d6cdf}.resume{background:#7a4a12}
.del{background:#3a2020;color:#e88;border-radius:7px;font-size:15px;padding:6px 12px;line-height:1;flex:0 0 auto}
.dl{background:#227a4b;color:#fff;border-radius:7px;font-size:16px;padding:6px 12px;line-height:1;flex:0 0 auto}
.lk,.dk{background:#2a2a2a;color:#eee;border-radius:7px;font-size:15px;padding:6px 12px;line-height:1}
.lk.on{background:#1f4d33;color:#8fe0b0}.dk.on{background:#4d1f1f;color:#e08f8f}
button:disabled{opacity:.45;cursor:not-allowed}
.empty{color:#666;padding:20px 0}.off{color:#c96;font-size:12px}
.ovl{position:fixed;inset:0;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:.2s;z-index:30}
.ovl.show{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;left:0;bottom:0;width:270px;background:#1a1a1a;border-right:1px solid #2a2a2a;
transform:translateX(-100%);transition:.2s;z-index:40;padding:18px;display:flex;flex-direction:column;gap:8px;overflow:auto}
.drawer.show{transform:none}
.drawer h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#777;margin:14px 0 4px}
.nav{background:none;color:#ddd;text-align:left;padding:10px 12px;border-radius:8px;font-size:14px;width:100%}
.nav:hover{background:#242424}.nav.on{background:#2d6cdf;color:#fff;font-weight:600}
.dlf{display:flex;gap:6px}
.dlf input{flex:1;background:#111;border:1px solid #333;border-radius:7px;color:#eee;padding:8px 10px;font-size:13px;min-width:0}
.dlf input:focus{outline:0;border-color:#2d6cdf}
.qsel{background:#111;border:1px solid #333;border-radius:7px;color:#eee;padding:8px 10px;font-size:13px;width:100%}
.mgo{background:#227a4b;border-radius:7px;color:#fff;font-size:15px;padding:0 12px;flex:0 0 auto}
.mcyc{background:#333;border-radius:8px;color:#fff;font-size:14px;padding:10px;width:100%;font-weight:600}
.err{background:#4a1d1d;color:#f2b8b8;padding:10px 20px;font-size:13px;border-bottom:1px solid #6a2a2a}
</style></head><body>
<div class=ovl id=ovl></div>
<nav class=drawer id=drawer>
 <h3>Télécharger par lien</h3>
 <form class=dlf id=dlf><input id=url placeholder="Lien YouTube…" autocomplete=off><button class=mgo type=submit title=Télécharger>⬇</button></form>
 <select id=q class=qsel>
  <option value=max>Qualité max</option>
  <option value=1080>1080p</option>
  <option value=720 selected>720p</option>
  <option value=480>480p</option>
  <option value=360>360p</option>
  <option value=audio>Audio seul (m4a)</option>
 </select>
 <button class=mcyc id=cyc type=button>Relancer un cycle</button>
 <h3>Naviguer</h3>
 <button class=nav data-v=encours>En cours</button>
 <button class="nav on" data-v=stock>Téléchargées</button>
 <button class=nav data-v=hist>Vues récemment</button>
 <button class=nav data-v=cand>Suggestions</button>
</nav>
<header><button class=burger id=burger title=Menu>☰</button><h1>🎬 ytstock</h1><div class=bar id=bar>…</div></header>
<div class=err id=err hidden></div>
<main>
 <section data-view=encours hidden><h2>En cours — reprendre là où tu t'es arrêté</h2><div class=grid id=g-encours></div></section>
 <section data-view=stock><h2>Téléchargées — à regarder</h2><div class=grid id=g-stock></div></section>
 <section data-view=hist hidden><h2>Vues récemment — 👍 des similaires · 👎 éviter la chaîne</h2><div class=grid id=g-hist></div></section>
 <section data-view=cand hidden><h2>Suggestions — à télécharger <span class=off id=offnote></span></h2><div class=grid id=g-cand></div></section>
</main>
<script>
const online=navigator.onLine;
async function j(u,o){const r=await fetch(u,o);return r.json()}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function card(html){const d=document.createElement('div');d.className='card';d.innerHTML=html;return d}
function thumbLocal(it){return it.thumb?`<img class=thumb src="/thumb/${it.id}">`:`<div class=ph>🎬</div>`}
function fail(m){const e=document.getElementById('err');e.textContent=m;e.hidden=false}
function ok(){document.getElementById('err').hidden=true}
function fmt(s){return s<60?s+' s':Math.round(s/60)+' min'}
// serveur arrêté != bouton cassé : on distingue panne réseau, statut HTTP, refus applicatif.
async function post(u){let r;
 try{r=await fetch(u,{method:'POST'})}catch(e){fail('Serveur injoignable — relance : python3 ytstock.py serve');return {ok:false}}
 if(!r.ok){fail('Erreur serveur ('+r.status+')');return {ok:false}}
 ok();return r.json()}
function delBtn(it){const b=document.createElement('button');b.className='del';b.title='Supprimer';b.textContent='✕';
 b.onclick=async()=>{if(confirm('Supprimer « '+it.title+' » ?')){await post('/api/delete?id='+it.id);load()}};return b}
function playBtn(it,resume){const b=document.createElement('button');b.className='act '+(resume?'resume':'play');
 b.textContent=resume?'▶ Reprendre':'▶ Lancer';
 b.onclick=async()=>{const t=b.textContent;b.textContent='…';await post('/api/open?id='+it.id);b.textContent=t};return b}
function rateBtn(it,kind){const on=kind=='like'?it.liked:it.disliked, other=kind=='like'?it.disliked:it.liked;
 const b=document.createElement('button');b.className=(kind=='like'?'lk':'dk')+(on?' on':'');
 b.textContent=(kind=='like'?'👍':'👎')+(on?' ✓':'');
 b.title=kind=='like'?"J'aime — l'app cherchera des similaires":"Pas pour moi — l'app évitera cette chaîne";
 if(on||other)b.disabled=true;
 else b.onclick=async()=>{b.disabled=true;const r=await post('/api/'+kind+'?id='+it.id);if(r.ok){load()}else b.disabled=false};
 return b}
function renderStock(st){
 const enc=st.filter(it=>it.watched_secs>0), fresh=st.filter(it=>!it.watched_secs);
 const ge=document.getElementById('g-encours');ge.innerHTML='';
 enc.forEach(it=>{const c=card(`${thumbLocal(it)}<div class=body><div class=t>${esc(it.title)}</div>
   <div class=meta>⏱ ${fmt(it.watched_secs)} déjà vues · ${it.size_mib} Mo</div><div class=row></div></div>`);
  c.querySelector('.row').append(playBtn(it,true),delBtn(it));ge.appendChild(c)});
 if(!enc.length)ge.innerHTML='<div class=empty>Aucune vidéo en cours.</div>';
 const gs=document.getElementById('g-stock');gs.innerHTML='';
 fresh.forEach(it=>{const c=card(`${thumbLocal(it)}<div class=body><div class=t>${esc(it.title)}</div>
   <div class=meta>${it.size_mib} Mo</div><div class=row></div></div>`);
  c.querySelector('.row').append(playBtn(it,false),delBtn(it));gs.appendChild(c)});
 if(!fresh.length)gs.innerHTML='<div class=empty>Rien de neuf en stock.</div>';
}
function renderHist(hs){const gh=document.getElementById('g-hist');gh.innerHTML='';
 hs.forEach(it=>{const c=card(`${thumbLocal(it)}<div class=body><div class=t>${esc(it.title)}</div><div class=row></div></div>`);
  c.querySelector('.row').append(rateBtn(it,'like'),rateBtn(it,'dislike'));gh.appendChild(c)});
 if(!hs.length)gh.innerHTML='<div class=empty>Aucune vidéo vue pour l\\'instant.</div>';
}
function renderCand(cd){const gc=document.getElementById('g-cand');gc.innerHTML='';
 document.getElementById('offnote').textContent=online?'':'(hors ligne — indispo)';
 cd.forEach(m=>{const c=card(`<img class=thumb src="https://i.ytimg.com/vi/${m.id}/mqdefault.jpg" onerror="this.outerHTML='<div class=ph>🎬</div>'">
   <div class=body><div class=t>${esc(m.title||m.id)}</div><div class=meta>❤ ${m.like} · 💬 ${m.comment} · 👁 ${m.view}</div>
   <div class=row><button class=dl title=Télécharger ${online?'':'disabled'}>⬇</button></div></div>`);
  const b=c.querySelector('.dl');
  if(online)b.onclick=async()=>{b.textContent='…';b.disabled=true;const r=await post('/api/download?id='+m.id);if(r.ok){load()}else{b.textContent=r.busy?'⏳':'✕';setTimeout(()=>{b.textContent='⬇';b.disabled=false},2500)}};
  gc.appendChild(c)});
 if(!cd.length)gc.innerHTML='<div class=empty>Pas encore de suggestions (lance un cycle en ligne).</div>';
}
async function load(){
 const st=await j('/api/stock');renderStock(st);
 const mib=st.reduce((a,b)=>a+b.size_mib,0);
 document.getElementById('bar').innerHTML=`<b>${st.length}</b> vidéos · <b>${(mib/1024).toFixed(1)}</b> Go`;
 renderHist(await j('/api/history'));
 renderCand(await j('/api/candidates'));
}
function showMenu(v){document.getElementById('drawer').classList.toggle('show',v);document.getElementById('ovl').classList.toggle('show',v)}
function selectView(v){document.querySelectorAll('section[data-view]').forEach(s=>s.hidden=s.dataset.view!=v);
 document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('on',n.dataset.v==v))}
function setBusy(b){const c=document.getElementById('cyc');c.disabled=b;c.textContent=b?'Cycle en cours…':'Relancer un cycle'}
document.getElementById('burger').onclick=()=>showMenu(true);
document.getElementById('ovl').onclick=()=>showMenu(false);
document.querySelectorAll('.nav').forEach(n=>n.onclick=()=>{selectView(n.dataset.v);showMenu(false)});
document.getElementById('cyc').onclick=async()=>{const r=await post('/api/refill');if(r.busy)fail('Un cycle est déjà en cours.');else if(r.ok){setBusy(true);showMenu(false)}};
document.getElementById('dlf').onsubmit=async e=>{e.preventDefault();
 const u=document.getElementById('url');if(!u.value.trim())return;
 const b=e.target.querySelector('.mgo');b.disabled=true;b.textContent='…';
 const q=document.getElementById('q').value;
 const r=await post('/api/download?q='+q+'&url='+encodeURIComponent(u.value));b.disabled=false;b.textContent='⬇';
 if(r.ok){u.value='';selectView('stock');showMenu(false);load()}
 else fail(r.busy?'Occupé — un téléchargement est déjà en cours.':'Échec : URL invalide ou vidéo indisponible.')};
// sonde légère : le refill dure des minutes, seul retour honnête sans machine à états.
setInterval(async()=>{try{setBusy((await j('/api/busy')).busy)}catch(e){}},3000);
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
    valid_id = is_valid_id

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
            elif path == "/api/history":
                self._json(history_items())
            elif path == "/api/busy":
                self._json({"busy": is_busy()})
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
            q = parse_qs(urlparse(self.path).query)
            if path == "/api/refill":
                # refill() dure des minutes : en tâche de fond, on répond tout de
                # suite. Le verrou existant sert de garde anti-doublon.
                if is_busy():
                    return self._json({"ok": False, "busy": True})
                threading.Thread(target=refill, daemon=True).start()
                return self._json({"ok": True})
            vid = (q.get("id") or [""])[0]
            if path == "/api/download" and not vid:
                vid = id_from_url((q.get("url") or [""])[0]) or ""
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
            elif path in ("/api/like", "/api/dislike"):
                kind = "like" if path == "/api/like" else "dislike"
                # titre pris dans l'historique, jamais du client
                title = next((h["title"] for h in load_history()
                              if h.get("id") == vid), vid)
                (add_like if kind == "like" else add_dislike)(vid, title)
                # résolution de la chaîne hors du chemin HTTP (~5 s)
                threading.Thread(target=resolve_rating_channel, args=(kind, vid),
                                 daemon=True).start()
                self._json({"ok": True})
            elif path == "/api/download":
                # qualité choisie côté UI, validée contre la liste blanche
                quality = (q.get("q") or [""])[0]
                if quality not in QUALITIES:
                    quality = None                    # défaut = plafond démon
                with download_lock() as got:
                    if not got:                       # démon en train de télécharger
                        return self._json({"ok": False, "busy": True})
                    ok = download(vid, quality)
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
    if not os.environ.get("YTSTOCK_NO_OPEN"):   # le .app ouvre lui-même sa fenêtre
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

    # id_from_url : formes acceptées pour le champ "coller une URL"
    assert id_from_url("https://www.youtube.com/watch?v=0z8W4XQ6KOo") == "0z8W4XQ6KOo"
    assert id_from_url("https://youtu.be/0z8W4XQ6KOo?t=42") == "0z8W4XQ6KOo"
    assert id_from_url("https://www.youtube.com/shorts/0z8W4XQ6KOo") == "0z8W4XQ6KOo"
    assert id_from_url("https://m.youtube.com/watch?v=0z8W4XQ6KOo&list=x") == "0z8W4XQ6KOo"
    assert id_from_url("0z8W4XQ6KOo") == "0z8W4XQ6KOo"          # id brut
    assert id_from_url("https://evil.example.com/?v=0z8W4XQ6KOo") is None  # hôte non YouTube
    assert id_from_url("https://www.youtube.com/watch?v=trop court") is None
    assert id_from_url("") is None and id_from_url(None) is None

    # is_valid_channel : sert à construire une URL de chaîne
    assert is_valid_channel("UC" + "a" * 22) is True
    assert is_valid_channel("UCtrop-court") is False
    assert is_valid_channel("../../etc/passwd12345678") is False

    # ranked_score : un like pousse la chaîne, sans fausser engagement_score
    base = {"like": 100, "comment": 10, "view": 10000, "channel": "UC" + "b" * 22}
    assert ranked_score(base, []) == engagement_score(base)
    assert ranked_score(base, ["UC" + "b" * 22]) > ranked_score(base, [])
    assert ranked_score({"like": 1, "comment": 0, "view": 100}, ["UC" + "b" * 22]) \
        == engagement_score({"like": 1, "comment": 0, "view": 100})   # sans chaîne

    # is_valid_id : charset strict (bloque injection via id de 11 chars hostile)
    assert is_valid_id("0z8W4XQ6KOo") is True
    assert is_valid_id('"><script>x') is False   # 11 chars mais charset interdit
    assert is_valid_id("../etc/pass") is False
    assert is_valid_id("short") is False

    # Task 3: needs_more
    assert needs_more(0) is True
    assert needs_more(BUDGET_BYTES - 1) is True
    assert needs_more(BUDGET_BYTES) is False
    assert needs_more(BUDGET_BYTES + 1) is False

    # Task 4 & 6: Persistent state and mark_watched (nested in temp dir)
    global STATE_DIR, SEEN_FILE, WATCH_FILE, VIDEO_DIR, FAILS_FILE
    global HISTORY_FILE, LIKES_FILE, DISLIKES_FILE
    _saved_state = (STATE_DIR, SEEN_FILE, WATCH_FILE, FAILS_FILE,
                    HISTORY_FILE, LIKES_FILE, DISLIKES_FILE)
    _saved_vd = VIDEO_DIR
    _tmp = Path(tempfile.mkdtemp())
    STATE_DIR, SEEN_FILE, WATCH_FILE = _tmp, _tmp / "seen.txt", _tmp / "watch.json"
    FAILS_FILE = _tmp / "fails.json"
    HISTORY_FILE, LIKES_FILE = _tmp / "history.json", _tmp / "likes.json"
    DISLIKES_FILE = _tmp / "dislikes.json"
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
        # historique : mark_watched garde le TITRE avant de supprimer le fichier,
        # sinon impossible de liker la vidéo après coup
        h = load_history()
        assert h[0]["id"] == "eeeeeeeeeee" or any(x["id"] == "ddddddddddd" for x in h)
        entry = next(x for x in h if x["id"] == "ddddddddddd")
        assert entry["title"] == "Titre"          # pas le nom de fichier brut
        mark_watched("iiiiiiiiiii", "test")        # aucun fichier local
        assert next(x for x in load_history() if x["id"] == "iiiiiiiiiii")["title"] == "iiiiiiiiiii"
        # pas de doublon : revoir une vidéo la remonte en tête
        mark_watched("ddddddddddd", "test")
        assert [x["id"] for x in load_history()].count("ddddddddddd") == 1
        assert load_history()[0]["id"] == "ddddddddddd"
        # cap de l'historique
        for n in range(HISTORY_MAX + 5):
            record_history(f"z{n:010d}", f"t{n}")
        assert len(load_history()) == HISTORY_MAX

        # likes locaux : rien n'est envoyé à YouTube, on retient juste le choix
        add_like("jjjjjjjjjjj", "Un titre")
        add_like("jjjjjjjjjjj", "Un titre")        # idempotent
        assert list(load_likes()) == ["jjjjjjjjjjj"]
        assert liked_channels() == []              # chaîne pas encore résolue
        likes = load_likes()
        likes["jjjjjjjjjjj"]["channel"] = "UC" + "a" * 22
        _atomic_write(LIKES_FILE, json.dumps(likes))
        assert liked_channels() == ["UC" + "a" * 22]

        # dislikes : symétriques, servent à ÉVITER une chaîne
        add_dislike("kkkkkkkkkkk", "Pas pour moi")
        add_dislike("kkkkkkkkkkk", "Pas pour moi")   # idempotent
        assert list(load_dislikes()) == ["kkkkkkkkkkk"]
        assert disliked_channels() == []
        dk = load_dislikes()
        dk["kkkkkkkkkkk"]["channel"] = "UC" + "c" * 22
        _atomic_write(DISLIKES_FILE, json.dumps(dk))
        assert disliked_channels() == ["UC" + "c" * 22]
        # un candidat d'une chaîne dislikée est écarté du classement
        save_candidates([{"id": "lllllllllll", "title": "x", "like": 9, "comment": 9,
                          "view": 9999, "channel": "UC" + "c" * 22}])
        assert load_candidates() == []               # filtré (chaîne évitée)
    finally:
        (STATE_DIR, SEEN_FILE, WATCH_FILE, FAILS_FILE,
         HISTORY_FILE, LIKES_FILE, DISLIKES_FILE) = _saved_state
        VIDEO_DIR = _saved_vd
        shutil.rmtree(_tmp, ignore_errors=True)

    # Task 5: watcher_tick — le temps s'accumule et PERSISTE (visionnage partiel)
    # ouverture progressive
    watched, acc = watcher_tick({"a"}, {})
    assert watched == set() and acc == {"a": POLL_SECS}
    for _ in range(5):                          # 6 ticks * 15s = 90s
        watched, acc = watcher_tick({"a"}, acc)
    assert acc["a"] >= WATCHED_SECS and watched == set()
    # fermée après le seuil => vue, oubliée
    watched, acc = watcher_tick(set(), acc)
    assert watched == {"a"} and "a" not in acc
    # partiel puis fermée => PAS vue mais CONSERVÉE ("en cours")
    _, acc2 = watcher_tick({"b"}, {})           # b à 15s
    watched, acc2 = watcher_tick(set(), acc2)   # fermée avant seuil
    assert watched == set() and acc2.get("b") == POLL_SECS
    # reprise plus tard : le temps s'additionne jusqu'au seuil puis vue à la fermeture
    for _ in range(6):
        watched, acc2 = watcher_tick({"b"}, acc2)
    assert watched == set() and acc2["b"] >= WATCHED_SECS   # pas supprimée en lecture
    watched, acc2 = watcher_tick(set(), acc2)
    assert watched == {"b"}

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
