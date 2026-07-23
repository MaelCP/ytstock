#!/usr/bin/env python3
"""Serveur MCP (stdio) pour ytstock — pilotable par Claude Code.

Ne fait que taper sur l'API HTTP locale de `ytstock serve` (:8787) : aucune
dépendance PyPI, aucune modification de ytstock.py. Installer côté Claude Code :

    claude mcp add ytstock -- python3 <repo>/mcp/ytstock_mcp.py

Puis : « télécharge cette vidéo <url> » ou « télécharge 4 vidéos sur la guerre ».
"""
import json
import os
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PORT = int(os.environ.get("YTSTOCK_MCP_PORT", "8787"))
BASE = os.environ.get("YTSTOCK_MCP_BASE", f"http://127.0.0.1:{PORT}")
ORIGIN = f"http://127.0.0.1:{PORT}"      # doit matcher la liste blanche du serveur
VALID_Q = {"max", "1080", "720", "480", "360", "audio"}


# --- accès à l'API HTTP locale --------------------------------------------
def _req(method, path, params=None, boot=False):
    """Requête vers le serveur ytstock, avec le header Origin anti-CSRF. Si le
    serveur est éteint et boot=True, lance l'app puis réessaie une fois."""
    url = BASE + path
    if params:
        url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
    req = Request(url, method=method, headers={"Origin": ORIGIN})
    if method == "POST":
        req.data = b""
    try:
        with urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"null")
    except URLError:
        if boot and _boot():
            with urlopen(req, timeout=15) as r:
                return json.loads(r.read() or b"null")
        raise


def _boot():
    """Démarre ytstock.app (démon + serveur) et attend que :8787 réponde."""
    subprocess.run(["open", "-a", "ytstock"], check=False)
    for _ in range(60):
        try:
            urlopen(BASE + "/api/queue", timeout=1)
            return True
        except URLError:
            time.sleep(0.5)
    return False


def _q(quality):
    return quality if quality in VALID_Q else None


def _yt_search(query, count):
    count = max(1, min(int(count), 20))
    out = subprocess.run(
        ["yt-dlp", f"ytsearch{count}:{query}", "--flat-playlist",
         "--no-warnings", "--print", "%(id)s\t%(title)s"],
        capture_output=True, text=True, timeout=120)
    return _parse_search(out.stdout)


def _parse_search(text):
    res = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            vid, title = line.split("\t", 1)
            res.append((vid, title))
        else:
            res.append((line, line))       # id seul, pas de titre
    return res


# --- outils MCP ------------------------------------------------------------
def t_download(a):
    r = _req("POST", "/api/download", {"url": a["url"], "q": _q(a.get("quality"))}, boot=True)
    if r.get("ok"):
        return f"En file (position {r.get('n', '?')}) : {a['url']}"
    return f"Refusé : {r.get('err', 'raison inconnue')}"


def t_search_and_download(a):
    query = a["query"]
    results = _yt_search(query, a.get("count", 5))
    if not results:
        return f"Aucun résultat pour « {query} »."
    queued = []
    for vid, title in results:
        r = _req("POST", "/api/download", {"id": vid, "q": _q(a.get("quality"))}, boot=True)
        if r.get("ok"):
            queued.append(title)
    if not queued:
        return f"Rien mis en file pour « {query} »."
    return "En file :\n- " + "\n- ".join(queued)


def t_queue_status(a):
    q = _req("GET", "/api/queue") or []
    active = [i for i in q if i.get("state") in ("pending", "downloading")]
    if not active:
        return "File vide — rien en cours."
    lines = []
    for i in active:
        t = i.get("title") or i.get("id")
        if i.get("state") == "downloading":
            lines.append(f"▶ {t} — {i.get('pct', 0)}%")
        else:
            lines.append(f"… {t} (en attente)")
    return "\n".join(lines)


def t_list_stock(a):
    s = _req("GET", "/api/stock") or []
    if not s:
        return "Stock vide."
    return "\n".join(f"{it['id']}  {it.get('title', '?')} "
                     f"({it.get('size_mib', '?')} Mo)" for it in s[:50])


def t_play(a):
    r = _req("POST", "/api/open", {"id": a["id"]}, boot=True)
    return "Lancé dans VLC." if r.get("ok") else f"Introuvable : {a['id']}"


_QUALITY_PROP = {"type": "string", "enum": sorted(VALID_Q),
                 "description": "Qualité optionnelle (défaut : réglage serveur)"}

TOOL_DEFS = [
    {"name": "download",
     "description": "Télécharge une vidéo YouTube depuis son URL (ou son id). L'ajoute à la file ytstock.",
     "inputSchema": {"type": "object", "required": ["url"], "properties": {
         "url": {"type": "string", "description": "URL ou id de la vidéo YouTube"},
         "quality": _QUALITY_PROP}}},
    {"name": "search_and_download",
     "description": "Cherche des vidéos YouTube par thème (ex. « guerre ») et met les N premières en file de téléchargement.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "Thème / mots-clés"},
         "count": {"type": "integer", "description": "Nombre de vidéos (défaut 5, max 20)"},
         "quality": _QUALITY_PROP}}},
    {"name": "queue_status",
     "description": "État de la file : ce qui télécharge, le %, et ce qui attend.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_stock",
     "description": "Vidéos déjà téléchargées, prêtes à regarder (id + titre).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "play",
     "description": "Ouvre une vidéo déjà téléchargée dans VLC (par id — voir list_stock).",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string", "description": "id de la vidéo (voir list_stock)"}}}},
]

TOOLS = {"download": t_download, "search_and_download": t_search_and_download,
         "queue_status": t_queue_status, "list_stock": t_list_stock, "play": t_play}


# --- boucle JSON-RPC stdio -------------------------------------------------
def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(id_, result):
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def serve_mcp():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        m, id_ = msg.get("method"), msg.get("id")
        if m == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion", "2024-11-05")
            _result(id_, {"protocolVersion": pv, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "ytstock", "version": "1.0"}})
        elif m == "ping":
            _result(id_, {})
        elif m == "tools/list":
            _result(id_, {"tools": TOOL_DEFS})
        elif m == "tools/call":
            p = msg.get("params") or {}
            fn = TOOLS.get(p.get("name"))
            if not fn:
                _result(id_, {"content": [{"type": "text", "text": f"Outil inconnu : {p.get('name')}"}], "isError": True})
                continue
            try:
                text = fn(p.get("arguments") or {})
                _result(id_, {"content": [{"type": "text", "text": text}]})
            except Exception as e:                       # renvoyé à Claude, pas de crash
                _result(id_, {"content": [{"type": "text", "text": f"Erreur : {e}"}], "isError": True})
        elif id_ is not None:                            # requête inconnue
            _send({"jsonrpc": "2.0", "id": id_,
                   "error": {"code": -32601, "message": f"method not found: {m}"}})
        # sinon : notification non gérée -> on ignore


# --- test intégré ----------------------------------------------------------
def _self_check():
    import http.server
    import threading
    global BASE
    calls = []

    class Fake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            calls.append(("POST", self.path, self.headers.get("Origin")))
            self._reply({"ok": True, "n": 1})

        def do_GET(self):
            calls.append(("GET", self.path, self.headers.get("Origin")))
            self._reply([{"id": "aaaaaaaaaaa", "title": "Titre", "state": "downloading", "pct": 42}])

    srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
    BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    assert t_download({"url": "https://youtu.be/x", "quality": "720"}).startswith("En file")
    assert calls[-1][0] == "POST" and "/api/download" in calls[-1][1], calls[-1]
    assert "q=720" in calls[-1][1], calls[-1]
    assert calls[-1][2] == ORIGIN, calls[-1]              # header anti-CSRF présent
    assert "42%" in t_queue_status({}), "progression non lue"
    assert _q("banana") is None and _q("720") == "720"
    assert _parse_search("id1\tTitre A\nid2\tTitre B") == [("id1", "Titre A"), ("id2", "Titre B")]
    assert _parse_search("idseul") == [("idseul", "idseul")]
    srv.shutdown()
    print("self-check OK")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        serve_mcp()
