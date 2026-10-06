import os

class Config:
    DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
    # PoW is ON by default to throttle abuse of the shared emulator. Disable only for
    # local testing with ENABLE_POW=false.
    ENABLE_POW = os.environ.get("ENABLE_POW", "true").lower() == "true"
    SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(24)

    # Socket.IO allowed origins. "*" for convenience; set CORS_ORIGINS to your site
    # origin(s) (comma-separated) in production.
    _cors = os.environ.get("CORS_ORIGINS", "*")
    CORS_ORIGINS = "*" if _cors.strip() == "*" else [o.strip() for o in _cors.split(",") if o.strip()]

    # Completed/errored jobs (and their screenshots) are reaped after this many seconds
    # to bound disk + memory on a long-running server.
    JOB_RETENTION_SECONDS = int(os.environ.get("JOB_RETENTION_SECONDS", "3600"))

    MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", "100")) * 1024 * 1024
    ALLOWED_EXTENSIONS = {'apk'}

    UPLOAD_FOLDER = 'uploads'
    SCREENSHOT_FOLDER = 'screenshots'
    CHALLENGES_FOLDER = 'challenges'

    MAX_QUEUE_SIZE = int(os.environ.get("MAX_QUEUE_SIZE", "50"))

    # Max concurrent (not-yet-finished) jobs a single session may have (anti-DoS).
    MAX_JOBS_PER_SESSION = int(os.environ.get("MAX_JOBS_PER_SESSION", "1"))
    # Max concurrent jobs from one source IP (cookie-independent anti-DoS).
    MAX_JOBS_PER_IP = int(os.environ.get("MAX_JOBS_PER_IP", "2"))

    # Session cookie hardening. Behind a TLS proxy set SESSION_COOKIE_SECURE=true.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

    # Seconds to wait after the POC callback before taking the proof screenshot,
    # so the POC/target UI has time to render. Per-challenge override: set
    # SCREENSHOT_DELAY in the challenge's client.py.
    SCREENSHOT_DELAY = int(os.environ.get("SCREENSHOT_DELAY", "3"))

    ADB_TIMEOUT = int(os.environ.get("ADB_TIMEOUT", "30"))
    PROCESS_TIMEOUT = int(os.environ.get("PROCESS_TIMEOUT", "30"))
    APK_INSTALL_TIMEOUT = int(os.environ.get("APK_INSTALL_TIMEOUT", "60"))

    ADB_HOST = os.environ.get("ADB_HOST", "device")
    ADB_PORT = int(os.environ.get("ADB_PORT", "5037"))

    # LAMDA is reached through the device container's socat (port 65000).
    DEVICE_HOST = os.environ.get("DEVICE_HOST", ADB_HOST)

    POW_DIFFICULTY = int(os.environ.get("POW_DIFFICULTY", "250000"))
