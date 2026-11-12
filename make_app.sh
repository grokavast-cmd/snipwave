#!/bin/bash
# Builds "~/Applications/Clip Maker.app": a tiny wrapper that starts Clip Maker
# from THIS folder (if you move the folder, run this script again).
set -eu

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_DIR="$HOME/Applications"
BUNDLE="$DEST_DIR/Clip Maker.app"

mkdir -p "$DEST_DIR"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Clip Maker</string>
  <key>CFBundleDisplayName</key><string>Clip Maker</string>
  <key>CFBundleIdentifier</key><string>local.clipmaker.app</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>ClipMaker</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# The executable: if setup is done, start the app directly (no Terminal window).
# Otherwise open the launcher in Terminal so you can watch the one-time setup.
cat > "$BUNDLE/Contents/MacOS/ClipMaker" <<EXE
#!/bin/bash
APP_DIR="$APP_DIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:\$HOME/.deno/bin:\$PATH"
if [ ! -d "\$APP_DIR" ]; then
  osascript -e 'display alert "Clip Maker" message "Can'"'"'t find the Clip Maker folder at:\n$APP_DIR\n\nRun make_app.sh again from the folder'"'"'s new location."'
  exit 1
fi
LOG="\$HOME/Library/Logs/ClipMaker.log"
if [ -x "\$APP_DIR/.venv/bin/python" ] && [ -f "\$APP_DIR/.venv/.clipmaker-ready-v1" ]; then
  cd "\$APP_DIR" || exit 1
  "\$APP_DIR/.venv/bin/python" "\$APP_DIR/app.py" >"\$LOG" 2>&1
  code=\$?
  if [ \$code -ne 0 ]; then
    osascript -e "display alert \"Clip Maker couldn't start\" message \"Details were saved to \$LOG. Opening it now.\"" >/dev/null 2>&1
    open -a TextEdit "\$LOG"
  fi
  exit \$code
else
  open -a Terminal "\$APP_DIR/Clip Maker.command"
fi
EXE
chmod +x "$BUNDLE/Contents/MacOS/ClipMaker"

if [ -f "$APP_DIR/assets/icon.icns" ]; then
  cp "$APP_DIR/assets/icon.icns" "$BUNDLE/Contents/Resources/AppIcon.icns"
else
  echo "(No icon found; the app will use the default icon.)"
fi

# Remove the "downloaded from the internet" flag so Gatekeeper doesn't block it.
xattr -dr com.apple.quarantine "$BUNDLE" 2>/dev/null || true
xattr -dr com.apple.quarantine "$APP_DIR" 2>/dev/null || true
touch "$BUNDLE"   # nudge Finder to refresh the icon

echo "✓ Created: $BUNDLE"
echo "  Open it from Finder > Go > Home > Applications, or with Spotlight (⌘Space, 'Clip Maker')."
echo "  Tip: drag it to the Dock to keep it handy."
