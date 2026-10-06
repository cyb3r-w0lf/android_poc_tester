# android_env — Dockerized Android CTF POC Runner

A platform for **mobile CTF challenges**. Players get a handout APK (with a **fake flag**),
reverse/exploit it locally, then upload their **POC APK** to this site. The site installs the
POC next to the **real-flag challenge app** running in a shared, hardened **Android 14 (API 34)**
emulator, runs it, and returns a **screenshot** as proof. Device automation is **LAMDA (firerpa)**.
Runs under **podman**.

> Threat model in one line: the real flag lives only inside the server's challenge app and is
> reachable only by actually exploiting it at runtime. Reversing the handout never yields the real flag.

---

## 1. Architecture

```
            ┌─────────────────────────┐        ┌──────────────────────────────┐
  browser ──┤  web  (Flask+SocketIO)  │  gRPC  │  device  (Android 14 emulator)│
   :5000    │  upload, PoW, job queue ├────────┤  LAMDA server :65000          │
            │  screenshot serving     │  65000 │  challenge app + POC app      │
            └─────────────────────────┘        └──────────────────────────────┘
```

- **web** container: Flask + Flask-SocketIO. Upload endpoint, PoW gate, single-worker job
  queue (one shared emulator), per-job timeout, live status over SocketIO, screenshot serving.
- **device** container: privileged, boots a rooted AVD, installs the baked challenge APK(s),
  launches the LAMDA server, applies the boot-time security hardening, exposes LAMDA on `:65000`.
- **Device control**: LAMDA (firerpa) v8.40 over gRPC — not raw ADB for the app workflow.

### Job flow
`upload` → queue (`PENDING`) → `INITIALIZING` (get challenge app; install if missing)
→ `INSTALLING_POC` (aapt parse pkg; reject if POC pkg == challenge pkg; install POC)
→ run per-challenge `callback(poc_app, update_status)` → `TAKING_SCREENSHOT` → `COMPLETED`
→ POC uninstalled. Status streamed to the browser over SocketIO; screenshot at `/screenshot/<id>`.

---

## 2. Prerequisites

- Linux host with **KVM** (`/dev/kvm` present and accessible) and CPU virtualization (VT-x/AMD-V).
- **podman** + **podman-compose** (tested: podman 6.x, podman-compose 1.6).
  - Use `podman-compose`, *not* `podman compose` — the latter needs the podman API socket.
- ~**15 GB** free disk for images (Android SDK + API-34 system image are large).
  Rootless podman stores under `~/.local/share/containers`; move it to a bigger disk if needed
  (see `scripts/move-podman-storage.sh`).

Check:
```bash
ls -l /dev/kvm && grep -cE 'vmx|svm' /proc/cpuinfo
podman-compose --version
```

---

## 3. Build & deploy

```bash
cd android_env
cp env.example .env           # set SECRET_KEY, tune PoW, etc.
./run.sh                      # writes a random SU_NAME to .env, then podman-compose up --build
```

Or step by step:
```bash
podman-compose build          # device image pulls the Android SDK + API-34 image (slow, one-time)
podman-compose up -d
podman-compose logs -f device # watch boot + hardening asserts
```

First boot: image build is minutes; the emulator cold-boots and runs hardening for a few more
minutes. The **web** container gates uploads until the device is ready.

### Readiness
```bash
podman exec android_env_device_1 cat /app/device_ready     # -> 1 when ready
curl -s http://localhost:5000/device_status                # -> {"device_ready":true}
```
Open **http://localhost:5000**.

> Note: the `/app/device_ready` sentinel is written when the LAMDA HTTP port answers; the LAMDA
> *script* API warms up a few seconds later, so a page load in that window may briefly show
> "Device is not ready". It clears on the next poll (5 s).

### Stop / reset
```bash
podman-compose down            # stop
podman-compose down && podman-compose up -d --build   # rebuild + restart
```

---

## 4. Adding a challenge

Each challenge = **one baked real-flag APK** + **one plugin folder**.

1. **Build two APKs** (author's job), identical except the flag constant:
   - `fake-flag.apk` → the **handout** given to players (outside this system).
   - real-flag APK → goes into the server image.
   - The server APK **must be a release build**: `android:debuggable="false"` (and ideally
     `android:allowBackup="false"`). A debuggable APK is **rejected at boot** — because
     `run-as <pkg>` would read its private data (the flag) with no exploit at all.

2. **Drop the real-flag APK**: `device/challenges_apk/<name>.apk`
   (unsigned is fine — it's auto-signed at boot with a throwaway key; see §6).

3. **Create the plugin**: `web/src/challenges/<name>/client.py`
   ```python
   PACKAGE_NAME = "com.example.challenge"   # required: the vulnerable app's package
   TIMEOUT = 300                            # optional, seconds (default 300)
   SCREENSHOT_DELAY = 5                     # optional, per-challenge screenshot delay
   # CHALLENGE_NAME is set by the loader to the folder name.

   # Optional: admin-defined inputs the player fills in on the upload page.
   # Types: text, number, password, textarea, select (with "options").
   INPUTS = [
       {"name": "host", "label": "C2 Host", "type": "text", "required": True,
        "default": "10.0.2.2", "placeholder": "10.0.2.2"},
       {"name": "port", "label": "Port", "type": "number", "required": True, "default": "8080"},
       {"name": "api_key", "label": "API Key", "type": "password", "required": False},
       {"name": "mode", "label": "Mode", "type": "select", "options": ["fast", "slow"], "default": "fast"},
   ]

   def callback(poc_app, update_status, inputs=None):   # optional; `inputs` optional too
       # poc_app: LAMDA application object for the player's POC
       #   .start() .stop() .is_installed() .uninstall()
       # update_status(Status.X): push a status to the player
       # inputs: dict of the fields above, e.g. inputs["host"], inputs["port"] (may be {})
       inputs = inputs or {}
       poc_app.start()
       # add a settle delay / wait-for-UI here if the proof needs the POC's screen
   ```
   - Declare `INPUTS` to collect per-challenge values from the player (host:port, keys, …).
     The upload page renders them; values are validated (required/number/select, max 1 KB each)
     and passed to `callback` as the `inputs` dict. A `callback(poc_app, update_status)` with no
     third parameter still works — inputs are only passed when the callback accepts them.
   - Use the values to configure the run: pass as intent extras, write a config the app reads, etc.

4. **Rebuild** so both files bake in (`COPY` into the images):
   ```bash
   podman-compose up -d --build
   ```

The folder name, the APK basename, and `PACKAGE_NAME` should refer to the same challenge.

---

## 5. Using it (players)

1. Open the site, pick the challenge, upload the POC `.apk`.
2. If PoW is enabled, solve it with the command the page shows — a **self-hosted** solver (no
   external domain): `curl -s http://<your-site>/solve | python3 - <token>`. Paste the result.
3. Watch live status; when `COMPLETED`, click **View Screenshot** — that is the proof.

Rules enforced: `.apk` only, max file size, max queue size, and the POC's package may **not**
equal the challenge's package (no impersonation). The POC is uninstalled after each run.

**Per-job device logs:** each job captures `adb logcat` filtered to the challenge + POC packages
(and their pids). The status card shows a **Logs** panel (View / Refresh) once the job finishes,
served from `GET /logs/<job-id>` (text/plain). Logcat is cleared at the start of each job, and
captured even if the POC callback errors — useful for debugging a failing exploit.

---

## 6. APK signing

`adb install` needs a validly signed APK. Unsigned challenge APKs are auto-signed at boot with a
disposable key (alias `ctf`) — a CTF only needs *a* valid signature. This is handled by
`ensure_signed()` in `device/start.sh`. To pre-sign on the host:
```bash
# inside the device image (has build-tools), or any host with the Android SDK:
scripts/sign-apk.sh device/challenges_apk/<name>.apk
scripts/sign-apk.sh --dir device/challenges_apk        # sign every unsigned apk in a dir
```

---

## 7. Security hardening (root prevention)

If a POC gets root — or reads another app's `/data/data` — it reads the flag directly, bypassing
the intended exploit. Defense in depth, enforced at boot (`start.sh` fails closed — no
`/app/device_ready` unless every check passes):

1. **SELinux Enforcing** — blocks cross-app private-dir reads (primary control).
2. **Non-debuggable assert** — every baked challenge APK is checked with `aapt`; debuggable → abort.
3. **su neutered** — renamed/removed (`SU_REMOVE=true` default); app UIDs can't exec it anyway
   (AOSP su is `root:shell`, SELinux `su_exec`).
4. **Port isolation** — `iptables -m owner` REJECTs app UIDs `10000–19999` from the LAMDA port
   (65000) and adb (5555), so a POC can't drive the root-level LAMDA RPC from inside the emulator.
5. **Per-run cleanup** — POC uninstalled after each job.
6. **Author hygiene** — flag only in the app's internal private dir; `allowBackup=false`; no
   world-readable provider/prefs.

Verify on a running device:
```bash
podman exec android_env_device_1 sh -c 'adb shell getenforce'                 # Enforcing
podman exec android_env_device_1 sh -c 'adb shell "ls /system/xbin/su" '      # no such file
podman exec android_env_device_1 sh -c 'adb shell "iptables -S OUTPUT | grep owner"'
```

Residual risk: kernel/local-privesc in the emulator image is not fully eliminable — keep the
image updated and reset state between runs. Known CTF-acceptable risk.

---

## 8. Configuration (`.env` / `env.example`)

| Var | Default | Meaning |
|-----|---------|---------|
| `SECRET_KEY` | — | Flask session secret (set it) |
| `ENABLE_POW` | `true` | Require pwn.red PoW on upload (set `false` only for local testing) |
| `POW_DIFFICULTY` | `250000` | PoW difficulty (~a few seconds) |
| `MAX_FILE_SIZE` | `100` | Max upload size (MB) |
| `MAX_QUEUE_SIZE` | `50` | Max concurrent queued jobs (global) |
| `MAX_JOBS_PER_SESSION` | `1` | Max concurrent in-flight jobs per session cookie (anti-DoS) |
| `MAX_JOBS_PER_IP` | `2` | Max concurrent in-flight jobs per source IP (cookie-independent; uses `X-Forwarded-For` behind a proxy) |
| `JOB_RETENTION_SECONDS` | `3600` | Finished jobs + their screenshots are deleted after this |
| `CORS_ORIGINS` | `*` | Socket.IO allowed origins (comma-separated list in prod) |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` behind a TLS proxy (HTTPS-only ownership cookie) |
| `SESSION_COOKIE_SAMESITE` | `Lax` | Session cookie SameSite policy |
| `SCREENSHOT_DELAY` | `3` | Seconds to wait after the POC runs before the proof screenshot (per-challenge override: `SCREENSHOT_DELAY` in `client.py`) |
| `BACKEND_FORWARDS` | — | Fixed emulator-side port → challenge backend, `LISTEN:HOST:PORT` (see §4b / §9). Leave empty when using `EGRESS_ALLOW` instead |
| `EGRESS_LOCKDOWN` | `true` | Emulator apps may reach ONLY the backend forward + `EGRESS_ALLOW` + loopback; all else rejected. Metadata (`169.254.0.0/16`) always blocked |
| `EGRESS_ALLOW` | — | Extra IPs/CIDRs emulator apps may reach (e.g. an independently-spawned backend's host IP) |
| `EGRESS_ALLOW_PORTS` | — | Restrict `EGRESS_ALLOW` to a port/range, e.g. `1024:65535`. Empty = all ports |
| `EGRESS_DENY_PORTS` | — | Ports blocked even on allowlisted hosts, e.g. `22,3306` |
| `OPSSCHED_BACKEND` | `http://10.0.2.2:3014` | opsscheduler challenge: admin-fixed backend URL the app fetches |
| `ADB_HOST` | `device` | Hostname of the device container (LAMDA target) |
| `MAX_MEMORY` | `4096` | Emulator RAM (MB) |
| `SU_NAME` | random | Name su is renamed to (set by `run.sh`) |
| `SU_REMOVE` | `true` | Remove su entirely after setup |
| `WEB_PORT` | `5000` | Host port for the web UI |
| `DEBUG` | `false` | Flask debug (keep `false` in prod; `true` uses the Werkzeug dev server, else **waitress**) |

> **Security:** see **`docs/SECURITY.md`** for the threat model, the controls the platform enforces
> (network segmentation, PoW, owner-bound results, waitress in prod, challenge-UID-only backend
> access), and the few items that remain the operator's / challenge author's responsibility.

### Proof of Work (anti-spam)
The emulator runs **one job at a time**, so an uncosted upload endpoint lets one person starve
everyone. PoW makes each upload cost the player some CPU time first.

- **`POW_DIFFICULTY`** is the number of **sequential** rounds (`d`) the player must compute (a VDF —
  can't be parallelised). **Solving is ~1277× heavier than the server's verification**, which is the
  point: cheap to check, costly to flood.
- Rough solve time: `10000` ≈ sub-second (too weak), **`250000` ≈ a few seconds** (good default),
  higher = proportionally longer. Benchmark on a typical player machine and pick a tolerable wait.
- **Self-hosted solver** — the scheme is pwn.red/kctf-compatible, but the solver is served by **this
  app** at `GET /solve` (pure-Python, no external domain, no `curl|sh` from a third party). The page
  shows: `curl -s http://<site>/solve | python3 - <challenge>`.
- Pair PoW with the rate caps (`MAX_JOBS_PER_IP`, `MAX_JOBS_PER_SESSION`): PoW slows each flood
  attempt, the caps bound concurrency.

---

## 9. Deploying for a CTF (instance-per-team)

The queue is single-worker on one shared emulator, so for isolation run **one stack per team**:

- **Compose per team**: copy the stack with a unique project name and `WEB_PORT`:
  ```bash
  WEB_PORT=5101 podman-compose -p team01 up -d
  WEB_PORT=5102 podman-compose -p team02 up -d
  ```
  Each gets its own device+web pair and network. Mind host RAM/CPU: each emulator wants
  `MAX_MEMORY` RAM + a CPU core or two.
- **Kubernetes**: one Pod (two containers: device needs `/dev/kvm` + privileged; web) per team,
  fronted by a per-team Service/Ingress. Add a NetworkPolicy so only the ingress reaches `:5000`
  and nothing external reaches `:65000`.
- Enable PoW in production to throttle abuse of the shared emulator.

Capacity planning: emulators are heavy. Budget ~1 emulator per team, ~4 GB RAM + KVM each.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `podman compose` → "docker API socket" error | Use `podman-compose` (native), not `podman compose`. |
| Build: `no space left on device` | Move podman storage to a bigger disk: `scripts/move-podman-storage.sh` (revert with `revert-podman-storage.sh`). |
| Emulator segfault, `libX11-xcb.so.1` | Device image installs the X11/GL libs; it uses `-gpu swiftshader_indirect`. Rebuild device image. |
| Device loops "restarting emulator", lamda not found | LAMDA tarball path; `start.sh` pushes `/lamda-server-x86_64.tar.gz`. |
| Boot aborts: `... is DEBUGGABLE` | The challenge APK is a debug build. Ship a release (`android:debuggable=false`). |
| `adb install` fails `NO_CERTIFICATES` | Unsigned APK — auto-signed at boot, or pre-sign with `scripts/sign-apk.sh`. |
| Page shows "Device is not ready" right after boot | LAMDA script API warm-up; clears within ~10 s. |
| Screenshot shows home screen, not POC/flag | Screenshot fired before the POC/target UI drew. Add a settle delay in the challenge `callback` (e.g. `poc_app.start(); time.sleep(3)`) or wait for the target activity. |

Logs:
```bash
podman-compose logs -f device
podman-compose logs -f web
podman exec android_env_device_1 sh -c 'adb logcat -d | tail -100'
```

---

## 11. Layout

```
android_env/
  device/
    Dockerfile            # Android 14/API34 AVD + LAMDA + graphics libs
    start.sh              # boot, remount, install APKs, LAMDA, hardening (fail-closed)
    challenges_apk/       # baked real-flag APKs: <name>.apk
  web/
    Dockerfile
    requirements.txt
    src/
      app.py              # Flask+SocketIO, upload, queue, job execution
      device_manager.py   # LAMDA Device wrapper + readiness poll
      type.py             # Status enum, Client, Queue dataclasses
      pow.py              # pwn.red-compatible PoW
      utils.py            # aapt/process helpers
      config.py           # env-driven config
      templates/index.html
      challenges/<name>/client.py   # per-challenge plugin
  scripts/
    sign-apk.sh                  # sign unsigned APKs with a throwaway key
    move-podman-storage.sh       # relocate rootless podman storage
    revert-podman-storage.sh
  docker-compose.yml
  run.sh
  env.example
  PLAN.md                 # design + threat model
```
