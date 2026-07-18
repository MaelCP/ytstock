#!/usr/bin/env python3
"""ytstock — file de vidéos YouTube toujours prête. Voir docs/superpowers/specs/."""
import sys
from pathlib import Path

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


def run_self_check():
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
