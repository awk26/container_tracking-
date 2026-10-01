"""Hapag-Lloyd sign-in helper.

Starts your installed Google Chrome (visible, not headless) with a remote
debugging port and its own profile, attaches Playwright to it, signs in
with the account in .env (USER_HAPAG / PASSWORD_HAPAG), then goes to the
home page. Playwright never launches a browser itself.

Credentials are only read from the environment and are never printed.
If Hapag shows a human-verification check, complete it by hand in the
Chrome window: this script only waits for the sign-in form to appear and
does not interact with the check.

Run directly:  python scrapers/hapag.py
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

HOME_URL = "https://www.hapag-lloyd.com/en/home.html"

# Not 9222: maersk.py uses that port, and both only check whether something
# is listening on it, so sharing it would let one attach to the other's Chrome.
CDP_PORT = int(os.getenv("HAPAG_CDP_PORT", "9223"))
CDP_URL = f"http://localhost:{CDP_PORT}"

_LOG_IN_BUTTON = 'a:has-text("Log in"), button:has-text("Log in")'

# Hapag's sign-in page (Azure AD B2C): both fields and the button are on one form.
_USERNAME_FIELD = "#signInName"
_PASSWORD_FIELD = "#password"
_SUBMIT_BUTTON = "#next"

# Long on purpose: leaves time to finish a verification check by hand.
_FORM_TIMEOUT_MS = 300000

_chrome_process = None


# ============================================================
# CHROME
# ============================================================

def _find_chrome_path():
    env_path = os.getenv("HAPAG_CHROME_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    if sys.platform.startswith("win"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]

    for path in candidates:
        if os.path.exists(path):
            return path

    raise FileNotFoundError(
        "Google Chrome not found. Set HAPAG_CHROME_PATH to its executable."
    )


def _data_dir():
    env_dir = os.getenv("HAPAG_CHROME_DATA_DIR")
    if env_dir:
        path = Path(env_dir)
    elif sys.platform.startswith("win"):
        path = Path(os.getenv("TEMP", ".")) / "hapag_chrome_profile"
    else:
        path = Path("/tmp/hapag_chrome_profile")

    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _chrome_debugging_available():
    try:
        return requests.get(f"{CDP_URL}/json/version", timeout=2).status_code == 200
    except Exception:
        return False


def ensure_chrome_running():
    """Start Chrome if nothing is listening on the debug port.

    Returns True if this call started Chrome, False if it was already up.
    """

    global _chrome_process

    if _chrome_debugging_available():
        print("[HAPAG] Chrome is already running.")
        return False

    chrome_path = _find_chrome_path()
    data_dir = _data_dir()

    print(f"[HAPAG] Starting Chrome: {chrome_path}")

    command = [
        chrome_path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={data_dir}",
        "--start-maximized",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--disable-notifications",
        "--disable-infobars",
        HOME_URL,
    ]

    popen_kwargs = {}
    if not sys.platform.startswith("win"):
        command.extend(["--disable-dev-shm-usage", "--no-sandbox"])
        popen_kwargs["start_new_session"] = True

    _chrome_process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_kwargs,
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        if _chrome_debugging_available():
            print("[HAPAG] Chrome is ready.")
            return True
        time.sleep(0.5)

    raise RuntimeError("Chrome's debugging interface did not become available.")


def close_chrome():
    """Close only the Chrome this script started."""

    global _chrome_process

    if _chrome_process is None:
        return

    pid = _chrome_process.pid
    print("[HAPAG] Closing Chrome...")

    try:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    finally:
        _chrome_process = None


# ============================================================
# LOGIN
# ============================================================

def login_to_hapag(page):
    username = os.getenv("USER_HAPAG")
    password = os.getenv("PASSWORD_HAPAG")

    if not username or not password:
        raise RuntimeError("USER_HAPAG and PASSWORD_HAPAG must be set in .env")

    print("[HAPAG] Opening Hapag-Lloyd...")
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90000)

    # Cookie banner, if it is the standard OneTrust one; ignore if absent.
    try:
        page.locator("#onetrust-accept-btn-handler").click(timeout=5000)
    except Exception:
        pass

    print("[HAPAG] Going to the login page...")
    page.locator(_LOG_IN_BUTTON).first.click(timeout=30000)

    print("[HAPAG] Waiting for the sign-in form...")
    username_field = page.locator(_USERNAME_FIELD)
    username_field.wait_for(state="visible", timeout=_FORM_TIMEOUT_MS)
    username_field.fill(username)

    page.locator(_PASSWORD_FIELD).fill(password)
    page.locator(_SUBMIT_BUTTON).click()

    print("[HAPAG] Signing in...")
    try:
        page.wait_for_load_state("networkidle", timeout=60000)
    except Exception:
        pass

    print("[HAPAG] Going to the home page...")
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=90000)

    if page.locator(_LOG_IN_BUTTON).first.is_visible(timeout=5000):
        print("[HAPAG] Warning: the page still shows 'Log in' - sign-in may not have worked.")
    else:
        print("[HAPAG] Signed in.")
    page.goto("https://www.ag-lloyd.com/solutions/tracking/#/", wait_until="domcontentloaded", timeout=90000)

def track_container(raw_container_number):
    """Kept only so app.py can still import this module."""

    from .errors import ContainerNotFoundError

    raise ContainerNotFoundError("Hapag-Lloyd tracking isn't implemented yet.")


if __name__ == "__main__":
    started = ensure_chrome_running()

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()

            login_to_hapag(page)

            input("Press Enter to close the browser...")
    finally:
        if started:
            close_chrome()
