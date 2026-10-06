# android_env — Complete Setup & Admin Guide

This guide assumes **zero prior knowledge**. Follow it top to bottom and you will have a working
Android CTF POC runner, with your own challenges, that players can use from a web page.

If a command block shows multiple lines, run them one at a time unless told otherwise. Lines
starting with `#` are comments — you don't type them.

---

## 0. What this thing is (plain English)

- You host an **Android app with a vulnerability** (the "challenge"). Players must exploit it.
- Each player writes a small Android app (a "POC" = proof of concept) that performs the exploit.
- They upload that POC to **your web page**. Your server installs it next to the real challenge
  app inside a hidden Android phone (an **emulator**), runs it, and takes a **screenshot**.
- If their exploit worked, the screenshot shows the proof (often the flag).
- The real flag only exists on **your** server. The copy you hand out to players has a **fake**
  flag, so reversing the handout never reveals the real one.

Two containers run the show:
- **device** = the Android emulator + remote-control server.
- **web** = the website (upload form, queue, screenshots).

---

## 1. Prepare the machine (one time)

You need a **Linux** machine with:

### 1a. Hardware virtualization (KVM)
The emulator needs KVM to run at usable speed. Check:
```bash
ls -l /dev/kvm
grep -cE 'vmx|svm' /proc/cpuinfo    # should print a number > 0
```
- If `/dev/kvm` is missing: enable **virtualization (VT-x / AMD-V)** in your BIOS/UEFI, and on a
  cloud VM make sure it's a "nested virtualization" / metal instance.
- `/dev/kvm` should be readable/writable. If not: `sudo chmod 666 /dev/kvm` (or add yourself to the
  `kvm` group and re-login).

### 1b. podman + podman-compose
```bash
# Arch
sudo pacman -S podman podman-compose
# Debian/Ubuntu
sudo apt install -y podman podman-compose
```
Verify:
```bash
podman --version
podman-compose --version
```
> Always use **`podman-compose`** (two words with a dash) in this guide. Do **not** use
> `podman compose` (space) — it needs an extra background service and will fail here.

### 1c. Disk space
The Android SDK + emulator image are large. You need about **15 GB free** where podman stores
images (`~/.local/share/containers`). Check:
```bash
df -h ~/.local/share/containers
```
If that disk is nearly full but another disk has room, see **Appendix A** to relocate podman
storage.

---

## 2. Get the code and do the first build

```bash
cd /path/to/Mobile-POC-Tester/android_env      # the folder that has docker-compose.yml
cp env.example .env                              # create your settings file
```

Open `.env` in any editor and set at least:
```
SECRET_KEY=change-me-to-a-long-random-string
```
Proof-of-Work is **on by default** (anti-abuse). For local testing, turn it off so you can upload
without solving a PoW; re-enable it for the real deployment:
```
ENABLE_POW=false     # local testing only — leave it true (default) in production
```
(Leave everything else at defaults for now.)

Build and start everything:
```bash
./run.sh
```
What `run.sh` does: puts a random `SU_NAME` into `.env`, then runs `podman-compose up --build`.

**The first build is slow** (it downloads the Android SDK + the Android 14 system image — can be
10–30 min depending on your internet). This is normal and happens only once.

After the build, the emulator **cold-boots** and runs security checks for a few more minutes.
Leave it running. To watch progress in another terminal:
```bash
cd /path/to/Mobile-POC-Tester/android_env
podman-compose logs -f device
```
You're waiting for the line: **`[i] Device is ready!`**

---

## 3. Verify it's up

```bash
# the emulator is ready when this prints 1:
podman exec android_env_device_1 cat /app/device_ready

# the website reports the device ready:
curl -s http://localhost:5000/device_status
# -> {"device_ready":true,"status":"success"}
```
Now open **http://localhost:5000** in a browser. You should see the "Android CTF POC Runner" page.

> If a page load right after boot says "Device is not ready", wait ~10 seconds and refresh — the
> remote-control service takes a moment to warm up after the emulator boots.

Container names are `android_env_device_1` and `android_env_web_1` (podman-compose adds the `_1`).

---

## 4. Add YOUR challenge (full walkthrough)

A challenge is **two files**: the challenge APK (goes in the image) and a small Python "plugin".

### 4a. Build two versions of your challenge APK
As the challenge author, build your app **twice**, identical except the flag string:
1. **Handout APK** — contains a **FAKE** flag. You give this to players (outside this system).
2. **Server APK** — contains the **REAL** flag. This goes into the server.

The server APK **must be a release build**, meaning in its `AndroidManifest.xml`:
```xml
<application android:debuggable="false" android:allowBackup="false" ...>
```
> Why: if the app is `debuggable`, anyone can run `run-as <package>` and read the app's private
> files — including the flag — **without exploiting anything**. The server refuses to boot with a
> debuggable challenge APK on purpose.

Signing: if your APK is unsigned, that's fine — the server auto-signs it on boot with a throwaway
key. (You can also pre-sign it; see **Appendix B**.)

### 4b. Drop the server APK into the image folder
Name it after your challenge (lowercase, no spaces), e.g. `myctf`:
```bash
cp /path/to/your-release.apk \
   /path/to/Mobile-POC-Tester/android_env/device/challenges_apk/myctf.apk
```
> **Never commit real-flag APKs.** `device/challenges_apk/*.apk` is gitignored for this reason — the
> server APK contains the real flag. Keep it out of git; distribute only the fake-flag handout.

### 4c. Find the app's package name
```bash
cd /path/to/Mobile-POC-Tester/android_env
podman run --rm --entrypoint sh -v "$PWD/device/challenges_apk:/apk:ro" android_env_device \
  -c 'aapt dump badging /apk/myctf.apk | grep -E "^package:|application-debuggable"'
```
- Note the `package: name='...'` value — that's your `PACKAGE_NAME`.
- If you see `application-debuggable`, STOP — rebuild a release APK (see 4a).

### 4d. Create the plugin file

**Every challenge is different.** The only required line is `PACKAGE_NAME`. `INPUTS`, `callback`,
`SCREENSHOT_DELAY`, and the backend step (§5) are **all optional** — add only what your app needs.

**Simplest challenge — no player input, just launch the POC:**
```python
# web/src/challenges/myctf/client.py
PACKAGE_NAME = "com.yourcompany.myctf"

def callback(poc_app, update_status):
    poc_app.start()          # launch the player's POC; it does the exploit
```
That's a complete, working challenge. No `INPUTS` → the upload page shows no extra fields. No
backend → nothing else to configure.

> If you omit `callback` entirely, the POC gets installed but never launched (the screenshot would
> just show the home screen). Most challenges want at least the two-line `callback` above.

**Fuller template** (folder name = `myctf`, same as the APK name) — copy and delete what you don't need:
```python
PACKAGE_NAME = "com.yourcompany.myctf"   # REQUIRED: from step 4c
TIMEOUT = 300                            # optional: max seconds per run
SCREENSHOT_DELAY = 5                     # optional: wait before the proof screenshot

# OPTIONAL: inputs the PLAYER fills in on the upload page (host:port, keys, etc.)
# Types: text, number, password, textarea, select (with "options").
INPUTS = [
    {"name": "host", "label": "Server host", "type": "text", "required": False,
     "default": "10.0.2.2", "placeholder": "10.0.2.2"},
    {"name": "port", "label": "Server port", "type": "number", "required": False, "default": "8080"},
]

# OPTIONAL: runs after the POC is installed. Use it to launch the POC / set up state.
def callback(poc_app, update_status, inputs=None):
    inputs = inputs or {}
    # poc_app.start() launches the player's POC. The POC does the exploit.
    # inputs["host"], inputs["port"] = what the player typed (if you declared INPUTS).
    poc_app.start()
```

### 4e. Rebuild so the new files are baked in
```bash
cd /path/to/Mobile-POC-Tester/android_env
podman-compose up -d --build
```
Wait for `[i] Device is ready!` again. Your challenge now appears in the dropdown at
http://localhost:5000.

---

## 5. If your challenge app talks to a backend server

Some apps call out to a server (e.g. to fetch a token, then the flag). Two things are needed:

### 5a. Make the emulator able to reach your backend
Inside the emulator, the special address **`10.0.2.2`** means "the machine hosting the emulator".
Set `BACKEND_FORWARDS` in `.env` so the container forwards that to your real backend.

Example: your backend runs on the **host** at port **3014**:
```
# in .env
BACKEND_FORWARDS=3014
```
That makes `10.0.2.2:3014` (as seen by the app) reach your host's `localhost:3014`.

More forms (comma-separated):
```
BACKEND_FORWARDS=3014                               # 10.0.2.2:3014 -> host:3014
BACKEND_FORWARDS=3014:host.containers.internal:3014 # same, explicit
BACKEND_FORWARDS=9000:10.89.0.9:9000                # forward to a specific IP
```
Apply it:
```bash
podman-compose up -d                 # recreates the device with the new setting
```
Verify the app can reach it (replace the path with one your backend serves):
```bash
podman exec android_env_device_1 sh -c \
 'adb shell "echo -e \"GET / HTTP/1.0\r\n\r\n\" | toybox nc 10.0.2.2 3014 | head -c 200"'
```

### 5b. Tell the app WHICH server to use
This depends on how your app is written. Common patterns:
- The app reads the **server from the player** → declare it in `INPUTS` (step 4d) and have your
  `callback` apply it (write a pref, pass an intent extra, etc.).
- The **POC sets the server** itself (via an exported component / intent) → the player's POC does
  it; you just provide the `BACKEND_FORWARDS` path and tell players the address (`10.0.2.2:<port>`).

**Worked example — the bundled `opsscheduler`:** its `AdminActivity` fetches `<url>/admin/nonce`
where `<url>` is read from the app's private `SharedPreferences("pp")["url"]`. Only the app's own
UI writes that pref, so the harness `callback` writes it as root (via `device`) from the player's
`INPUTS["url"]` (sanitized), then launches the POC, which performs the exported-CallbackActivity /
mutable-PendingIntent exploit to reach `AdminActivity`. See `web/src/challenges/opsscheduler/client.py`
for the exact code.

### 5c. More than one challenge with a backend
Give each backend its own port and list them all in `BACKEND_FORWARDS`.

**Backend published on a host port:**
```bash
podman run -d --name chalB-backend -p 3015:3015 your/chalB-backend
```
```
# .env
BACKEND_FORWARDS=3014,3015
```
**Backend on the stack's podman network (nothing exposed on the host — preferred for prod):**
```bash
podman run -d --name chalB-backend --network android_env_app-network your/chalB-backend
```
```
# .env — LISTEN:HOST:PORT form targets the container by name
BACKEND_FORWARDS=3014,3015:chalB-backend:3015
```
The emulator reaches each as `10.0.2.2:<port>`; point each challenge's `client.py` (or its input
default) at its own `http://10.0.2.2:<port>`. Apply with `podman-compose up -d`.

Isolation note: the platform allows a forwarded port only to the **challenge app's own UID** (an
uploaded POC cannot reach the backend directly). A challenge backend should still authenticate the
caller — see `docs/SECURITY.md`.

### 5d. Backend on a RANDOM port / spawned independently (no wiring)
Use this when the backend is a separate container started like
`podman run -d -e FLAG=... -p <RANDOM>:<port> your/backend` and android_env has no way to learn
that random port. Here you do NOT use `BACKEND_FORWARDS`; instead the emulator reaches the backend
directly, and you allowlist the server it lives on.

1. Comment out the forward (nothing to map):
   ```
   # BACKEND_FORWARDS=
   ```
2. Allowlist the server IP (and the random-port range) that emulator apps may reach — everything
   else (metadata, other hosts, internet) stays blocked:
   ```
   EGRESS_ALLOW=<server-IP>        # the one IP your backends run on
   EGRESS_ALLOW_PORTS=1024:65535   # any random port on it
   EGRESS_DENY_PORTS=22,3306       # optional: block ssh/db even on that IP
   ```
3. Let the **player enter** `http://<server-IP>:<RANDOM>` — re-enable the challenge's URL `INPUT`
   (the arena shows the player their backend's address).

**Security requirement for this model:** because the player types the host:port, you must stop a POC
from hitting the backend directly and skipping the exploit. Two measures (do both):
- The backend **must authenticate the exploit flow** (e.g. the token is only valid when produced by
  the real in-app path), so merely reaching it yields nothing.
- Keep each team's backend on a **distinct IP** where possible; if all share one IP at different
  random ports, a team could reach another team's backend by guessing its port — the backend auth
  above is what prevents a flag leak in that case.

### Choosing between the two models
| | §5a–5c Fixed forward | §5d Independent / random port |
|---|---|---|
| android_env knows the backend port? | yes (you template `BACKEND_FORWARDS`) | no |
| Player enters host:port? | no (app uses fixed `10.0.2.2:<port>`) | yes |
| SSRF surface | none (only the forward is reachable) | limited to `EGRESS_ALLOW` IP + port range |
| Direct-curl bypass protection | per-UID forward rule | backend auth (required) |
| Best for | orchestrator-wired deploys | fully independent spawners |

---

## 6. How players use it

1. Open the site, pick the challenge from the dropdown.
2. Fill any inputs you defined.
3. Upload their POC `.apk`.
4. (If PoW is on) solve the proof-of-work with the shown command.
5. Watch the live status. When **COMPLETED**, click **View Screenshot** (the proof) and **View
   logs** (the device logcat for the challenge + POC — handy for debugging).

Rules the server enforces automatically: `.apk` only; size/queue limits; the POC's package name may
not equal the challenge's; the POC is uninstalled after each run.

---

## 7. Everyday operations

```bash
cd /path/to/Mobile-POC-Tester/android_env

podman-compose ps                 # what's running
podman-compose logs -f device     # device logs (boot, hardening)
podman-compose logs -f web        # website logs
podman-compose restart web        # restart just the website
podman-compose down               # stop everything
podman-compose up -d              # start again (no rebuild)
podman-compose up -d --build      # rebuild (after changing APKs/plugins/Dockerfiles)
```

Settings live in `.env` (see the table in `README.md` §8). After editing `.env`, run
`podman-compose up -d` to apply.

---

## 8. Running one instance per team (CTF deployment)

The emulator is shared and runs one job at a time, so for fairness/isolation run **one full stack
per team**, each on its own port:
```bash
WEB_PORT=5101 podman-compose -p team01 up -d --build
WEB_PORT=5102 podman-compose -p team02 up -d --build
# team01 -> http://localhost:5101 , team02 -> http://localhost:5102
```
Each `-p <name>` is a separate, isolated stack (its own emulator + website + network). Put each
behind your reverse proxy / give each team their URL.

**Capacity:** every emulator wants ~4 GB RAM + KVM + a CPU core or two. Plan hardware accordingly
(e.g. 10 teams ≈ 40 GB RAM just for emulators).

For production also set in each `.env`:
```
ENABLE_POW=true
POW_DIFFICULTY=250000
```
to throttle abuse of the shared emulator.

---

## 9. Troubleshooting (symptom → fix)

| You see | Do this |
|---|---|
| `podman compose` errors about a socket | Use `podman-compose` (dash), not `podman compose`. |
| Build fails: `no space left on device` | Free space or relocate storage — **Appendix A**. |
| Boot aborts: `... is DEBUGGABLE` | Your challenge APK is a debug build. Rebuild release (4a). |
| Challenge not in the dropdown | APK in `device/challenges_apk/`? Plugin in `web/src/challenges/<name>/client.py`? Did you `up -d --build`? |
| Page: "Device is not ready" after boot | Wait ~10 s, refresh (remote-control warm-up). |
| Screenshot shows home screen, not the app | Increase `SCREENSHOT_DELAY` (global in `.env` or per challenge in `client.py`). |
| App shows a network/URL error | Backend not reachable or not configured — **step 5**. |
| Emulator keeps "restarting emulator" | `podman-compose logs -f device` and read the first FATAL line; check KVM (step 1a). |

More logs:
```bash
podman exec android_env_device_1 sh -c 'adb logcat -d | tail -200'
```

---

## Appendix A — Move podman storage to a bigger disk

If the disk holding `~/.local/share/containers` is too small, move it to a disk with room.
Helper scripts are in `scripts/` (adjust paths to where you keep them):
```bash
# one-time: make a folder on the big disk that you own
sudo mkdir -p /var/podman/$USER
sudo chown -R "$USER":"$USER" /var/podman/$USER

# migrate (copies existing images, keeps a backup of your config)
scripts/move-podman-storage.sh

# verify, then reclaim the old space:
podman info --format '{{.Store.GraphRoot}}'    # should be the new path
scripts/move-podman-storage.sh --cleanup-old

# to undo:
scripts/revert-podman-storage.sh
```

## Appendix B — Pre-sign an APK yourself

Unsigned APKs are auto-signed at boot. To sign manually:
```bash
scripts/sign-apk.sh device/challenges_apk/myctf.apk       # one file
scripts/sign-apk.sh --dir device/challenges_apk           # a whole folder
```
(Run inside the device image or on a host with the Android SDK; it uses a throwaway `ctf` key —
a CTF only needs a valid signature, not a trusted one.)

## Appendix C — Glossary

- **APK** — an Android app file.
- **Emulator / AVD** — a virtual Android phone running on your machine.
- **ADB** — the tool that talks to an Android device/emulator.
- **LAMDA** — the remote-control server inside the emulator the website drives.
- **POC** — the player's exploit app.
- **10.0.2.2** — inside the emulator, the address of the machine hosting it.
- **PoW (proof of work)** — a small puzzle players solve to prevent spamming the queue.
