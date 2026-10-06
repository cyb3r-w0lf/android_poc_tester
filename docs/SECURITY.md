# android_env — Security Model & Hardening

This document describes the threat model, the controls the platform enforces, and the two things
that remain the **operator's / challenge author's** responsibility.

## Threat model

The attacker is a **player** who controls:
- the uploaded POC `.apk` (arbitrary bytes, arbitrary manifest/package name, arbitrary code that
  runs inside the shared emulator as a normal app),
- the upload form fields (challenge name, per-challenge inputs, PoW solution, filename),
- whatever their app does at runtime.

The attacker must **not** be able to: read a flag without performing the intended exploit; get root
on the emulator; run commands on the web container or host; read/write arbitrary files; pivot to
host/backend services; break other players' jobs; or DoS the platform.

## Controls enforced by the platform

### Device / emulator (`device/start.sh`, boot-time, fail-closed)
- **SELinux Enforcing** — blocks cross-app `/data/data` reads.
- **Non-debuggable challenge APKs** — a debuggable build is refused at boot (`run-as` would read
  the flag with no exploit).
- **`su` removed** (`SU_REMOVE=true`) and, regardless, not executable by app UIDs.
- **Network segmentation (the critical control).** In the emulator's user-mode network, `10.0.2.2`
  is the **device container's** loopback, which exposes root services (adb server `5037`, the LAMDA
  forward `65010`) and the guest exposes the LAMDA RPC (`65000`) and adbd (`5555`). The app UID
  range `10000–19999` is:
  - `REJECT`ed to guest-local `65000` and `5555`,
  - `REJECT`ed to **all of `10.0.2.2`** by default (closes `5037`/`65010`/anything else),
  - `ACCEPT`ed to a backend forward port **only for the challenge app's own UID** — so an uploaded
    POC cannot reach the backend directly and bypass the intended exploit.
- **Backend forwards** (`BACKEND_FORWARDS`) expose only the explicitly listed ports, only to the
  challenge app's UID (see above).
- **POC uninstalled** after each run; userdata wiped on emulator restarts.

### Web app (`web/src/app.py`)
- **PoW gate** (kctf/pwn.red-compatible VDF, **self-hosted** solver at `GET /solve` — no external domain) **on by default** (`ENABLE_POW=true`, `POW_DIFFICULTY=250000`). `POW_DIFFICULTY` = sequential solve rounds; solving is ~1277x the verify cost.
- **Per-session job cap** (`MAX_JOBS_PER_SESSION=1`) + global `MAX_QUEUE_SIZE`.
- **Owner-bound results** — `/logs/<id>` and `/screenshot/<id>` require the uploader's session.
- **Input validation** — `validate_inputs` caps length/type; uploaded package name must match a
  strict regex and is `shlex.quote`d before any device shell use.
- **No path traversal** — screenshots use `secure_filename` + server UUIDs; logs are in-memory.
- **aapt** runs with a list argv (no shell).
- **Artifact cleanup** — uploaded APK deleted after install; finished jobs + screenshots reaped
  after `JOB_RETENTION_SECONDS`.
- **Production WSGI** — served by **waitress** when `DEBUG=false` (no Werkzeug dev server).
- **CORS** — Socket.IO origins configurable via `CORS_ORIGINS` (set to your site in prod).

## Container isolation & host-escape posture

A participant's code runs as an app **inside the emulator VM**. Reaching the host requires chaining:
app → guest-root (Android/kernel privesc) → **QEMU/KVM VM escape** (the hard boundary) → the device
container → the host. Controls:

- **Unprivileged, all caps dropped.** Both services run with **no `--privileged`, `cap_drop: [ALL]`,
  and `no-new-privileges`** — the device service adds only `devices: [/dev/kvm]`. The emulator opens
  the world-rw `/dev/kvm`, issues KVM ioctls, and uses user-mode networking, none of which need a
  capability; guest-side root ops (iptables/su/remount) run *inside the VM*. Dropping privileged also
  keeps the default seccomp/apparmor profiles active.
- **Rootless podman.** Container "root" maps to an unprivileged host user, so a container escape does
  not yield host root by itself.
- **No Docker/privileged socket is mounted** into any container, so container code cannot reach it.

### Neutralizing the host-escape amplifier (no dedicated user required)
If a full escape ever reaches the **host user**, and that user can reach a Docker socket
(`/var/run/docker.sock`, `root:docker`) or has sudo, that is a direct path to host root. When running
as a dedicated locked-down user is **not** an option, isolate the whole stack at the boundary so an
escape lands somewhere useless:
- **Run the stack in its own VM** (or a sandboxed container runtime such as **gVisor `runsc`** /
  **Kata Containers**). An escape then reaches only that throwaway sandbox, which holds no Docker
  socket and no other credentials.
- **On Kubernetes**, give the pod a hardened `securityContext` and schedule it with isolation:
  ```yaml
  securityContext:
    runAsNonRoot: true
    allowPrivilegeEscalation: false
    capabilities: { drop: ["ALL"] }
    seccompProfile: { type: RuntimeDefault }
  # no hostPath mounts (never mount the container runtime socket)
  # /dev/kvm via a KVM device plugin; use gVisor/Kata (runtimeClassName) or a dedicated node pool
  ```
- Ensure the account/service that runs the stack **cannot reach a container-runtime socket** and has
  no passwordless sudo — even if it is a shared infra identity, remove those specific powers for the
  workload (e.g. run under a systemd unit with `NoNewPrivileges`, `PrivateDevices` off only for
  `/dev/kvm`, and no docker-group supplementary gid).
- **Keep the emulator system image updated** (shrinks the guest-privesc + QEMU-escape surface).
- **Per-run reset** (userdata wiped on restart) limits persistence.

## Operator / author responsibilities (NOT enforceable by the platform)

1. **Challenge backend must authenticate the caller.** If a challenge app talks to a backend
   (e.g. `/admin/nonce`), the platform restricts network access to the challenge app's UID, but a
   determined attacker who achieves code execution *as the challenge app* (which is the goal) can
   still call it. The backend must bind the flag to the *intended flow* (e.g. a value only the real
   AdminActivity path can produce), not merely to network reachability. Treat the forwarded port as
   reachable by the challenge context.
2. **Keep `DEBUG=false` in production** and set a strong `SECRET_KEY`.
3. **Scope `BACKEND_FORWARDS` minimally**; prefer a dedicated challenge-backend container on an
   isolated network over `host.containers.internal`.
4. **Flag hygiene in the challenge APK** — flag only in the app's internal private storage,
   `allowBackup=false`, no world-readable provider/prefs, not printed to logcat (the per-job log
   view filters to the challenge/POC packages and would surface a logged flag).
5. **Keep the emulator image updated** — kernel/local-privesc in the Android image is out of scope
   to fully eliminate; state reset between runs contains a one-off compromise.

## Production deployment — instance-per-team with backends

Running one stack per team where each challenge backend binds a random host port is fully supported.
The key idea: **the emulator-side port is fixed; the random host port is just the other end of the
forward, templated at deploy time.**

1. **Keep the backend internal — never public.** If the backend is reachable on a public IP, players
   `curl` the flag directly and skip the challenge. Use a k8s `ClusterIP` / a sidecar in the same pod
   / a localhost bind — not `NodePort`/`LoadBalancer`.
2. **Fixed emulator-side port, random host port mapped in.** The app always talks to
   `10.0.2.2:<FIXED>` (e.g. `3014`). Your orchestrator knows the random port it assigned, so it
   templates that into the instance's env:
   ```
   BACKEND_FORWARDS=3014:<backend-host-or-service>:<RANDOM_PORT>
   OPSSCHED_BACKEND=http://10.0.2.2:3014      # emulator-side, stays fixed
   ```
   No guessing — the emulator side never changes; only the forward target does.
   **Independent backend / random port (no wiring):** if the backend is spawned separately on a
   random port android_env can't learn, drop `BACKEND_FORWARDS` and instead allowlist the server IP;
   the player enters `host:port` (see SETUP §5d):
   ```
   # BACKEND_FORWARDS=              # (left empty)
   EGRESS_ALLOW=<server-IP>        # emulator apps may reach only this IP
   EGRESS_ALLOW_PORTS=1024:65535   # any random port on it
   EGRESS_DENY_PORTS=22,3306       # blocked even on allowed IPs
   ```
   Trade-off: with a player-entered host:port the backend becomes reachable by the POC too, so the
   backend MUST authenticate the exploit flow (and/or each team gets a distinct IP). The fixed-forward
   model above avoids this because only the challenge UID can reach the forward.
3. **SSRF is closed at the network layer.** With `EGRESS_LOCKDOWN=true` (default), an app UID in the
   emulator can reach ONLY the backend forward (its own challenge UID), any `EGRESS_ALLOW` IPs
   (optionally port-restricted, minus `EGRESS_DENY_PORTS`), and loopback; cloud metadata
   (`169.254.0.0/16`, always), the internet, and other cluster pods are rejected. `EGRESS_ALLOW`
   entries sit BELOW the gateway/root-port rejects, so they can never re-open the root control plane.
   Operator egress values are validated (IP/CIDR + numeric port) before they reach a shell.
4. **Per-team isolation.** Run each team as its own pod/namespace (k8s) or compose project
   (`podman-compose -p teamNN`), each with its own emulator, web, and backend, on its own network.
5. **Expose only the web port** (5000) per team. Never expose `5037` (adb server), `5554/5555`
   (emulator console/adbd), or `65000` (LAMDA) via any Service/NodePort/LoadBalancer.
6. **Unprivileged everywhere** — `cap_drop: [ALL]`, `no-new-privileges`, rootless, `/dev/kvm` via a
   device plugin; sandbox the pod (gVisor/Kata or a dedicated node) so a worst-case escape is
   contained and reaches no container-runtime socket.

### Production checklist
- [ ] `ENABLE_POW=true`, `POW_DIFFICULTY` non-trivial, strong `SECRET_KEY`, `DEBUG=false` (waitress serves it)
- [ ] Anti-DoS: `MAX_JOBS_PER_SESSION=1`, `MAX_JOBS_PER_IP` set, and a proxy-level rate limit; `JOB_RETENTION_SECONDS` reaps old jobs
- [ ] Backend internal only (no public Service). Wired model: `BACKEND_FORWARDS` templated per instance. Independent/random-port model: `EGRESS_ALLOW=<server-IP>` + `EGRESS_ALLOW_PORTS` and the backend authenticates the exploit flow
- [ ] `EGRESS_LOCKDOWN=true`; metadata always blocked; `EGRESS_DENY_PORTS` for ssh/db; dispatch URL admin-fixed when wired
- [ ] Only web :5000 exposed; device ports (5037/5554/5555/65000) never published
- [ ] `cap_drop: [ALL]` + `no-new-privileges` + rootless; pod sandboxed; runner identity cannot reach a runtime socket / sudo
- [ ] `CORS_ORIGINS` set to the site origin; `SESSION_COOKIE_SECURE=true` behind TLS
- [ ] Real-flag APKs NOT in git (`device/challenges_apk/*.apk` gitignored); challenge APKs non-debuggable release builds
- [ ] Emulator system image current; per-run reset on

## Residual risk

- Native kernel/local privilege escalation in the emulator image (mitigated by updates + per-run
  reset).
- Socket.IO uses HTTP long-polling under waitress (no native WebSocket); functionally fine for
  status updates.
