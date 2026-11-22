#!/usr/bin/env bash
# Builds the Android app (a Trusted Web Activity for the dashboard's site) and signs it with the upload key:
#   build-out/wealth-ledger-<version>.aab   upload this to Google Play
#   build-out/wealth-ledger-<version>.apk   install directly for testing (adb install -r ...)
# The keystore password comes from the GNOME login keyring (secret-tool, service=wealth-ledger-android key=upload)
# and reaches apksigner/jarsigner through an environment variable, never a command line. See README.md.
set -euo pipefail
cd "$(dirname "$0")"
KEYSTORE="${WEALTH_LEDGER_UPLOAD_KEYSTORE:-$HOME/.config/wealth-ledger/android/upload-keystore.jks}"
BW="${BUBBLEWRAP:-$HOME/.bubblewrap/cli/node_modules/.bin/bubblewrap}"
CONF="$HOME/.bubblewrap/config.json"
JDK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["jdkPath"])' "$CONF")"
SDK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["androidSdkPath"])' "$CONF")"
TOOLS="$(ls -d "$SDK"/build-tools/* 2>/dev/null | sort -V | tail -1 || true)"
VERSION="$(python3 -c 'import json; m = json.load(open("twa-manifest.json")); print(m["appVersionName"], m["appVersionCode"], sep="-")')"

[ -r "$KEYSTORE" ] || { echo "no upload keystore at $KEYSTORE" >&2; exit 1; }
KS_PASS="$(secret-tool lookup service wealth-ledger-android key upload)" || { echo "keystore password not in the keyring" >&2; exit 1; }
export KS_PASS

# 1. unsigned release APK (zip-aligned) and app bundle; Bubblewrap regenerates the project first if twa-manifest.json
#    changed (run "$BW update" yourself to review that), and installs the Android build-tools it needs
"$BW" build --skipSigning
TOOLS="$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)"

# 2. sign both with the upload key
mkdir -p build-out
APK="build-out/wealth-ledger-$VERSION.apk" AAB="build-out/wealth-ledger-$VERSION.aab"
rm -f "$APK" "$AAB" "$APK.idsig"
"$TOOLS/apksigner" sign --ks "$KEYSTORE" --ks-key-alias upload --ks-pass env:KS_PASS --key-pass env:KS_PASS \
  --out "$APK" app-release-unsigned-aligned.apk
"$JDK/bin/jarsigner" -keystore "$KEYSTORE" -storepass:env KS_PASS -keypass:env KS_PASS -sigalg SHA256withRSA \
  -digestalg SHA-256 -signedjar "$AAB" app/build/outputs/bundle/release/app-release.aab upload >/dev/null
rm -f app-release-unsigned-aligned.apk "$APK.idsig"

# 3. check
"$TOOLS/apksigner" verify --print-certs "$APK" | grep -E "certificate SHA-256 digest"
"$JDK/bin/jarsigner" -verify "$AAB" | tail -1
ls -l "$APK" "$AAB"
