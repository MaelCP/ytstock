#!/usr/bin/env python3
"""ytstock — file de vidéos YouTube toujours prête. Voir docs/superpowers/specs/."""
import sys
from pathlib import Path
import re
import json
import os
import datetime
import tempfile

VIDEO_DIR    = Path.home() / "Downloads" / "videos"
STATE_DIR    = VIDEO_DIR / ".ytstock"
SEEN_FILE    = STATE_DIR / "seen.txt"
WATCH_FILE   = STATE_DIR / "watch.json"
LOG_FILE     = STATE_DIR / "ytstock.log"

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

    # Task 3: needs_more
    assert needs_more(0) is True
    assert needs_more(BUDGET_BYTES - 1) is True
    assert needs_more(BUDGET_BYTES) is False
    assert needs_more(BUDGET_BYTES + 1) is False

    # Task 4 & 6: Persistent state and mark_watched (nested in temp dir)
    global STATE_DIR, SEEN_FILE, WATCH_FILE, VIDEO_DIR
    _saved_state = (STATE_DIR, SEEN_FILE, WATCH_FILE)
    _saved_vd = VIDEO_DIR
    _tmp = Path(tempfile.mkdtemp())
    STATE_DIR, SEEN_FILE, WATCH_FILE = _tmp, _tmp / "seen.txt", _tmp / "watch.json"
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
    finally:
        STATE_DIR, SEEN_FILE, WATCH_FILE = _saved_state
        VIDEO_DIR = _saved_vd

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
    else:
        print("usage: ytstock.py [daemon|refill|status|--self-check]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
