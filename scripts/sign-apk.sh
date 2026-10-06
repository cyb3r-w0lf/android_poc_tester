#!/usr/bin/env bash
# Sign an APK with a throwaway "ctf" release key IF it is not already signed.
# Idempotent: an already-signed APK is left untouched. Needs Android build-tools
# (apksigner, zipalign) + keytool. Runs inside the device image, or on any host
# with the SDK. The signature key is disposable — a CTF only needs a valid sig.
#
# Usage:
#   sign-apk.sh <apk> [out.apk]      # sign one apk in place (or to out.apk)
#   sign-apk.sh --dir <dir>          # sign every unsigned *.apk in a directory, in place
set -euo pipefail

KS="${CTF_KEYSTORE:-/tmp/ctf-signing.jks}"
KS_PASS="${CTF_KS_PASS:-pentathon}"
ALIAS="ctf"
DNAME="${CTF_DNAME:-CN=pentathon-ctf}"

log() { printf '[sign-apk] %s\n' "$*"; }
die() { printf '[sign-apk] ERROR: %s\n' "$*" >&2; exit 1; }

# locate build-tools (latest)
find_buildtools() {
  local root="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-/opt/android}}"
  local bt
  bt=$(ls -d "$root"/build-tools/* 2>/dev/null | sort -V | tail -1) || true
  [ -n "$bt" ] || die "build-tools not found under $root (set ANDROID_SDK_ROOT)"
  APKSIGNER="$bt/apksigner"; ZIPALIGN="$bt/zipalign"
  [ -x "$APKSIGNER" ] || die "apksigner missing at $APKSIGNER"
  [ -x "$ZIPALIGN" ]  || die "zipalign missing at $ZIPALIGN"
}

ensure_keystore() {
  [ -f "$KS" ] && return 0
  command -v keytool >/dev/null 2>&1 || die "keytool not on PATH"
  log "Generating throwaway keystore (alias=$ALIAS) at $KS"
  keytool -genkeypair -keystore "$KS" -storepass "$KS_PASS" -keypass "$KS_PASS" \
    -alias "$ALIAS" -keyalg RSA -keysize 2048 -validity 3650 -dname "$DNAME" >/dev/null 2>&1 \
    || die "keystore generation failed"
}

is_signed() { "$APKSIGNER" verify "$1" >/dev/null 2>&1; }

sign_one() {
  local in="$1" out="${2:-$1}"
  [ -f "$in" ] || die "no such apk: $in"
  if is_signed "$in"; then
    log "already signed, skipping: $(basename "$in")"
    [ "$out" != "$in" ] && cp -f "$in" "$out"
    return 0
  fi
  ensure_keystore
  local tmp; tmp="$(mktemp --suffix=.apk)"
  log "zipalign + sign (unsigned): $(basename "$in")"
  "$ZIPALIGN" -f -p 4 "$in" "$tmp"
  "$APKSIGNER" sign --ks "$KS" --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" --out "$out" "$tmp"
  rm -f "$tmp" "$out.idsig" 2>/dev/null || true
  is_signed "$out" || die "signature verify failed after signing $out"
  log "signed -> $(basename "$out")"
}

main() {
  find_buildtools
  if [ "${1:-}" = "--dir" ]; then
    local dir="${2:?--dir needs a path}"; shopt -s nullglob
    for apk in "$dir"/*.apk; do sign_one "$apk"; done
  else
    [ $# -ge 1 ] || die "usage: sign-apk.sh <apk> [out.apk]  |  sign-apk.sh --dir <dir>"
    sign_one "$@"
  fi
}
main "$@"
