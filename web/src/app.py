import os
import uuid
import re
import time
import glob
import hmac
import io
import shlex
import tomllib
from types import SimpleNamespace
from threading import Thread, Lock
from flask import Flask, render_template, request, jsonify, send_file, session, Response
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from importlib import import_module

from type import Status, Queue
from utils import run_process
from config import Config
from pow import Challenge, check

from lamda.client import *
from lamda.const import *
from device_manager import DeviceManager

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = Config.MAX_FILE_SIZE
app.config['SESSION_COOKIE_HTTPONLY'] = Config.SESSION_COOKIE_HTTPONLY
app.config['SESSION_COOKIE_SAMESITE'] = Config.SESSION_COOKIE_SAMESITE
app.config['SESSION_COOKIE_SECURE'] = Config.SESSION_COOKIE_SECURE

socketio = SocketIO(app, cors_allowed_origins=Config.CORS_ORIGINS, async_mode='threading', logger=False, engineio_logger=False)

# Initialize device manager
device_manager = DeviceManager()

queue = []
queue_lock = Lock()
clients = []

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS

# ---- Per-challenge user inputs (admin-defined schema in each challenge's client.py) ----
ALLOWED_INPUT_TYPES = {'text', 'number', 'password', 'textarea', 'select'}
MAX_INPUT_LEN = 1024

def challenge_input_schema(client):
    """Return a sanitized input schema list for a challenge module (client.INPUTS)."""
    raw = getattr(client, 'INPUTS', None) or []
    schema = []
    for f in raw:
        try:
            name = str(f['name']).strip()
        except Exception:
            continue
        if not name:
            continue
        ftype = str(f.get('type', 'text'))
        if ftype not in ALLOWED_INPUT_TYPES:
            ftype = 'text'
        schema.append({
            'name': name,
            'label': str(f.get('label', name)),
            'type': ftype,
            'required': bool(f.get('required', False)),
            'default': str(f.get('default', '')),
            'placeholder': str(f.get('placeholder', '')),
            'options': [str(o) for o in f.get('options', [])] if ftype == 'select' else [],
        })
    return schema

def validate_inputs(client, provided):
    """Validate user-provided values against the schema. Returns (clean_dict, error_or_None)."""
    provided = provided or {}
    clean = {}
    for field in challenge_input_schema(client):
        name = field['name']
        val = provided.get(name, '')
        val = '' if val is None else str(val)
        if len(val) > MAX_INPUT_LEN:
            return None, f"Input '{field['label']}' is too long (max {MAX_INPUT_LEN} chars)."
        if field['required'] and val == '':
            return None, f"Input '{field['label']}' is required."
        if field['type'] == 'number' and val != '':
            # Strict decimal only. Rejects 'inf'/'nan'/'1e5'/hex that float() would accept —
            # defense in depth for authors who pass a numeric input to the guest shell.
            if not re.fullmatch(r'-?\d{1,15}(\.\d{1,6})?', val):
                return None, f"Input '{field['label']}' must be a number."
        if field['type'] == 'select' and val != '' and val not in field['options']:
            return None, f"Invalid value for '{field['label']}'."
        clean[name] = val
    return clean, None

# ---- Author-provided per-challenge hooks (setup.sh / reset.sh) ----
# SECURITY: these files are baked into the image by the challenge author (trusted). Their text is
# run as root INSIDE the emulator guest via device.execute_script (the same mechanism the harness
# already uses). NO player-controlled data (APK, filenames, form inputs) is ever passed to them.
HOOK_TIMEOUT = 60

def _challenge_dir(client):
    return os.path.join(Config.CHALLENGES_FOLDER, client.CHALLENGE_NAME)

def run_hook(client, script_name):
    """Run challenges/<name>/<script_name> in the guest if it exists. Returns True if it ran."""
    path = os.path.join(_challenge_dir(client), script_name)
    if not os.path.isfile(path):
        return False
    with open(path, 'r', encoding='utf-8') as fh:
        body = fh.read()
    env_name = 'CHALLENGE_FLAG_' + re.sub(r'[^A-Za-z0-9]', '_', client.CHALLENGE_NAME).upper()
    flag = os.environ.get(env_name, '')
    script = f'FLAG={shlex.quote(flag)}\nexport FLAG\n{body}\n'
    res = device_manager.device.execute_script(script, timeout=HOOK_TIMEOUT)
    rc = getattr(res, 'exitcode', 0)
    if rc not in (0, None):
        raise Exception(f'{script_name} failed (exit {rc})')
    return True

def default_callback(poc_app, update_status, inputs=None, device=None):
    """Default callback for challenge.toml-only challenges (setup.sh already ran in INITIALIZING)."""
    poc_app.start()

def load_challenge(folder):
    """Build a client object from challenges/<name>/{client.py,challenge.toml}. Raises on error."""
    name = os.path.basename(folder)
    client_py = os.path.join(folder, 'client.py')
    toml_path = os.path.join(folder, 'challenge.toml')
    cfg = {}
    if os.path.isfile(toml_path):
        with open(toml_path, 'rb') as fh:
            cfg = tomllib.load(fh)

    if os.path.isfile(client_py):
        client = import_module(client_py.replace(os.sep, ".")[:-3])
    else:
        client = SimpleNamespace()
    client.CHALLENGE_NAME = name

    # client.py wins for anything it defines; challenge.toml fills the rest.
    if not getattr(client, 'PACKAGE_NAME', None) and cfg.get('package_name'):
        client.PACKAGE_NAME = str(cfg['package_name'])
    if not hasattr(client, 'TIMEOUT'):
        client.TIMEOUT = int(cfg.get('timeout', 300))
    if not hasattr(client, 'SCREENSHOT_DELAY') and 'screenshot_delay' in cfg:
        client.SCREENSHOT_DELAY = int(cfg['screenshot_delay'])
    if not hasattr(client, 'INPUTS') and isinstance(cfg.get('inputs'), list):
        client.INPUTS = cfg['inputs']
    backend = cfg.get('backend')
    if isinstance(backend, dict) and 'port' in backend and not hasattr(client, 'BACKEND_PORT'):
        client.BACKEND_PORT = int(backend['port'])
    if not getattr(client, 'callback', None):
        client.callback = default_callback

    if not getattr(client, 'PACKAGE_NAME', None):
        raise ValueError('PACKAGE_NAME / package_name is required')
    if not PKG_RE.match(client.PACKAGE_NAME):
        raise ValueError('invalid package name')
    return client

def run_reset(client):
    return run_hook(client, 'reset.sh')

def clear_logcat():
    try:
        device_manager.device.execute_script('logcat -c', timeout=5)
    except Exception:
        pass

def dismiss_system_dialogs(rounds=3):
    """Clear ANR / "isn't responding" system dialogs before the proof screenshot.

    Android emulators routinely pop "System UI isn't responding" / app-ANR dialogs
    under load. CLOSE_SYSTEM_DIALOGS dismisses them like tapping "Wait" — the
    foreground app is kept, so the exploit output stays visible. Best effort only;
    a failure here must never block the screenshot."""
    for _ in range(rounds):
        try:
            device_manager.device.execute_script(
                'am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS', timeout=5)
        except Exception:
            break
        time.sleep(0.4)

PKG_RE = re.compile(r'^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$')

def capture_logcat(packages, max_lines=800):
    """Dump logcat and keep only lines from the given packages (and their current pids)."""
    # Only trust valid Android package names (defense in depth: these reach a root shell).
    packages = [p for p in packages if p and PKG_RE.match(p)]
    pats = [p for p in packages]
    try:
        for pkg in list(packages):
            try:
                out = device_manager.device.execute_script('pidof ' + shlex.quote(pkg), timeout=5).stdout.decode().strip()
                pats += [tok for tok in out.split() if tok]
            except Exception:
                pass
        raw = device_manager.device.execute_script('logcat -d -v threadtime', timeout=20).stdout.decode('utf-8', 'replace')
    except Exception as e:
        return f'(failed to capture logcat: {e})'
    lines = [ln for ln in raw.splitlines() if any(p in ln for p in pats)]
    if not lines:
        return '(no log lines captured for the challenge or POC apps)'
    return '\n'.join(lines[-max_lines:])

def emit_queue_stats():
    with queue_lock:
        pending = len([q for q in queue if q.status == Status.PENDING_QUEUE])
        processing = len([q for q in queue if q.status not in [Status.COMPLETED, Status.ERROR, Status.PENDING_QUEUE]])
        queue_size = pending + processing
        
    socketio.emit('queue_stats', {'queue_size': queue_size})

def emit_status_update(queue_item, update_stats=True):
    status_data = {
        'id': queue_item.id,
        'status': queue_item.status.value if hasattr(queue_item.status, 'value') else queue_item.status,
        'error': queue_item.error,
        'created_at': queue_item.created_at.isoformat() if queue_item.created_at else None,
        'completed_at': queue_item.completed_at.isoformat() if queue_item.completed_at else None,
        'duration': queue_item.duration,
        'challenge': queue_item.client.CHALLENGE_NAME,
        'has_logs': bool(queue_item.logs)
    }
    socketio.emit('status_update', status_data, room=f'queue_{queue_item.id}')
    if update_stats:
        emit_queue_stats()

@app.route('/')
def index():
    challenges = [client.CHALLENGE_NAME for client in clients]
    
    if not device_manager.is_device_ready():
        return jsonify({'status': 'error', 'message': 'Device is not ready! Please come back later.'})
    
    if 'challenge' not in session:
        challenge = Challenge.generate(Config.POW_DIFFICULTY)
        session['challenge'] = str(challenge)
    
    # Self-hosted solve command (no external domain).
    pow_cmd = f"curl -s {request.host_url}solve | python3 - {session.get('challenge', 'CHALLENGE_TOKEN')}"
    return render_template('index.html', challenges=challenges, enable_pow=Config.ENABLE_POW, pow_cmd=pow_cmd)

# Self-hosted PoW solver (pure stdlib, no external domain, no pip deps). Matches pow.py's
# VDF exactly. Players run:  curl -s <site>/solve | python3 - <challenge>
POW_SOLVER = r'''#!/usr/bin/env python3
import sys, base64, struct
MOD = 2 ** 1279 - 1
EXP = 2 ** 1277
def _l2b(n):
    return n.to_bytes((n.bit_length() + 7) // 8 or 1, 'big')
def solve(chal):
    v, d_b64, x_b64 = chal.split('.', 2)
    if v != 's':
        raise SystemExit('bad challenge version')
    d = struct.unpack('>I', base64.standard_b64decode(d_b64))[0]
    y = int.from_bytes(base64.standard_b64decode(x_b64), 'big')
    for _ in range(d):
        y = pow(y, EXP, MOD)   # inverse of square (sqrt; MOD % 4 == 3, EXP == (MOD+1)//4)
        y ^= 1                 # inverse of xor1
    return 's.' + base64.standard_b64encode(_l2b(y)).decode()
if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: solve.py <challenge>')
    print(solve(sys.argv[1]))
'''

@app.route('/solve')
def pow_solver():
    return Response(POW_SOLVER, mimetype='text/x-python')

@app.route('/challenges')
def challenges_schema():
    return jsonify([
        {'name': c.CHALLENGE_NAME, 'inputs': challenge_input_schema(c)}
        for c in clients
    ])

@app.route('/upload', methods=['POST'])
def upload():
    if not device_manager.is_device_ready():
        return jsonify({'status': 'error', 'message': 'Device is not ready! Please come back later.'})

    if Config.ENABLE_POW:
        try:
            solution = request.form.get('solution')
            challenge = Challenge.from_string(session.get('challenge'))

            if not solution or not check(challenge, solution):
                return jsonify({'status': 'error', 'message': 'Incorrect solution!'})

            session.pop('challenge', None)
        except Exception:
            return jsonify({'status': 'error', 'message': 'Invalid solution or something went wrong!'})

    chall_name = request.form.get('chall_name')
    if not chall_name:
        return jsonify({'status': 'error', 'message': 'Please specify the challenge name!'})

    client = next((c for c in clients if c.CHALLENGE_NAME == chall_name), None)
    if not client:
        return jsonify({'status': 'error', 'message': 'Invalid challenge!'})

    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file uploaded!'})
    
    file = request.files['file']
    if not file or file.filename == '' or not allowed_file(file.filename):
        return jsonify({'status': 'error', 'message': 'Invalid file!'})

    # Per-challenge user inputs: accept a JSON blob 'inputs' or individual input_<name> fields.
    provided_inputs = {}
    raw_inputs = request.form.get('inputs')
    if raw_inputs:
        try:
            import json as _json
            provided_inputs = _json.loads(raw_inputs) or {}
            if not isinstance(provided_inputs, dict):
                provided_inputs = {}
        except Exception:
            return jsonify({'status': 'error', 'message': 'Invalid inputs format!'})
    for field in challenge_input_schema(client):
        fv = request.form.get(f"input_{field['name']}")
        if fv is not None:
            provided_inputs[field['name']] = fv
    clean_inputs, input_err = validate_inputs(client, provided_inputs)
    if input_err:
        return jsonify({'status': 'error', 'message': input_err})

    # Identify the uploader (for per-session limits + owning their job's logs/screenshot).
    sid = session.get('sid')
    if not sid:
        sid = uuid.uuid4().hex
        session['sid'] = sid
    # Source IP — a harder anti-abuse key than the cookie sid. X-Forwarded-For is only
    # honored when TRUSTED_PROXY_COUNT > 0 (real proxies in front); otherwise it is
    # attacker-controlled and ignored, so players can't spoof it to dodge the per-IP cap.
    src_ip = request.remote_addr or 'unknown'
    n = Config.TRUSTED_PROXY_COUNT
    if n > 0:
        fwd = [h.strip() for h in request.headers.get('X-Forwarded-For', '').split(',') if h.strip()]
        if fwd:
            src_ip = fwd[-n] if len(fwd) >= n else fwd[0]

    with queue_lock:
        active = len([q for q in queue if not q.is_completed])
        if active >= Config.MAX_QUEUE_SIZE:
            return jsonify({'status': 'error', 'message': 'Queue is full, please try again later!'})
        mine = len([q for q in queue if not q.is_completed and q.owner == sid])
        if mine >= Config.MAX_JOBS_PER_SESSION:
            return jsonify({'status': 'error', 'message': 'You already have a job in progress. Please wait for it to finish.'}), 429
        mine_ip = len([q for q in queue if not q.is_completed and q.owner_ip == src_ip])
        if mine_ip >= Config.MAX_JOBS_PER_IP:
            return jsonify({'status': 'error', 'message': 'Too many jobs in progress from your address. Please wait.'}), 429

    queue_id = str(uuid.uuid4())
    filename = secure_filename(f"{queue_id}.apk")
    file_path = os.path.join(Config.UPLOAD_FOLDER, filename)
    
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    file.save(file_path)

    with queue_lock:
        q = Queue(
            id=queue_id,
            status=Status.PENDING_QUEUE,
            client=client,
            inputs=clean_inputs,
            owner=sid,
            owner_ip=src_ip,
        )
        queue.append(q)
    
    emit_queue_stats()

    challenge = Challenge.generate(Config.POW_DIFFICULTY)
    session['challenge'] = str(challenge)

    return jsonify({'status': 'success', 'id': queue_id, 'challenge': str(challenge), 'next_pow': str(challenge)})

def _owns_job(job_id):
    sid = session.get('sid')
    with queue_lock:
        item = next((q for q in queue if q.id == job_id), None)
    if item is None:
        return None, False
    # Strict: a job must have an owner and it must match this session (default-deny).
    return item, (item.owner is not None and item.owner == sid)

@app.route('/screenshot/<id>')
def screenshot(id):
    item, owned = _owns_job(id)
    if item is None or not owned:
        return 'Screenshot not found!', 404

    filename = secure_filename(id)
    if not filename.endswith('.png'):
        filename += '.png'

    file_path = os.path.join(Config.SCREENSHOT_FOLDER, filename)

    if not os.path.exists(file_path):
        return 'Screenshot not found!', 404

    return send_file(file_path, mimetype='image/png')

@app.route('/logs/<id>')
def logs(id):
    item, owned = _owns_job(id)
    if item is None or not owned:
        return 'Job not found!', 404
    if not item.logs:
        return 'No logs available yet for this job.', 404
    return Response(item.logs, mimetype='text/plain')

STREAM_ACTIVE_STATUSES = {Status.INITIALIZING, Status.INSTALLING_POC, Status.RUNNING_CHALLENGE,
                          Status.RUNNING_POC, Status.TAKING_SCREENSHOT}
STREAM_INTERVAL = 0.6        # ~1.5 fps
STREAM_MAX_SECONDS = 120     # hard cap per stream
STREAM_MAX_CONCURRENT = 4    # bound waitress threads / device load
_stream_count = 0
_stream_lock = Lock()

@app.route('/frame/<id>')
def frame(id):
    """One live JPEG of the emulator screen, owner-bound, only while this job is running.
    The browser polls this (~1.5 fps) for a 'live view'. Single short request = works under
    waitress (unlike a long-lived MJPEG stream). No adb/scrcpy is exposed to players."""
    item, owned = _owns_job(id)
    if item is None or not owned:
        return 'Job not found!', 404
    if item.is_completed:
        return 'Job finished', 409
    with _stream_lock:
        if _stream_count >= STREAM_MAX_CONCURRENT:
            return 'Busy', 429
    try:
        data = device_manager.device.screenshot().getvalue()   # already JPEG
    except Exception:
        return 'No frame', 503
    resp = Response(data, mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route('/reset/<challenge>', methods=['POST'])
def reset_challenge(challenge):
    """Admin only: run the challenge's author-provided reset.sh in the guest. Needs ADMIN_TOKEN."""
    token = request.headers.get('X-Admin-Token', '')
    if not Config.ADMIN_TOKEN or not hmac.compare_digest(token.encode(), Config.ADMIN_TOKEN.encode()):
        return 'Not found!', 404
    client = next((c for c in clients if c.CHALLENGE_NAME == challenge), None)
    if not client:
        return jsonify({'status': 'error', 'message': 'Unknown challenge'}), 404
    with queue_lock:
        busy = any(q.status in STREAM_ACTIVE_STATUSES or q.status == Status.TAKING_SCREENSHOT for q in queue)
    if busy:
        return jsonify({'status': 'error', 'message': 'A job is running; try again later'}), 409
    try:
        ran = run_reset(client)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    return jsonify({'status': 'success', 'ran': ran})

@app.route('/device_status')
def device_status():
    return jsonify({'status': 'success', 'device_ready': device_manager.is_device_ready()})

@app.errorhandler(413)
def too_large(e):
    return jsonify({'status': 'error', 'message': 'File too large!'}), 413

@socketio.on('connect')
def handle_connect(auth):
    emit_queue_stats()

@socketio.on('disconnect')
def handle_disconnect():
    pass

@socketio.on('join_queue')
def handle_join_queue(data):
    queue_id = data.get('queue_id')
    if not queue_id:
        emit('error', {'message': 'Queue ID is required'})
        return
    
    join_room(f'queue_{queue_id}')

@socketio.on('leave_queue')
def handle_leave_queue(data):
    queue_id = data.get('queue_id')
    if queue_id:
        leave_room(f'queue_{queue_id}')

@socketio.on('get_status')
def handle_get_status(data):
    queue_id = data.get('queue_id')
    if not queue_id:
        emit('error', {'message': 'Queue ID is required'})
        return
    
    with queue_lock:
        q = next((q for q in queue if q.id == queue_id), None)
    
    if q:
        emit_status_update(q)
    else:
        emit('error', {'message': 'Queue item not found'})

class QueueThread(Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.running = True

    def run(self):
        while self.running:
            try:
                with queue_lock:
                    pending = [q for q in queue if q.status == Status.PENDING_QUEUE]
                
                if pending:
                    for q in pending:
                        if not self.running:
                            break
                        self._process_queue(q)

                emit_queue_stats()
                self._reap_old_jobs()
            except Exception as e:
                import traceback
                traceback.print_exc()

            time.sleep(5)

    def _reap_old_jobs(self):
        """Delete finished jobs (and their screenshots) older than the retention window."""
        from datetime import datetime
        cutoff = Config.JOB_RETENTION_SECONDS
        now = datetime.now()
        with queue_lock:
            keep, drop = [], []
            for q in queue:
                age = (now - q.completed_at).total_seconds() if (q.is_completed and q.completed_at) else -1
                (drop if age >= cutoff else keep).append(q)
            queue[:] = keep
        for q in drop:
            for p in (os.path.join(Config.SCREENSHOT_FOLDER, f'{q.id}.png'),
                      os.path.join(Config.UPLOAD_FOLDER, f'{q.id}.apk')):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def stop(self):
        self.running = False

    def _process_queue(self, q: Queue):
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self._do_work, q)
                future.result(timeout=q.client.TIMEOUT)
        except TimeoutError:
            q.mark_error("Timeout! Please try again.")
            emit_status_update(q)
        except Exception as e:
            import traceback
            traceback.print_exc()
            q.mark_error('Error: ' + str(e))
            emit_status_update(q)

    def _do_work(self, q: Queue):
        self._poc_app = None
        try:
            self._run(q)
        finally:
            # Always remove the POC (even on error/timeout) so nothing persists for the next player.
            if self._poc_app is not None:
                try:
                    self._poc_app.uninstall()
                except Exception:
                    pass

    def _run(self, q: Queue):
        chall_app = None
        poc_app = None

        q.update_status(Status.INITIALIZING)
        emit_status_update(q)

        clear_logcat()  # start a clean log buffer for this job

        chall_app = device_manager.device.application(q.client.PACKAGE_NAME)
        if not chall_app.is_installed():
            # Normally installed at device boot (device/start.sh). Fallback only if a
            # challenge.apk ships with the plugin; never install a debuggable build.
            apk_path = os.path.join(os.getcwd(), Config.CHALLENGES_FOLDER, q.client.CHALLENGE_NAME, 'challenge.apk')
            if not os.path.exists(apk_path):
                raise Exception('Challenge is not installed on the device! Contact an admin.')

            out, _ = run_process('aapt', ['dump', 'badging', apk_path])
            if 'application-debuggable' in out:
                raise Exception('Refusing to install a debuggable challenge APK!')

            device_manager.device.upload_file(apk_path, '/data/local/tmp/challenge.apk')
            device_manager.device.install_local_file('/data/local/tmp/challenge.apk')
            device_manager.device.delete_file('/data/local/tmp/challenge.apk')

            if not chall_app.is_installed():
                raise Exception('Failed to install challenge APK!')

        # Author-provided setup.sh (guest root; no player input). Runs before the POC is installed.
        try:
            run_hook(q.client, 'setup.sh')
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise Exception('Challenge setup failed! Contact an admin.')

        q.update_status(Status.INSTALLING_POC)
        emit_status_update(q)

        apk_path = os.path.join(os.getcwd(), Config.UPLOAD_FOLDER, f'{q.id}.apk')
        if not os.path.exists(apk_path):
            raise Exception('POC APK file not found!')

        out, err = run_process('aapt', ['dump', 'badging', apk_path])
        match = re.search(r"package: name='(.*?)'", out)
        if err or not match:
            raise Exception('Invalid APK file!')

        package_name = match.group(1)
        if not PKG_RE.match(package_name):
            raise Exception('Invalid APK package name!')
        # The POC may not impersonate ANY loaded challenge app (not just the current one),
        # which would otherwise let it masquerade as a not-yet-installed challenge.
        if any(package_name == c.PACKAGE_NAME for c in clients):
            raise Exception('Your POC cannot use the same package name as a challenge!')

        device_manager.device.upload_file(apk_path, '/data/local/tmp/poc.apk')
        device_manager.device.install_local_file('/data/local/tmp/poc.apk')
        device_manager.device.delete_file('/data/local/tmp/poc.apk')

        # The uploaded APK is no longer needed on the web container once installed.
        try:
            os.remove(apk_path)
        except OSError:
            pass

        poc_app = device_manager.device.application(package_name)
        self._poc_app = poc_app
        if not poc_app.is_installed():
            raise Exception('Failed to install POC APK!')

        try:
            if hasattr(q.client, 'callback') and q.client.callback:
                def update_status(status):
                    q.update_status(status)
                    emit_status_update(q)
                try:
                    # Pass optional context (inputs, device) only if the callback declares it
                    # (or uses **kwargs). Backward compatible with callback(poc_app, update_status).
                    import inspect
                    inputs = q.inputs or {}
                    try:
                        params = inspect.signature(q.client.callback).parameters
                        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                    except (TypeError, ValueError):
                        params, has_varkw = {}, False
                    call_kwargs = {}
                    if 'inputs' in params or has_varkw:
                        call_kwargs['inputs'] = inputs
                    if 'device' in params or has_varkw:
                        call_kwargs['device'] = device_manager.device
                    q.client.callback(poc_app, update_status, **call_kwargs)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    raise Exception('Something went wrong while testing your POC. This is a normal behavior and you should try again.')
        finally:
            # Capture logcat for the challenge + POC apps (even if the callback failed).
            q.logs = capture_logcat([q.client.PACKAGE_NAME, package_name])
            emit_status_update(q)

        q.update_status(Status.TAKING_SCREENSHOT)
        emit_status_update(q)

        # Let the POC/target UI settle before the proof screenshot.
        delay = getattr(q.client, 'SCREENSHOT_DELAY', Config.SCREENSHOT_DELAY)
        if delay and delay > 0:
            time.sleep(delay)

        # Dismiss any ANR / "isn't responding" dialog so it doesn't cover the proof.
        dismiss_system_dialogs()

        screenshot = device_manager.device.screenshot()
        screenshot.save(os.path.join(Config.SCREENSHOT_FOLDER, f'{q.id}.png'))

        q.mark_completed()
        emit_status_update(q)

if __name__ == '__main__':
    folders = set()
    for pat in ("client.py", "challenge.toml"):
        for f in glob.glob(os.path.join("challenges", "*", pat)):
            folders.add(os.path.dirname(f))
    for folder in sorted(folders):
        try:
            client = load_challenge(folder)
            clients.append(client)
            print(f"[i] Loaded challenge: {client.CHALLENGE_NAME} ({client.PACKAGE_NAME})")
        except Exception as e:
            print(f"[!] Failed to load {folder}: {e}")

    # Start device monitoring
    device_manager.start_monitoring()

    # Start queue processing thread
    queue_thread = QueueThread()
    queue_thread.start()

    if Config.DEBUG:
        # Dev only: Werkzeug reloader/debugger.
        socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
    else:
        # Production: a real WSGI server. Socket.IO runs in threading async mode
        # (HTTP long-polling transport), which waitress serves.
        from waitress import serve
        print("[i] Serving on 0.0.0.0:5000 via waitress")
        serve(app, host='0.0.0.0', port=5000, threads=16)
