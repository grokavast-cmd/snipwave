#!/bin/bash
# Clip Maker launcher. Double-click me in Finder.
# First run: installs ffmpeg/deno/Python (via Homebrew) and the app's Python packages.
# Later runs: starts the app straight away.
#   ./Clip\ Maker.command --reinstall   rebuilds the private Python environment
set -u

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.deno/bin:$PATH"
VENV="$APP_DIR/.venv"
STAMP="$VENV/.clipmaker-ready-v1"
REQ_HASH="$(cksum < "$APP_DIR/requirements.txt" | awk '{print $1}')"

pause_and_exit() {
  echo
  if [ -t 0 ]; then read -r -p "Press Enter to close this window… " _; fi
  exit "${1:-1}"
}

launch() {
  # Hide the Terminal noise; show errors only.
  exec "$VENV/bin/python" "$APP_DIR/app.py" "$@"
}

if [ "${1:-}" = "--reinstall" ]; then
  echo "Removing the old environment…"; rm -rf "$VENV"; shift
fi

# ---------- fast path ----------
if [ -x "$VENV/bin/python" ] && [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$REQ_HASH" ] \
   && command -v ffmpeg >/dev/null 2>&1; then
  launch "$@"
fi

# ---------- first-run setup ----------
echo "=============================================="
echo "  Clip Maker: one-time setup (takes 1-5 min)"
echo "=============================================="
echo

if ! command -v brew >/dev/null 2>&1; then
  cat <<'MSG'
Homebrew (the Mac package manager) isn't installed yet. Clip Maker needs it
to install ffmpeg (the video cutter) and Python.

1. Open https://brew.sh and copy the install command, or paste this into Terminal:

   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

2. When it finishes, follow the "Next steps" it prints (on Apple Silicon Macs it
   asks you to run two lines that add brew to your PATH).
3. Double-click "Clip Maker.command" again.
MSG
  pause_and_exit 1
fi

brew_install() {   # brew_install <formula> : install only if missing
  if brew list --formula "$1" >/dev/null 2>&1; then
    echo "✓ $1 already installed"
  else
    echo "→ Installing $1 (via Homebrew)…"
    brew install "$1" || { echo "✗ Couldn't install $1. See the messages above."; pause_and_exit 1; }
  fi
}

brew_install ffmpeg
# YouTube now requires a JavaScript runtime for yt-dlp to see all video formats.
if ! command -v deno >/dev/null 2>&1 && ! command -v node >/dev/null 2>&1; then
  # Use Deno's official prebuilt binary (works on macOS 12+). Homebrew would
  # try to compile it from source on older macOS, which needs Xcode 15.
  echo "→ Installing deno (official installer)…"
  curl -fsSL https://deno.land/install.sh | sh -s -- -y --no-modify-path \
    || { echo "✗ Couldn't install deno. See the messages above."; pause_and_exit 1; }
  export PATH="$HOME/.deno/bin:$PATH"
fi

# A Python 3.9+ whose tkinter works with Tk 8.6 or newer.
good_python() {
  "$1" -c 'import sys, tkinter
assert sys.version_info >= (3, 9)
assert tkinter.TkVersion >= 8.6
tkinter.Tcl()' >/dev/null 2>&1
}

PY=""
BREW_PREFIX="$(brew --prefix)"
for cand in \
  "$BREW_PREFIX/opt/python@3.13/bin/python3.13" \
  "$BREW_PREFIX/opt/python@3.12/bin/python3.12" \
  "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
  "/Library/Frameworks/Python.framework/Versions/Current/bin/python3" \
  "$BREW_PREFIX/bin/python3"; do
  if [ -x "$cand" ] && good_python "$cand"; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  # Homebrew's Python ships without Tk; it lives in a separate python-tk@X.Y formula.
  for cand in "$BREW_PREFIX/opt/python@3.13/bin/python3.13" "$BREW_PREFIX/opt/python@3.12/bin/python3.12"; do
    if [ -x "$cand" ]; then
      ver="$("$cand" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
      brew_install "python-tk@$ver"
      if good_python "$cand"; then PY="$cand"; break; fi
    fi
  done
fi

if [ -z "$PY" ]; then
  brew_install python@3.12
  brew_install python-tk@3.12
  PY="$BREW_PREFIX/opt/python@3.12/bin/python3.12"
  if ! good_python "$PY"; then
    echo "✗ Python's Tk support still isn't working ($PY)."
    echo "  Try:  brew reinstall python@3.12 python-tk@3.12   then run me again."
    pause_and_exit 1
  fi
fi
echo "✓ Using Python: $PY ($("$PY" --version 2>&1))"

if [ ! -x "$VENV/bin/python" ]; then
  echo "→ Creating a private Python environment in $(basename "$VENV")…"
  "$PY" -m venv "$VENV" || { echo "✗ Couldn't create the environment."; pause_and_exit 1; }
fi
echo "→ Installing Python packages (customtkinter, yt-dlp, pillow)…"
"$VENV/bin/python" -m pip install --upgrade --disable-pip-version-check -q pip
"$VENV/bin/python" -m pip install --upgrade --disable-pip-version-check -r "$APP_DIR/requirements.txt" \
  || { echo "✗ Package install failed. Check your internet connection and try again."; pause_and_exit 1; }

if ! "$VENV/bin/python" -c "import tkinter, customtkinter, yt_dlp, PIL" >/dev/null 2>&1; then
  echo "✗ Something's still missing. Run:  \"$APP_DIR/Clip Maker.command\" --reinstall"
  pause_and_exit 1
fi
echo "$REQ_HASH" > "$STAMP"

echo
echo "✓ All set! Starting Clip Maker…  (you can close this Terminal window after the app opens"
echo "  next time; tip: run ./make_app.sh once to get a normal app icon in Applications)"
launch "$@"
