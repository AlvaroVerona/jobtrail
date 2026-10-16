#!/usr/bin/env bash
set -euo pipefail

# Builds jobtrail.app from this checkout. The app is a thin native shell: it starts the jobtrail
# server from this project's own virtualenv and shows its web UI in a window. It is built from source
# and ad-hoc signed, for use on the Mac that built it; it is not notarized for distribution.
#
#   macos/Scripts/build-app.sh            # release build into macos/.build/app/jobtrail.app
#   macos/Scripts/build-app.sh debug

cd "$(dirname "$0")/.."            # macos/
REPO_ROOT="$(cd .. && pwd)"        # the jobtrail project the app will run

APP_NAME="Jobtrail"                # the executable
BUNDLE="jobtrail.app"
CONFIGURATION="${1:-release}"
OUTPUT=".build/app"

if [ ! -x "$REPO_ROOT/.venv/bin/jobtrail" ]; then
  echo "==> Installing jobtrail with uv (first time only)..."
  (cd "$REPO_ROOT" && uv sync)
fi

echo "==> Building the app ($CONFIGURATION)..."
swift build -c "$CONFIGURATION"
BINARY="$(swift build -c "$CONFIGURATION" --show-bin-path)/$APP_NAME"
[ -f "$BINARY" ] || { echo "error: built binary not found at $BINARY" >&2; exit 1; }

echo "==> Assembling $BUNDLE..."
rm -rf "$OUTPUT/$BUNDLE"
mkdir -p "$OUTPUT/$BUNDLE/Contents/MacOS" "$OUTPUT/$BUNDLE/Contents/Resources"
cp "$BINARY" "$OUTPUT/$BUNDLE/Contents/MacOS/$APP_NAME"
cp Resources/Info.plist "$OUTPUT/$BUNDLE/Contents/Info.plist"
cp Resources/AppIcon.icns "$OUTPUT/$BUNDLE/Contents/Resources/AppIcon.icns"

# Tell the app which project to run. Moving the project later means running this script again.
/usr/libexec/PlistBuddy -c "Add :JobtrailRepoPath string $REPO_ROOT" "$OUTPUT/$BUNDLE/Contents/Info.plist"

echo "==> Ad-hoc code signing (local use only)..."
codesign --force --deep --sign - "$OUTPUT/$BUNDLE"

echo "==> Done: macos/$OUTPUT/$BUNDLE"
echo "    Run:      open \"macos/$OUTPUT/$BUNDLE\""
echo "    Install:  cp -R \"macos/$OUTPUT/$BUNDLE\" /Applications/"
echo "    Project:  $REPO_ROOT"
