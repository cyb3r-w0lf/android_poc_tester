#!/bin/bash

function kill_adb() {
  echo "[i] Killing ADB..."
  pkill -f "adb.*server" 2>/dev/null
  sleep 2
  pkill -9 -f "adb.*server" 2>/dev/null
};

function start_adb() {
  echo "[i] Starting ADB..."
  nohup adb -a -P 5037 nodaemon server > adb.log 2>&1 &
};

function kill_emulator() {
  echo "[i] Killing emulator..."
  pkill -f "qemu-system-x86_64.*${EMULATOR_NAME}" 2>/dev/null
  sleep 2
  pkill -9 -f "qemu-system-x86_64.*${EMULATOR_NAME}" 2>/dev/null
}

function start_emulator() {
  echo "[i] Starting emulator..."

  accel_option=""

  if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if [[ $(egrep -c '(vmx|svm)' /proc/cpuinfo) -gt 0 ]]; then
      accel_option="-accel on"
    else
      accel_option="-no-accel"
    fi
  elif [[ "$OSTYPE" == "darwin"* ]]; then
    accel_option="-accel on"
  elif [[ "$OSTYPE" == "msys" ]]; then
    accel_option="-accel hax"
  else
    accel_option="-no-accel"
  fi

  nohup emulator -avd "$EMULATOR_NAME" -writable-system -no-window -noaudio -no-boot-anim -gpu swiftshader_indirect -memory ${MAX_MEMORY:-4096} -no-snapshot-save -no-snapshot-load ${EMU_EXTRA_ARGS} $accel_option > emulator.log 2>&1 &
};

function wait_for_device() {
  echo "[i] Waiting for device..."
  adb wait-for-device
  while [ "$(adb get-state)" == "offline" ]; do
      sleep 1
  done
};

ADB_TCP_PORT=5555
LAMDA_PORT=65000
APP_UID_RANGE="10000-19999"
SU_REMOVE="${SU_REMOVE:-true}"
CHALLENGES_DIR="/app/challenges_apk"

# Fail closed: invalidate sentinel, stop the emulator, exit non-zero.
function fatal() {
  echo "[!] FATAL: $*" >&2
  rm -f /app/device_ready
  kill_emulator
  exit 1
}

# Layer 2: refuse to run if any baked challenge APK is debuggable (run-as => data read without root).
function assert_apks_not_debuggable() {
  shopt -s nullglob
  local apk
  for apk in "$CHALLENGES_DIR"/*.apk; do
    local badging
    badging=$(aapt dump badging "$apk" 2>/dev/null) || fatal "aapt failed on $apk"
    if echo "$badging" | grep -q "application-debuggable"; then
      fatal "$apk is DEBUGGABLE; ship a release (android:debuggable=false) build"
    fi
  done
}

function pkg_installed() {
  adb shell pm list packages 2>/dev/null | tr -d '\r' | grep -qx "package:$1"
}

# If an APK is unsigned, sign it in place with a throwaway "ctf" release key
# (any valid signature is enough to install). Idempotent: signed APKs untouched.
CTF_KEYSTORE="/tmp/ctf-signing.jks"
CTF_KS_PASS="pentathon"
function ensure_signed() {
  local apk="$1"
  if apksigner verify "$apk" >/dev/null 2>&1; then return 0; fi
  echo "[i] $(basename "$apk") is unsigned; signing with throwaway 'ctf' key..."
  if [ ! -f "$CTF_KEYSTORE" ]; then
    keytool -genkeypair -keystore "$CTF_KEYSTORE" -storepass "$CTF_KS_PASS" -keypass "$CTF_KS_PASS" \
      -alias ctf -keyalg RSA -keysize 2048 -validity 3650 -dname "CN=pentathon-ctf" >/dev/null 2>&1 \
      || fatal "keystore generation failed"
  fi
  local tmp="${apk%.apk}-aligned.apk"
  zipalign -f -p 4 "$apk" "$tmp" || fatal "zipalign failed on $apk"
  apksigner sign --ks "$CTF_KEYSTORE" --ks-pass "pass:$CTF_KS_PASS" --key-pass "pass:$CTF_KS_PASS" \
    --out "$apk" "$tmp" || fatal "signing failed on $apk"
  rm -f "$tmp" "$apk.idsig"
  apksigner verify "$apk" >/dev/null 2>&1 || fatal "signature invalid after signing $apk"
}

# Install baked challenge APKs (idempotent). Plugin folder name == apk basename.
function install_challenges() {
  shopt -s nullglob
  local apk
  for apk in "$CHALLENGES_DIR"/*.apk; do
    local pkg
    pkg=$(aapt dump badging "$apk" 2>/dev/null | sed -n "s/^package: name='\([^']*\)'.*/\1/p" | head -n1)
    [ -n "$pkg" ] || fatal "cannot parse package name of $apk"
    if pkg_installed "$pkg"; then
      echo "[i] $pkg already installed, skipping ($(basename "$apk"))"
    else
      echo "[i] Installing challenge $(basename "$apk") ($pkg)..."
      ensure_signed "$apk"
      adb install -r "$apk" || fatal "failed to install $apk"
    fi
    pkg_installed "$pkg" || fatal "$pkg not present after install"
    # Re-check on-device flags too.
    if adb shell dumpsys package "$pkg" | grep -E "pkgFlags|flags=" | grep -q "DEBUGGABLE"; then
      fatal "$pkg is installed as DEBUGGABLE"
    fi
    CHALLENGE_PKGS="${CHALLENGE_PKGS:-} $pkg"
  done
}

# Layer 3: lock su (random name, root:shell 6750) and, by default, delete it entirely.
function neuter_su() {
  local name="${SU_NAME:-su-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
  local found=0 dir
  # The early `adb remount` is undone by the reboot in the setup flow; re-make
  # the system overlay writable, else mv/rm of su fails with "Read-only file system".
  adb root >/dev/null 2>&1
  for p in /system /system_ext; do adb shell "mount -o rw,remount $p" >/dev/null 2>&1; done
  for dir in /system/xbin /system/bin; do
    if adb shell "[ -e $dir/su ]"; then
      found=1
      adb shell "mv $dir/su $dir/$name" || fatal "cannot rename su in $dir"
      adb shell "chown root:shell $dir/$name && chmod 6750 $dir/$name" || fatal "cannot lock su in $dir"
      if [ "$SU_REMOVE" == "true" ]; then
        adb shell "rm -f $dir/$name" || fatal "cannot remove su in $dir"
      fi
    fi
  done
  [ "$found" == "0" ] && echo "[i] No su binary found (nothing to lock)"
  # Assert: no su under its default name; if kept, it is exactly 6750 root:shell.
  for dir in /system/xbin /system/bin /system/sbin /sbin /vendor/bin; do
    adb shell "[ -e $dir/su ]" && fatal "su still present at $dir/su"
  done
  if [ "$SU_REMOVE" != "true" ]; then
    for dir in /system/xbin /system/bin; do
      if adb shell "[ -e $dir/$name ]"; then
        [ "$(adb shell "stat -c '%a %U:%G' $dir/$name" | tr -d '\r')" == "6750 root:shell" ] || fatal "su perms wrong at $dir/$name"
      fi
    done
  fi
}

# The LISTEN ports configured in BACKEND_FORWARDS (challenge backends the app may reach).
function forward_listen_ports() {
  local spec entry listen
  spec="${BACKEND_FORWARDS:-}"
  for entry in $(echo "$spec" | tr ',' ' '); do
    case "$entry" in
      *:*:*) listen="${entry%%:*}" ;;
      *)     listen="$entry" ;;
    esac
    [ -n "$listen" ] && echo "$listen"
  done
}

# Insert a rule at the TOP of OUTPUT, idempotently. $1=iptables|ip6tables, rest=rule (no "OUTPUT").
function _ins_output() {
  local b="$1"; shift
  adb shell "$b -C OUTPUT $*" >/dev/null 2>&1 && return 0
  adb shell "$b -I OUTPUT $*" >/dev/null 2>&1
}

# Layer 3: network policy for untrusted app UIDs (10000-19999).
#  1. No root control plane: guest LAMDA RPC (65000) + adbd (5555); and the container host
#     (10.0.2.2) which exposes the container adb server (5037) and LAMDA forward (65010).
#  2. EGRESS LOCKDOWN (EGRESS_LOCKDOWN=true, default): an app may reach ONLY the challenge
#     backend forward, any operator-allowlisted hosts, and loopback — everything else
#     (internet, other cluster pods) is rejected. Cloud metadata (169.254.0.0/16) is ALWAYS
#     rejected. This neutralises SSRF regardless of any attacker-supplied URL.
# Configurable (env):
#   EGRESS_ALLOW        comma/space list of IPs/CIDRs apps may reach (e.g. the backend host IP)
#   EGRESS_ALLOW_PORTS  restrict EGRESS_ALLOW to a port/range (e.g. 1024:65535); empty = all
#   EGRESS_DENY_PORTS   ports blocked even on allowlisted hosts (e.g. 22,3306)
# Final top-to-bottom order: REJECT metadata | REJECT deny-ports | ACCEPT challenge-uid->backend
#   | ACCEPT allow-IPs[:ports] | REJECT ->10.0.2.2 | REJECT 65000/5555 | ACCEPT ->127/8 | REJECT all
# Validate operator-supplied egress tokens before they reach a root shell (defense vs footgun/injection).
function _valid_ipcidr() { echo "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$'; }
function _valid_portspec() { echo "$1" | grep -Eq '^[0-9]{1,5}(:[0-9]{1,5})?$'; }

function block_app_ports() {
  local ipt port lp gw cuid cpkg ip prt
  local R="-m owner --uid-owner $APP_UID_RANGE"
  gw="10.0.2.2"
  local pflt=""
  if [ -n "${EGRESS_ALLOW_PORTS:-}" ]; then
    _valid_portspec "${EGRESS_ALLOW_PORTS}" || fatal "invalid EGRESS_ALLOW_PORTS: ${EGRESS_ALLOW_PORTS}"
    pflt="-p tcp --dport ${EGRESS_ALLOW_PORTS}"
  fi

  # Inserted in REVERSE priority (last insert = top). Final top-to-bottom order:
  #   REJECT metadata | REJECT deny-ports | ACCEPT challenge-uid->backend | REJECT ->10.0.2.2
  #   | REJECT 65000/5555 | ACCEPT allow-IPs[:ports] | ACCEPT ->127/8 | REJECT all (tcp/udp/v6)
  # Note: allow-IPs sit BELOW the gateway/root-port rejects, so an EGRESS_ALLOW that accidentally
  # covers 10.0.2.2 / 5037 / 65010 / 65000 / 5555 cannot re-open the root control plane.
  if [ "${EGRESS_LOCKDOWN:-true}" != "false" ]; then
    # bottom-most: deny everything else for app UIDs (kills SSRF to internet/cluster). Fail CLOSED.
    _ins_output ip6tables "$R -j REJECT" || fatal "cannot set ip6tables egress deny"
    _ins_output iptables  "-p udp $R -j REJECT" || fatal "cannot set app udp egress deny"
    _ins_output iptables  "-p tcp $R -j REJECT" || fatal "cannot set app tcp egress deny"
    # allow benign loopback (an app's own sockets); guest root ports are rejected above this
    _ins_output iptables  "-d 127.0.0.0/8 $R -j ACCEPT" || echo "[w] loopback allow not set"
    # allow DNS ONLY to the emulator's built-in resolver (10.0.2.3:53). Android/okhttp needs the
    # resolver to set up connections (even to a literal IP), so blocking all UDP breaks the app.
    # Everything else (incl. DNS to any other server) stays blocked by the catch-all below.
    _ins_output iptables "-d 10.0.2.3 -p udp --dport 53 $R -j ACCEPT" || echo "[w] dns-udp allow not set"
    _ins_output iptables "-d 10.0.2.3 -p tcp --dport 53 $R -j ACCEPT" || echo "[w] dns-tcp allow not set"
  fi

  # operator allowlist (kept BELOW the control-plane rejects below): reachable IPs/CIDRs
  for ip in $(echo "${EGRESS_ALLOW:-}" | tr ',' ' '); do
    [ -n "$ip" ] || continue
    _valid_ipcidr "$ip" || fatal "invalid EGRESS_ALLOW entry: $ip"
    _ins_output iptables "-d $ip $pflt $R -j ACCEPT" || fatal "cannot allow egress to $ip"
    echo "[i]   allow app UIDs -> $ip ${EGRESS_ALLOW_PORTS:+ports ${EGRESS_ALLOW_PORTS}}"
  done

  # guest-local root ports (also covers loopback access to them)
  for ipt in iptables ip6tables; do
    for port in $LAMDA_PORT $ADB_TCP_PORT; do
      _ins_output "$ipt" "-p tcp --dport $port $R -j REJECT" || {
        [ "$ipt" == "ip6tables" ] && { echo "[w] $ipt reject $port not set"; continue; }
        fatal "cannot set iptables reject for port $port"
      }
    done
  done

  # container host (10.0.2.2) default-deny for app UIDs (closes 5037/65010/etc)
  _ins_output iptables "-d $gw -p tcp $R -j REJECT" || fatal "cannot set gateway default-deny"
  adb shell "iptables -C OUTPUT -d $gw -p tcp $R -j REJECT" >/dev/null 2>&1 || fatal "gateway deny not present"

  # backend forward: only the challenge app's own UID (POC of a different UID cannot reach it).
  # Sits ABOVE the gateway reject so the challenge app can still reach its backend.
  for cpkg in ${CHALLENGE_PKGS:-}; do
    cuid=$(adb shell "stat -c %u /data/data/$cpkg" 2>/dev/null | tr -d '\r')
    [ -n "$cuid" ] || { echo "[w] could not resolve uid for $cpkg"; continue; }
    for lp in $(forward_listen_ports); do
      _ins_output iptables "-d $gw -p tcp --dport $lp -m owner --uid-owner $cuid -j ACCEPT" \
        || fatal "cannot allow backend port $lp for uid $cuid"
      echo "[i]   allow uid $cuid ($cpkg) -> ${gw}:${lp} (backend)"
    done
  done

  # excluded ports: blocked even on allowlisted hosts (inserted ABOVE the accepts)
  for prt in $(echo "${EGRESS_DENY_PORTS:-}" | tr ',' ' '); do
    [ -n "$prt" ] || continue
    _valid_portspec "$prt" || fatal "invalid EGRESS_DENY_PORTS entry: $prt"
    _ins_output iptables "-p tcp --dport $prt $R -j REJECT" || fatal "cannot deny egress port $prt"
    echo "[i]   deny app UIDs -> port $prt (excluded)"
  done

  # cloud metadata: ALWAYS blocked, top-most priority. Fail CLOSED.
  _ins_output iptables "-d 169.254.0.0/16 $R -j REJECT" || fatal "cannot set metadata reject"

  echo "[i] app-UID egress: lockdown=${EGRESS_LOCKDOWN:-true} allow=[${EGRESS_ALLOW:-}] allow_ports=[${EGRESS_ALLOW_PORTS:-all}] deny_ports=[${EGRESS_DENY_PORTS:-}]"
}

# Layer 1: SELinux must be enforcing.
function enforce_selinux() {
  adb shell setenforce 1
  [ "$(adb shell getenforce | tr -d '\r\n')" == "Enforcing" ] || fatal "SELinux is not Enforcing"
}

function start_device() {
  rm -f /app/device_ready

  # On restarts wipe userdata (clean state); first boot starts from the fresh AVD anyway.
  if [ "$1" != "true" ]; then EMU_EXTRA_ARGS="-wipe-data"; else EMU_EXTRA_ARGS=""; fi

  assert_apks_not_debuggable

  start_emulator
  wait_for_device

  if [ "$1" == "true" ]; then
    echo "[i] Disabling security (setup only; SELinux stays enforcing)..."

    adb root
    sleep 2.5

    adb shell avbctl disable-verification
    adb disable-verity

    echo "[i] Rebooting device..."
    adb reboot

    wait_for_device
  fi

  while [ "$(adb shell getprop sys.boot_completed 2>&1 | tr -d '\r')" != "1" ]; do
      sleep 1
  done

  echo "[i] Device booted!"

  echo "[i] Setting up device..."

  adb root
  sleep 2.5

  echo "[i] Mounting system..."

  adb remount
  sleep 2.5

  enforce_selinux

  echo "[i] Installing challenge APKs..."
  install_challenges

  echo "[i] Setting up lamda..."

  adb push /lamda-server-x86_64.tar.gz /data
  adb shell tar -zxf /data/lamda-server-x86_64.tar.gz -C /data
  adb shell chmod +x /data/server/bin/launch.sh
  adb shell "cd /data/server/bin; ./launch.sh"
  adb forward tcp:65010 tcp:65000
  pkill -f "socat TCP-LISTEN:65000" 2>/dev/null
  nohup socat TCP-LISTEN:65000,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:65010 > socat.log 2>&1 &

  echo "[i] Blocking app UIDs from lamda/adb ports..."
  block_app_ports

  echo "[i] Neutering 'su'..."
  neuter_su

  echo "[i] Disabling virtual keyboard..."

  adb shell pm disable-user com.google.android.inputmethod.latin
  adb shell pm disable-user com.google.android.tts
  adb shell pm disable-user com.google.android.googlequicksearchbox

  echo "[i] Reducing ANR dialogs + animation load..."
  # Don't pop ANR dialogs for background apps, and kill animations so the
  # emulator is less likely to stall and raise "isn't responding" under load.
  adb shell settings put secure anr_show_background 0 2>/dev/null || true
  adb shell settings put global window_animation_scale 0 2>/dev/null || true
  adb shell settings put global transition_animation_scale 0 2>/dev/null || true
  adb shell settings put global animator_duration_scale 0 2>/dev/null || true

  echo "[i] Starting backend forwards..."
  start_backend_forwards

  # Final re-assertion immediately before opening the gate.
  enforce_selinux
  assert_apks_not_debuggable

  echo 1 > /app/device_ready
  echo "[i] Device is ready!"
};

# Forward ports from inside the container (reachable by the emulator as 10.0.2.2:<port>)
# to a backend a challenge app needs. Configure with BACKEND_FORWARDS: a comma/space
# separated list of entries. Each entry is either "PORT" (forwards to
# host.containers.internal:PORT) or "LISTEN:HOST:PORT". Example:
#   BACKEND_FORWARDS="3014"                       -> 10.0.2.2:3014 -> host:3014
#   BACKEND_FORWARDS="3014:host.containers.internal:3014, 9000:10.89.0.9:9000"
function start_backend_forwards() {
  local spec entry listen thost tport
  spec="${BACKEND_FORWARDS:-}"
  [ -z "$spec" ] && { echo "[i] No BACKEND_FORWARDS configured."; return 0; }
  for entry in $(echo "$spec" | tr ',' ' '); do
    case "$entry" in
      *:*:*) listen="${entry%%:*}"; thost="$(echo "$entry" | cut -d: -f2)"; tport="${entry##*:}" ;;
      *) listen="$entry"; thost="host.containers.internal"; tport="$entry" ;;
    esac
    # Validate before these reach a shell (operator footgun / injection guard).
    echo "$listen" | grep -Eq '^[0-9]{1,5}$' || fatal "invalid BACKEND_FORWARDS listen port: $listen"
    echo "$tport"  | grep -Eq '^[0-9]{1,5}$' || fatal "invalid BACKEND_FORWARDS target port: $tport"
    echo "$thost"  | grep -Eq '^[A-Za-z0-9._-]+$' || fatal "invalid BACKEND_FORWARDS host: $thost"
    pkill -f "TCP-LISTEN:${listen}," 2>/dev/null
    setsid socat TCP-LISTEN:${listen},fork,reuseaddr "TCP:${thost}:${tport}" >/dev/null 2>&1 &
    echo "[i]   forward 10.0.2.2:${listen} -> ${thost}:${tport}"
  done
}

function main() {
  kill_adb
  start_adb
  kill_emulator
  start_device true

  while true; do
    if ! pgrep -f "qemu-system-x86_64.*${EMULATOR_NAME}" > /dev/null; then
      echo "[i] Main emulator process not running, restarting emulator..."
      kill_emulator
      sleep 2.5
      start_device false
    fi

    if ! pgrep -f "adb.*server" > /dev/null; then
      echo "[i] ADB server not running, restarting emulator..."
      kill_emulator
      sleep 2.5
      start_device false
    fi

    local connected_devices=$(adb devices | grep emulator | grep device | wc -l)
    if [[ $connected_devices -eq 0 ]]; then
      echo "[i] No devices connected, restarting emulator..."
      kill_emulator
      sleep 2.5
      start_device false
    else
      local device_id=$(adb devices | grep emulator | grep device | head -n1 | cut -f1)
      if ! timeout 10 adb -s "$device_id" shell echo "test" > /dev/null 2>&1; then
        echo "[i] Device unresponsive, restarting emulator..."
        kill_emulator
        sleep 2.5
        start_device false
      fi
    fi

    if ! timeout 5 curl -s --connect-timeout 3 http://localhost:65000 > /dev/null 2>&1; then
      echo "[i] Lamda service not responding, restarting emulator..."
      kill_emulator
      sleep 2.5
      start_device false
    fi

    sleep 5
  done
};

main
