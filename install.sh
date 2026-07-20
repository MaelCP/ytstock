#!/bin/sh
# Installe ytstock : dépendances + application macOS. Idempotent — relançable.
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/ytstock.app"

echo "▶ ytstock — installation depuis $REPO"

# --- 1. macOS uniquement (lecteurs, .app, lsof) ---
[ "$(uname)" = "Darwin" ] || { echo "✗ macOS requis (utilise 'python3 ytstock.py serve' ailleurs)."; exit 1; }

# --- 2. Homebrew ---
if ! command -v brew >/dev/null 2>&1; then
  echo "✗ Homebrew absent. Installe-le : https://brew.sh puis relance ./install.sh"
  exit 1
fi

# --- 3. Outils CLI (obligatoires) ---
echo "▶ dépendances CLI : yt-dlp, aria2, deno, ffmpeg…"
brew install yt-dlp aria2 deno ffmpeg

# --- 4. Applications (best-effort, ne bloque pas si l'utilisateur refuse) ---
#   VLC     : lecture des vidéos
#   Firefox : source des cookies YouTube (pas de prompt Trousseau)
#   Chrome  : fenêtre "app" sans barre d'adresse (sinon on tombe sur le navigateur par défaut)
for cask in vlc firefox google-chrome; do
  if ! brew list --cask "$cask" >/dev/null 2>&1 && [ ! -d "/Applications/$(echo $cask | sed 's/google-chrome/Google Chrome/;s/vlc/VLC/;s/firefox/Firefox/').app" ]; then
    echo "▶ installation de $cask…"; brew install --cask "$cask" || echo "  (ignoré : $cask non installé)"
  fi
done

# --- 5. Construction de l'app macOS pointant sur CE dossier ---
echo "▶ build $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>ytstock</string>
<key>CFBundleIdentifier</key><string>local.ytstock</string>
<key>CFBundleExecutable</key><string>ytstock</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleIconFile</key><string>ytstock</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST

# Icône de l'app
if [ -f "$REPO/assets/ytstock.icns" ]; then
  mkdir -p "$APP/Contents/Resources"
  cp "$REPO/assets/ytstock.icns" "$APP/Contents/Resources/ytstock.icns"
fi

# REPO est injecté en dur : l'app sait où vit le script quel que soit le dossier cloné.
cat > "$APP/Contents/MacOS/ytstock" <<LAUNCH
#!/bin/sh
# Généré par install.sh — lance démon + serveur, puis une fenêtre dédiée.
REPO="$REPO"
LAUNCH
cat >> "$APP/Contents/MacOS/ytstock" <<'LAUNCH'
STATE="$REPO/.ytstock"; mkdir -p "$STATE"
exec >>"$STATE/app.log" 2>&1
echo "--- lancement $(date)"
# python Homebrew d'abord : /usr/bin/python3 est un shim qui pointe vers Xcode
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && { PY="$c"; break; }
done
URL=http://127.0.0.1:8787

pgrep -f "ytstock.py daemon" >/dev/null || { echo "démon"; "$PY" "$REPO/ytstock.py" daemon & }

curl -s -o /dev/null --max-time 1 "$URL" || {
  echo "serveur"; YTSTOCK_NO_OPEN=1 "$PY" "$REPO/ytstock.py" serve &
  n=0; until curl -s -o /dev/null --max-time 1 "$URL"; do
    n=$((n+1)); [ $n -gt 60 ] && { echo "serveur injoignable"; exit 1; }; sleep 0.5
  done
}
echo "fenêtre"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -x "$CHROME" ]; then
  # profil dédié = vraie fenêtre app même si Chrome tourne déjà
  exec arch -arm64 "$CHROME" --app="$URL" --user-data-dir="$STATE/chrome"
else
  exec open "$URL"   # pas de Chrome : navigateur par défaut, onglet normal
fi
LAUNCH
chmod +x "$APP/Contents/MacOS/ytstock"
codesign --force --deep --sign - "$APP" 2>/dev/null || true

# --- 5b. App barre de menu (téléchargement rapide par lien copié) ---
if command -v swiftc >/dev/null 2>&1 && [ -f "$REPO/menubar/ytstock-menu.swift" ]; then
  echo "▶ build ytstock-menu.app (barre de menu)"
  MAPP="/Applications/ytstock-menu.app"
  swiftc -O -o "$REPO/menubar/ytstock-menu" "$REPO/menubar/ytstock-menu.swift" 2>/dev/null
  rm -rf "$MAPP"; mkdir -p "$MAPP/Contents/MacOS" "$MAPP/Contents/Resources"
  cp "$REPO/menubar/ytstock-menu" "$MAPP/Contents/MacOS/ytstock-menu"
  [ -f "$REPO/assets/ytstock.icns" ] && cp "$REPO/assets/ytstock.icns" "$MAPP/Contents/Resources/ytstock.icns"
  cat > "$MAPP/Contents/Info.plist" <<'MPL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>ytstock-menu</string>
<key>CFBundleIdentifier</key><string>local.ytstock.menu</string>
<key>CFBundleExecutable</key><string>ytstock-menu</string>
<key>CFBundleIconFile</key><string>ytstock</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
</dict></plist>
MPL
  codesign --force --deep --sign - "$MAPP" 2>/dev/null || true
fi

# --- 6. Vérification ---
echo "▶ self-check"
python3 "$REPO/ytstock.py" --self-check

cat <<DONE

✅ Installé.
   • Lance l'app : open -a ytstock   (ou double-clic sur ytstock dans Applications)
   • Ou en terminal : python3 "$REPO/ytstock.py" serve
   Les vidéos vont dans ~/Downloads/videos (change avec la variable YTSTOCK_DIR).

⚠️  ACCÈS DISQUE (une seule fois) : si le dossier des vidéos est dans un
    emplacement protégé (Downloads, Bureau, Documents), macOS bloque l'app.
    Réglages Système ▸ Confidentialité et sécurité ▸ Accès complet au disque
    ▸ + ▸ ajoute /Applications/ytstock.app, puis relance l'app.
    (Astuce : mets YTSTOCK_DIR dans ~/Movies ou ~/ytstock pour éviter cette étape.)
DONE
