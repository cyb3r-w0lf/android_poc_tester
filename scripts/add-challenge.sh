#!/usr/bin/env bash
# Interactive wizard (run on the HOST by the challenge author) to scaffold a new challenge:
#   - copies the APK to device/challenges_apk/<name>.apk
#   - writes web/src/challenges/<name>/challenge.toml
#   - writes a commented setup.sh stub
# Needs podman + the built device image (default: android_env_device) for aapt auto-detection.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${DEVICE_IMAGE:-android_env_device}"

die() { printf '[add-challenge] ERROR: %s\n' "$*" >&2; exit 1; }
ask() { local v; read -r -p "$1" v; printf '%s' "$v"; }
# Escape a string for a TOML basic string.
toml_esc() { local s="$1"; s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "$s"; }

name="$(ask 'Challenge name [a-z0-9_-]: ')"
[[ "$name" =~ ^[a-z0-9_-]+$ ]] || die "invalid name (use only a-z, 0-9, _ and -)"

apk="$(ask 'Path to challenge APK: ')"
apk="${apk/#\~/$HOME}"
[ -f "$apk" ] || die "APK not found: $apk"
apk="$(cd "$(dirname "$apk")" && pwd)/$(basename "$apk")"

dest_apk="$ROOT/device/challenges_apk/$name.apk"
chal_dir="$ROOT/web/src/challenges/$name"
if [ -e "$dest_apk" ] || [ -e "$chal_dir" ]; then
  yn="$(ask "Challenge '$name' already exists. Overwrite? (y/N) ")"
  [[ "$yn" =~ ^[Yy]$ ]] || die "aborted, nothing changed"
fi

pkg=""
if command -v podman >/dev/null 2>&1; then
  badging="$(podman run --rm --entrypoint sh -v "$(dirname "$apk")":/apk:ro "$IMAGE" \
      -c 'aapt dump badging "/apk/$1"' _ "$(basename "$apk")" 2>/dev/null || true)"
  pkg="$(printf '%s\n' "$badging" | sed -n "s/^package: name='\([^']*\)'.*/\1/p" | head -n1)"
  if printf '%s\n' "$badging" | grep -q 'application-debuggable'; then
    echo "[add-challenge] WARNING: APK is DEBUGGABLE. The device refuses to boot with debuggable challenge APKs; ship a release build." >&2
  fi
fi
if [ -n "$pkg" ]; then
  echo "Detected package: $pkg"
else
  echo "Could not auto-detect the package name."
  pkg="$(ask 'Package name: ')"
fi
[[ "$pkg" =~ ^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$ ]] || die "invalid package name: $pkg"

port="$(ask 'Backend port? (blank=none): ')"
if [ -n "$port" ]; then
  [[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -gt 0 ] && [ "$port" -lt 65536 ] || die "invalid port"
fi

inputs=""
yn="$(ask 'Add player inputs? (y/N) ')"
if [[ "$yn" =~ ^[Yy]$ ]]; then
  while :; do
    iname="$(ask '  input name (blank to finish): ')"
    [ -n "$iname" ] || break
    [[ "$iname" =~ ^[A-Za-z0-9_]+$ ]] || { echo "  invalid name (use A-Za-z0-9_)"; continue; }
    ilabel="$(ask "  label [$iname]: ")"
    ilabel="${ilabel:-$iname}"
    inputs+=$'\n[[inputs]]\n'"name = \"$iname\""$'\n'"label = \"$(toml_esc "$ilabel")\""$'\n'"type = \"text\""$'\n'"required = false"$'\n'
  done
fi

mkdir -p "$ROOT/device/challenges_apk" "$chal_dir"
cp "$apk" "$dest_apk"

{
  echo "package_name     = \"$pkg\""
  echo "timeout          = 300"
  echo "# screenshot_delay = 5"
  if [ -n "$port" ]; then
    printf '\n[backend]\nport = %s\n' "$port"
  fi
  printf '%s' "$inputs"
} > "$chal_dir/challenge.toml"

if [ ! -e "$chal_dir/setup.sh" ]; then
cat > "$chal_dir/setup.sh" <<'STUB'
# setup.sh: OPTIONAL. Runs as ROOT inside the emulator guest (never on the host) before every job.
# Author-provided only; no player input is ever passed here. $FLAG is set from the env var
# CHALLENGE_FLAG_<NAME_UPPER> (also add it to the web service environment in docker-compose.yml).
#
# Example: plant a dynamic flag in the app's private dir.
# PKG=__PKG__
# D=/data/data/$PKG/files
# UID_=$(stat -c %u /data/data/$PKG)
# mkdir -p "$D" && echo "$FLAG" > "$D/flag.txt"
# chown -R "$UID_:$UID_" "$D"
STUB
sed -i "s/__PKG__/$pkg/" "$chal_dir/setup.sh"
fi

echo
echo "Created:"
echo "  $dest_apk"
echo "  $chal_dir/challenge.toml"
echo "  $chal_dir/setup.sh"
echo
echo "Next steps:"
echo "  1. Edit challenge.toml / setup.sh if needed (backend: set BACKEND_FORWARDS in .env)."
echo "  2. Rebuild so the files are baked in:  cd $ROOT && podman-compose up -d --build"
