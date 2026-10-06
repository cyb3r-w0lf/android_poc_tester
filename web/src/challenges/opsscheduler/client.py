# Challenge plugin: opsscheduler
# CHALLENGE_NAME is set by the loader to the folder name ("opsscheduler").
#
# How this challenge works (from reversing the APK):
#   - AdminActivity.fetchNonce() GETs  <url>/admin/nonce  where <url> is read from
#     SharedPreferences file "pp", key "url" (empty by default -> MalformedURLException).
#   - That pref is only written by MainActivity's Save button; there is no IPC to set it.
#   - The player's POC exploits an exported CallbackActivity that hands back a MUTABLE
#     PendingIntent to the non-exported AdminActivity, and sends it with verified=true.
#   - So the harness (root, via LAMDA) pre-sets the dispatch URL to the backend; the POC
#     performs the PendingIntent exploit; AdminActivity then fetches the flag from the backend.
#
# Backend reachability: the emulator reaches the host backend as 10.0.2.2:<port> when
# BACKEND_FORWARDS is configured (e.g. BACKEND_FORWARDS=3014 in .env).

import os
import re

PACKAGE_NAME = "com.pentathon.opsscheduler"
TIMEOUT = 300
SCREENSHOT_DELAY = 10   # fetchNonce is async (network) — allow the token to render

# Default dispatch server if the player leaves the fields blank. OPSSCHED_BACKEND overrides.
DISPATCH_URL = os.environ.get("OPSSCHED_BACKEND", "http://10.0.2.2:3014")

# PLAYER-SUPPLIED dispatch server (host + port). The challenge app fetches <url>/admin/nonce
# from it. SECURITY: because the player controls the host, pair this with EGRESS_ALLOW so the
# emulator can only reach the intended backend IP(s) — otherwise this is an SSRF primitive.
INPUTS = [
    {"name": "host", "label": "Server host", "type": "text", "required": False,
     "default": "10.0.2.2", "placeholder": "10.0.2.2"},
    {"name": "port", "label": "Server port", "type": "number", "required": False, "default": "3014"},
]

# Allowed host chars (no shell/XML metacharacters) and the final-URL shape.
_HOST_RE = re.compile(r'^[A-Za-z0-9.\-]{1,255}$')
_URL_RE = re.compile(r'^https?://[A-Za-z0-9._:\-/]{1,200}$')


def callback(poc_app, update_status, inputs=None, device=None):
    inputs = inputs or {}
    host = str(inputs.get("host") or "").strip()
    port = str(inputs.get("port") or "").strip()
    if host and port:
        if not _HOST_RE.match(host):
            raise Exception("invalid server host")
        if not port.isdigit() or not (0 < int(port) < 65536):
            raise Exception("invalid server port")
        url = f"http://{host}:{port}"
    else:
        url = DISPATCH_URL
    if not _URL_RE.match(url):
        raise Exception("invalid dispatch server URL")

    # Pre-set the dispatch server in the challenge app's private SharedPreferences (root).
    if device is not None:
        sp = f"/data/data/{PACKAGE_NAME}/shared_prefs"
        xml = (
            "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n"
            "<map>\n"
            f'    <string name="url">{url}</string>\n'
            "</map>\n"
        )
        script = (
            "set -e\n"
            f"mkdir -p {sp}\n"
            f"cat > {sp}/pp.xml <<'PPEOF'\n{xml}PPEOF\n"
            f"uid=$(stat -c %u /data/data/{PACKAGE_NAME})\n"
            f"chown $uid:$uid {sp} {sp}/pp.xml\n"
            f"chmod 771 {sp}; chmod 660 {sp}/pp.xml\n"
            f"restorecon -R {sp} 2>/dev/null || true\n"
        )
        device.execute_script(script, timeout=15)
    
    # Launch the player's POC; it performs the exported-CallbackActivity /
    # mutable-PendingIntent exploit to reach AdminActivity, which then fetches the flag.
    poc_app.start()
