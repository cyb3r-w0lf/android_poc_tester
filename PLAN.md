# android_env — Dockerized Android CTF POC Runner — Plan

Clone-with-changes of Mobile-POC-Tester. All new code lives in `android_env/`. Original repo = reference only.

## Locked decisions
- **Flag strategy:** TWO builds (author's job). Author builds a real-flag `challenge.apk` (goes into server image) and a `fake-flag.apk` handout shared with players outside this system. This repo only installs/runs the real-flag APK; it does NOT inject flags.
- **Install timing:** challenge APKs baked into the Docker image at build; installed into the emulator on boot (cannot `adb install` during `docker build` — emulator not running then).
- **Device control:** LAMDA (firerpa), same as reference.

## Threat model (why this works)
- Players reverse `fake-flag.apk` freely → build POC locally against the challenge logic (intent redirection, IDOR, deserialization, exported component, etc.).
- Real flag exists ONLY in the server's `challenge.apk`, reachable only by actually exploiting the running app (exfil to POC UI / shared storage / screenshot).
- POC runs in a shared, rooted, network-restricted emulator. POC uninstalled after each run. PoW gates abuse.
- Author contract: fake and real APK must be byte-identical except the flag constant, so reversing the handout faithfully models the server app.

## Target structure
```
android_env/
  README.md
  PLAN.md
  run.sh                     # gen SU_NAME .env, docker compose up --build
  docker-compose.yml
  env.example
  device/
    Dockerfile               # AVD build (Android 14 / API 34 google_apis x86_64) + lamda-server download
    start.sh                 # boot emulator, root, install baked challenge APKs, launch lamda, self-heal
    challenges_apk/          # baked real-flag APKs copied into image: <challenge>.apk
  web/
    Dockerfile
    requirements.txt         # flask, flask-socketio, pycryptodome, lamda
    src/
      app.py                 # flask+socketio, upload, single-worker queue, exec
      device_manager.py      # LAMDA Device wrapper + readiness poll
      type.py                # Status enum, Client, Queue dataclasses
      pow.py                 # pwn.red-compatible VDF PoW
      utils.py               # run_process/run_adb (aapt package parse)
      config.py              # env-driven Config
      templates/index.html   # Tailwind + jQuery + socket.io UI
      challenges/            # per-challenge plugin: <name>/client.py (+ challenge.apk reference)
      uploads/  screenshots/
```

## Challenge plugin contract (LAMDA-based, current API)
Each `web/src/challenges/<name>/client.py` exposes:
- `PACKAGE_NAME` (str, required) — vulnerable app package.
- `CHALLENGE_NAME` (set by loader = folder name).
- `TIMEOUT` (int, default 300).
- `callback(poc_app, update_status)` (optional) — drives POC: launch, interact, prep state. LAMDA app object + status setter.

Challenge APK naming: `device/challenges_apk/<name>.apk` must match the `challenges/<name>/` plugin folder so boot install + runtime package all line up.

## End-to-end flow (server)
1. `GET /` — list challenges, device-ready check, issue PoW into session, render UI.
2. Player solves PoW locally (`curl pwn.red/pow | sh -s <token>`).
3. `POST /upload` — verify device ready, verify PoW, validate challenge name, `.apk` only, enforce MAX_QUEUE_SIZE, save `uploads/<uuid>.apk`, enqueue, return `{id, next_pow}`; client joins SocketIO room `queue_<id>`.
4. QueueThread (single worker, per-job `future.result(timeout=TIMEOUT)`):
   - INITIALIZING: LAMDA `application(PACKAGE_NAME)`; challenge APK already installed at boot (skip if present).
   - INSTALLING_POC: `aapt dump badging` → POC package; REJECT if POC package == challenge package; install POC via LAMDA.
   - RUN: call `client.callback(poc_app, update_status)`.
   - TAKING_SCREENSHOT: `device.screenshot()` → `screenshots/<id>.png`; COMPLETED; uninstall POC.
5. SocketIO `status_update` live; `GET /screenshot/<id>` serves PNG.

## Device container (boot-time install — key difference from reference)
`device/start.sh` sequence:
1. Start emulator (**Android 14 = API 34**, `system-images;android-34;google_apis;x86_64`, pixel_4a), wait boot complete. NOTE: verify lamda-server 8.40 runs on API 34; if not, bump lamda. Use `google_apis` (rootable), NOT `google_apis_playstore` (not rootable).
2. `adb root`, disable verity/verification, remount.
3. Randomize + lock `su` (SU_NAME) so POCs can't trivially root.
4. **NEW:** loop `device/challenges_apk/*.apk` → `adb install` each (idempotent; skip if package already installed). This realizes "install challenge APKs" tied to the image build (APKs baked at build, installed first boot).
5. Download/push/launch lamda-server; `adb forward` + `socat` expose port 65000 to web.
6. Disable IME/TTS/quicksearch for UI stability.
7. Self-heal supervisor: watch qemu/adb/device/lamda, restart on failure; write `/app/device_ready` sentinel.

## Build vs runtime note
`docker build` copies APKs into image (`device/challenges_apk/`). Actual `adb install` happens on container boot (start.sh) once emulator is live. If faster cold-start needed later: optional AVD-snapshot build step (needs KVM at build). Not in v1.

## Security posture
- Privileged device container, tmpfs `/data`, NetworkPolicy (65000 → gateway only, 5000 → proxy) in prod k8s.
- POC uninstalled each run; `secure_filename`, MAX_CONTENT_LENGTH, extension allowlist.
- aapt same-package collision block (POC can't impersonate challenge).
- PoW (dev 10000 / prod 250000). Real flag never leaves server APK; handout is fake-flag build.

## ROOT PREVENTION & FLAG CONFIDENTIALITY (hard requirement)
Threat: if an uploaded POC gains root (or equivalent read of another app's `/data/data`), it reads the flag directly, bypassing the intended exploit. Emulator is rooted for SETUP only; the POC app is untrusted and must NEVER reach root. Defense in depth — each layer independently blocks the bypass:

### Layer 1 — SELinux ENFORCING (primary control)
- Keep SELinux **enforcing** (`setenforce 1`; never permissive). An `untrusted_app` domain cannot read another app's private dir even within the same UID class. This is what actually stops cross-app `/data/data` reads.
- Do NOT globally remount `/system` writable or relabel to break SELinux. Reference disables dm-verity for setup — fine — but SELinux stays enforcing before uploads are accepted.

### Layer 2 — challenge APK is a RELEASE (non-debuggable) build
- Author MUST ship `android:debuggable="false"` release-signed challenge APK. If debuggable, a POC (or any shell) can `run-as com.challenge` and read its data dir with no root at all. Boot script asserts this: parse `aapt dump badging` for `application-debuggable` → refuse to start / alarm if set.

### Layer 3 — neuter root for apps (su + lamda, the two root paths)
- **su:** rename to random `SU_NAME`, `chown root:shell`, `chmod 6750` so only shell/root group can exec. App UIDs (10000+) are not in `shell` group → cannot exec su. Better in v1: after boot setup completes and BEFORE accepting uploads, **remove/neuter the su binary entirely** (two-build model needs no per-run root, so runtime root is unnecessary).
- **LAMDA server = root process with a local port — THE critical hole.** The POC app runs inside the same emulator and can `connect()` to `127.0.0.1:65000`; if it speaks LAMDA it executes commands as root. Block app UIDs from the lamda/adb ports:
  - `iptables -A OUTPUT -m owner --uid-owner 10000:19999 -p tcp --dport 65000 -j REJECT` (and the adbd port). xt_owner is available in the emulator kernel; verify at boot, fail closed if the rule can't be set.
  - Prefer binding lamda to a non-app-reachable transport (unix socket / loopback-only reached solely via `socat` on the container veth to the web container). Never expose it on an interface or port an app can dial.

### Layer 4 — no per-run root needed (shrink the attack window)
- Two-build model: flag is baked into the installed challenge APK, not injected per run → the run path never needs root. So root can be fully dropped before the queue opens. Callbacks drive the POC via LAMDA from the WEB side (root-side), not from inside the app.

### Layer 5 — state reset between runs (defeats persistence / successful root)
- POC uninstalled after each run (reference does this).
- Stronger: boot the emulator from a clean AVD **snapshot** and restore/reboot to it between jobs (or on any integrity-check failure). Even if a POC achieves root, the next player starts from a clean device and residual implants die. v1: reboot-on-failure + periodic reset; v2: snapshot restore per job.

### Layer 6 — flag file hygiene (author contract)
- Flag lives only in the challenge app's internal private dir, `chmod 600`, owned by the challenge UID. Never on world-readable external storage, never in logs, never in a world-readable SharedPrefs/MODE_WORLD_READABLE.
- No world-readable/exported content provider or debuggable backup (`android:allowBackup="false"`) that leaks the flag without the intended exploit.

### Boot-time assertions (start.sh must fail closed)
1. `getenforce` == Enforcing.
2. Each baked challenge APK is non-debuggable (aapt check).
3. su neutered / removed; verify an app UID cannot exec it.
4. iptables owner-reject rules for lamda+adb ports present; abort if not set.
5. Only after all pass → write `/app/device_ready`. Web refuses uploads until ready.

### Residual risk
- Kernel/local-privesc in the emulator image is out of scope to fully eliminate; mitigate with an up-to-date image + Layer 5 snapshot reset so a one-off root does not persist or leak across players. Accept as known CTF risk.

## Build order (implementation phases)
1. Scaffold `android_env/` tree + config/type/pow/utils (port + modernize from reference).
2. `web/src/app.py` + `device_manager.py` + `index.html` (LAMDA flow).
3. `device/` Dockerfile + start.sh with boot-time challenge-APK install loop.
4. `docker-compose.yml` + `run.sh` + `env.example` + README.
5. Add one sample challenge plugin (`challenges/demo/client.py`) + placeholder APK slot; document author two-build workflow.
6. Smoke test: build images, boot, confirm device_ready, upload a dummy POC, get screenshot.

## Open items for author
- Supply real-flag `challenge.apk` per challenge → drop in `device/challenges_apk/<name>.apk`.
- Write matching `web/src/challenges/<name>/client.py` with `PACKAGE_NAME` + `callback`.
- Keep fake/real builds identical except flag constant.
