#!/usr/bin/env bash
#
# install.sh — install the macos-dark theme for labwc.
#
#   ./install.sh              install/refresh the theme, patch rc.xml, reload
#   ./install.sh --theme-only copy the theme files only, touch nothing else
#   ./install.sh --no-reload  patch rc.xml but do not reload labwc
#   ./install.sh --uninstall  remove the theme files (leaves rc.xml alone)
#
# Honours XDG_CONFIG_HOME / XDG_DATA_HOME; defaults to ~/.config and
# ~/.local/share like labwc does.

set -euo pipefail

THEME_NAME="macos-dark"
PKG_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SRC_THEME="$PKG_DIR/themes/$THEME_NAME"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"

DEST_THEME="$DATA_HOME/themes/$THEME_NAME"
LABWC_DIR="$CONFIG_HOME/labwc"
RC_XML="$LABWC_DIR/rc.xml"
THEMERC_OVERRIDE="$LABWC_DIR/themerc-override"

BLUR_BLOCK='  <blur>
    <passes>4</passes>
    <radius>7</radius>
    <noise>0.02</noise>
    <brightness>0.9</brightness>
    <contrast>0.9</contrast>
    <saturation>1.1</saturation>
    <strength>1.0</strength>
  </blur>'

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
info() { printf '  %s\n' "$*"; }

MODE="install"
RELOAD=1
for arg in "$@"; do
	case "$arg" in
		--theme-only)  MODE="theme-only" ;;
		--no-reload)   RELOAD=0 ;;
		--uninstall)   MODE="uninstall" ;;
		-h|--help)     sed -n '3,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
		*)             die "unknown option: $arg" ;;
	esac
done

[ -d "$SRC_THEME" ] || die "theme source not found at $SRC_THEME"

# ----------------------------------------------------------------- uninstall --
if [ "$MODE" = "uninstall" ]; then
	if [ -d "$DEST_THEME" ]; then
		rm -rf -- "$DEST_THEME"
		info "removed $DEST_THEME"
	else
		info "nothing to remove at $DEST_THEME"
	fi
	info "rc.xml left untouched — drop <theme><name>$THEME_NAME</name> by hand if you want it gone"
	[ "$RELOAD" -eq 1 ] && pgrep -x labwc >/dev/null 2>&1 && labwc --reconfigure || true
	exit 0
fi

# -------------------------------------------------------------------- theme --
echo "Installing theme '$THEME_NAME' to $DEST_THEME"
mkdir -p -- "$DEST_THEME"
if [ -d "$DEST_THEME/labwc" ]; then
	BACKUP="$DEST_THEME/labwc.bak.$(date +%Y%m%d%H%M%S)"
	mv -- "$DEST_THEME/labwc" "$BACKUP"
	info "previous theme backed up to $BACKUP"
fi
cp -r -- "$SRC_THEME/labwc" "$DEST_THEME/labwc"
info "copied themerc + $(find "$DEST_THEME/labwc" -name '*.svg' | wc -l) button SVGs"

if [ "$MODE" = "theme-only" ]; then
	echo
	echo "Theme files installed. Still to do by hand:"
	echo "  1. set <theme><name>$THEME_NAME</name> in $RC_XML"
	echo "  2. merge the <blur> block from $PKG_DIR/rc.xml.snippet (needs a blur-capable labwc)"
	echo "  3. labwc --reconfigure"
	exit 0
fi

# -------------------------------------------------------------------- rc.xml --
if [ ! -f "$RC_XML" ]; then
	echo
	echo "No $RC_XML found — theme files are installed but rc.xml was not created."
	echo "Copy $PKG_DIR/rc.xml.snippet to $RC_XML, then run: labwc --reconfigure"
	exit 0
fi

RC_BACKUP="$RC_XML.bak.$(date +%Y%m%d%H%M%S)"
cp -- "$RC_XML" "$RC_BACKUP"
info "rc.xml backed up to $RC_BACKUP"

if ! command -v python3 >/dev/null 2>&1; then
	die "python3 is required to patch rc.xml safely; back it up (done) and paste $PKG_DIR/rc.xml.snippet by hand"
fi

THEME_NAME="$THEME_NAME" RC_XML="$RC_XML" BLUR_BLOCK="$BLUR_BLOCK" python3 <<'PY'
import os, re, sys

rc, theme, blur = os.environ["RC_XML"], os.environ["THEME_NAME"], os.environ["BLUR_BLOCK"]
with open(rc, encoding="utf-8") as f:
    xml = f.read()

if not re.search(r"<labwc_config", xml):
    sys.exit("error: %s does not look like a labwc config" % rc)

# Point the theme block at this theme (create the block if it is missing).
if re.search(r"<theme>.*?<name>.*?</name>.*?</theme>", xml, re.S):
    xml, n = re.subn(
        r"(<theme>.*?<name>)[^<]*(</name>)",
        lambda m: m.group(1) + theme + m.group(2),
        xml, count=1, flags=re.S)
    print("  theme name set to '%s'" % theme)
elif "<theme>" in xml:
    sys.exit("error: <theme> block found but has no <name>; set it to '%s' by hand" % theme)
else:
    xml = xml.replace("</labwc_config>", "  <theme>\n    <name>%s</name>\n  </theme>\n</labwc_config>" % theme, 1)
    print("  theme block created (name='%s')" % theme)

# Add the blur block only if the config has none.
if re.search(r"<blur>", xml):
    print("  <blur> already present, left as is")
else:
    xml = xml.replace("<labwc_config>", "<labwc_config>\n" + blur, 1)
    print("  <blur> block added")

with open(rc, "w", encoding="utf-8") as f:
    f.write(xml)
PY

# ------------------------------------------------------------------ override --
if [ -f "$THEMERC_OVERRIDE" ]; then
	echo
	echo "NOTE: $THEMERC_OVERRIDE exists and is read AFTER this theme."
	echo "      Anything set there wins over macos-dark. That is normally what you"
	echo "      want (e.g. a palette-sync tool regenerating it), but if your colours"
	echo "      look wrong, check that file first."
fi

# ------------------------------------------------------------------- reload --
echo
if [ "$RELOAD" -eq 1 ] && pgrep -x labwc >/dev/null 2>&1; then
	labwc --reconfigure && echo "labwc reloaded (SIGHUP)"
else
	echo "Run 'labwc --reconfigure' (or press your reload keybind) to apply."
fi

echo
echo "Done. Removing the theme again: $PKG_DIR/install.sh --uninstall"