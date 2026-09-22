import os
import platform
import shutil
import signal
import subprocess
import time
import requests
from playwright.sync_api import sync_playwright


# ============================================================
# CONFIGURATION
# ============================================================

# CONTAINER_NUMBER = "SLZU2577558"

def _find_chrome_path() -> str:
    """Locates a real Chrome/Chromium install across OSes.

    Set MAERSK_CHROME_PATH to override this entirely (e.g. a non-standard
    install location on a server).
    """

    env_override = os.environ.get("MAERSK_CHROME_PATH")
    if env_override:
        return env_override

    system = platform.system()

    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]

    for path in candidates:
        if os.path.exists(path):
            return path

    # Not at any known path - try the PATH itself.
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    # Nothing found: fall back to the first candidate so the resulting
    # error at least names a sensible path instead of a blank string.
    return candidates[0]


def _default_data_dir() -> str:
    env_override = os.environ.get("MAERSK_CHROME_DATA_DIR")
    if env_override:
        return env_override
    if platform.system() == "Windows":
        return r"C:\chrome_debug"
    return os.path.expanduser("~/.maersk_chrome_debug")


CHROME_PATH = _find_chrome_path()
DATA_DIR = _default_data_dir()

TRACKING_URL = "https://www.maersk.com/tracking/"

EVENT_COLUMNS = [
    "date",
    "location",
    "milestone",
]


# ============================================================
# VIRTUAL DISPLAY (Linux servers only)
# ============================================================

# Chrome here is launched non-headless on purpose (Maersk detects and
# blocks headless mode - see ensure_chrome_running below), which means it
# needs somewhere to render. On Windows/macOS desktops that's just the
# real display. On a Linux server with no X server, there's normally
# nothing to render into at all - so start a throwaway virtual one
# (Xvfb) automatically instead of requiring that to be set up by hand
# for every deployment.
#
# One gotcha this guards against: if the server process is started from an
# SSH session with X11 forwarding on (e.g. `ssh -X`, or a client like
# MobaXterm/PuTTY with X11 forwarding enabled), DISPLAY is already set to a
# tunnel back to *that client machine's* X server - so Chrome would silently
# render on whoever's laptop happened to start the process, and break the
# moment that laptop disconnects or shuts down. Set MAERSK_FORCE_XVFB=1 in
# the server's environment to always ignore any inherited DISPLAY and use a
# local Xvfb display instead, regardless of how the process got launched.

_xvfb_process = None


def _ensure_display():
    if platform.system() == "Windows":
        return

    force_xvfb = os.environ.get("MAERSK_FORCE_XVFB", "").strip().lower() in ("1", "true", "yes")
    inherited_display = os.environ.get("DISPLAY")

    if inherited_display and not force_xvfb:
        return  # a real or already-started virtual display exists

    global _xvfb_process
    if _xvfb_process is not None and _xvfb_process.poll() is None:
        return  # already started earlier in this process

    xvfb_path = shutil.which("Xvfb")
    if not xvfb_path:
        print(
            "[WARN] No usable DISPLAY and Xvfb isn't installed - Chrome will "
            "likely fail to launch. Install it, e.g. on RHEL/Fedora: "
            "sudo dnf install -y xorg-x11-server-Xvfb"
        )
        return

    display_num = os.environ.get("MAERSK_XVFB_DISPLAY", ":99")
    if force_xvfb and inherited_display:
        print(
            f"[INFO] MAERSK_FORCE_XVFB is set - ignoring inherited DISPLAY="
            f"{inherited_display!r} (likely an SSH X11 forward) and starting "
            f"a local virtual display on {display_num} instead..."
        )
    else:
        print(f"[INFO] No DISPLAY set - starting a virtual display on {display_num}...")

    _xvfb_process = subprocess.Popen(
        [xvfb_path, display_num, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)  # give it a moment to bind before Chrome tries to use it
    os.environ["DISPLAY"] = display_num


# ============================================================
# START / CONNECT TO CHROME
# ============================================================

def ensure_chrome_running():
    """
    Checks if Chrome is running on debugging port 9222.

    If Chrome is already running on port 9222:
        - Connect to it
        - Do not launch another Chrome

    If Chrome is not running:
        - Launch Chrome with remote debugging enabled

    Returns:
        Popen handle if we launched Chrome
        None if Chrome was already running
    """

    try:
        response = requests.get(
            "http://localhost:9222/json/version",
            timeout=1
        )

        if response.status_code == 200:
            print("[INFO] Connected to existing Chrome instance on port 9222.")
            return None

    except Exception:
        print("[INFO] Remote Chrome not detected. Launching Chrome...")

    _ensure_display()

    # Ensure profile directory exists
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)

    # NOT headless: tested this directly against Maersk (2026-09-21) with
    # --headless=new using this exact profile/cookies, and Maersk silently
    # returned "No results found" for a container that has real public
    # tracking data - the same lookup succeeds immediately in normal mode.
    # So headless isn't just slower here, it's detected and given a
    # degraded response. Launched minimized instead so it doesn't grab
    # focus or sit in the way, without pretending to be headless.
    # start_new_session makes Chrome (and the child renderer/GPU processes
    # it spawns) its own process group on Linux/macOS, so close_chrome can
    # reliably kill the whole tree with os.killpg instead of just the one
    # PID we happen to hold a handle to.
    popen_kwargs = {} if platform.system() == "Windows" else {"start_new_session": True}

    chrome_process = subprocess.Popen(
        [
            CHROME_PATH,
            "--remote-debugging-port=9222",
            f"--user-data-dir={DATA_DIR}",
            "--start-minimized",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        **popen_kwargs,
    )

    # Wait for debugging interface
    for _ in range(10):
        try:
            res = requests.get(
                "http://localhost:9222/json/version",
                timeout=1
            )

            if res.status_code == 200:
                print("[INFO] Chrome launched and ready.")
                return chrome_process

        except Exception:
            time.sleep(1)

    raise RuntimeError(
        "Failed to launch Chrome on remote debugging port 9222."
    )


# ============================================================
# CLOSE CHROME
# ============================================================

def close_chrome(chrome_process):
    """
    Closes only the Chrome process launched by this script.

    If Chrome was already running before the script started,
    it will remain open.
    """

    if chrome_process is None:
        print(
            "[INFO] Chrome was already running before this script "
            "- leaving it open."
        )
        return

    print("\n[INFO] Closing the Chrome process this script launched...")

    if platform.system() == "Windows":
        subprocess.run(
            [
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(chrome_process.pid)
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False
        )
    else:
        # Kill the whole process group (Chrome + its renderer/GPU/etc.
        # children), not just the one PID we have a handle to - otherwise
        # those children are silently left running. Relies on
        # start_new_session=True having been passed at launch.
        try:
            pgid = os.getpgid(chrome_process.pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already exited
        except Exception:
            try:
                chrome_process.kill()
            except Exception:
                pass

        try:
            chrome_process.wait(timeout=5)
        except Exception:
            pass

    print("[INFO] Chrome closed successfully.")


# ============================================================
# MAIN PUBLIC FUNCTION
# ============================================================

def process_active_chrome_tab(container_number: str = None) -> dict:
    """
    Main entry point.

    Returns:
        dict containing structured Maersk tracking information.
    """

    # Measured cold vs. warm (reused-tab) lookups against the real site and
    # a warm lookup wasn't actually faster - Maersk's own response time
    # dominates either way, not Chrome/page startup. So there's no speed
    # cost to closing Chrome after each call, and doing so avoids leaving
    # a Chrome process running in the background indefinitely.
    chrome_process = ensure_chrome_running()
    try:
        return _track_container(container_number)
    finally:
        close_chrome(chrome_process)


# ============================================================
# TRACK CONTAINER
# ============================================================

def _track_container(container_number: str = None) -> dict:

    with sync_playwright() as p:

        # Connect to Chrome
        browser = p.chromium.connect_over_cdp(
            "http://localhost:9222"
        )

        context = browser.contexts[0]

        # --------------------------------------------------------
        # Find existing Maersk tab
        # --------------------------------------------------------

        page = None

        for p_tab in context.pages:

            if "maersk.com" in p_tab.url:
                page = p_tab
                break

        # --------------------------------------------------------
        # Open new Maersk page if required
        # --------------------------------------------------------

        if not page:

            page = context.new_page()

            page.goto(
                TRACKING_URL,
                wait_until="domcontentloaded"
            )

        print(f"[INFO] Attached to page: {page.url}")

        # --------------------------------------------------------
        # Fill Container Number
        # --------------------------------------------------------

        container_input = page.locator(
            "#mc-input-track-input"
        )

        container_input.wait_for(
            state="visible",
            timeout=10000
        )

        container_input.fill(container_number)

        print(
            f"[INFO] Entered container number: "
            f"{container_number}"
        )

        # Now that the tab can carry over from a previous lookup, capture
        # whatever result text is already on the page (if any) *before*
        # clicking Track, so we can tell once it's actually been replaced
        # by this search's result rather than racing a stale "visible"
        # check against the old one.
        result_container = page.locator(
            'div[data-test="container"]'
        ).first

        previous_details_text = ""

        try:
            previous_details_text = result_container.locator(
                '[data-test="container-details"]'
            ).first.inner_text(timeout=1000)
        except Exception:
            pass

        # --------------------------------------------------------
        # Click Track
        # --------------------------------------------------------

        track_button = page.locator(
            'button[aria-label="Track"]'
        )

        track_button.click()

        print(
            "[INFO] Clicked track. Waiting for results..."
        )

        # --------------------------------------------------------
        # Wait for Tracking Result
        # --------------------------------------------------------

        if previous_details_text:
            try:
                page.wait_for_function(
                    """([selector, oldText]) => {
                        const el = document.querySelector(selector);
                        return !!el && el.innerText.trim() !== oldText;
                    }""",
                    arg=['[data-test="container-details"]', previous_details_text],
                    timeout=15000,
                )
            except Exception:
                # Didn't detect a change in time - fall through to the
                # plain visibility wait below rather than failing outright.
                pass

        result_container.wait_for(
            state="visible",
            timeout=30000
        )

        # ========================================================
        # CONTAINER INFORMATION
        # ========================================================

        container_details = result_container.locator(
            '[data-test="container-details"]'
        ).first

        container_number = ""
        container_type = ""

        try:

            detail_text = (
                container_details
                .inner_text()
                .strip()
            )

            parts = [
                x.strip()
                for x in detail_text.split("|")
            ]

            if len(parts) >= 1:
                container_number = parts[0]

            if len(parts) >= 2:
                container_type = parts[1]

        except Exception:
            pass

        # Fallback to input container number
       

        # ========================================================
        # ESTIMATED ARRIVAL
        # ========================================================

        eta = ""

        try:

            eta_element = result_container.locator(
                '[data-test="container-eta"]'
            )

            lines = [
                line.strip()
                for line in eta_element.inner_text().splitlines()
                if line.strip()
            ]

            if len(lines) >= 2:
                eta = lines[-1]

        except Exception:
            pass

        # ========================================================
        # EVENTS PARSING
        # ========================================================

        events = []

        event_items = result_container.locator(
            '[data-test^="transport-plan-item-"]'
        )

        event_count = event_items.count()

        print(
            f"[INFO] Parsing {event_count} tracking events..."
        )

        for i in range(event_count):

            item = event_items.nth(i)

            location = ""
            milestone = ""
            date = ""

            # ----------------------------------------------------
            # Location
            # ----------------------------------------------------

            try:

                location = (
                    item
                    .locator('[data-test="location-name"]')
                    .inner_text()
                    .splitlines()[0]
                    .strip()
                )

                print(
                    f"[INFO] Location: {location}"
                )

            except Exception:
                pass

            # ----------------------------------------------------
            # Milestone
            # ----------------------------------------------------

            try:

                milestone = (
                    item
                    .locator('[data-test="milestone"]')
                    .inner_text()
                    .splitlines()[0]
                    .strip()
                )

                print(
                    f"[INFO] Milestone: {milestone}"
                )

            except Exception:
                pass

            # ----------------------------------------------------
            # Date
            # ----------------------------------------------------

            try:

                date = (
                    item
                    .locator('[data-test="milestone-date"]')
                    .inner_text()
                    .strip()
                )

                print(
                    f"[INFO] Date: {date}"
                )

            except Exception:
                pass

            # ----------------------------------------------------
            # Add Event
            # ----------------------------------------------------

            events.append({
                "location": location,
                "milestone": milestone,
                "date": date
            })

        # ========================================================
        # SUMMARY
        # ========================================================

        summary_fields = {
            "container_number": container_number,
            "container_type": container_type,
            "eta": eta,
            "total_events": len(events),
        }

        # ========================================================
        # PRINT SUMMARY
        # ========================================================

        print(
            "\n========== TRACKING SUMMARY =========="
        )

        print(
            f"Container No : {container_number}"
        )

        print(
            f"Container Type: {container_type}"
        )

        print(
            f"ETA          : {eta}"
        )

        print(
            f"Total Events : {len(events)}"
        )

        print(
            "======================================"
        )

        # ========================================================
        # PRINT EVENT TIMELINE
        # ========================================================

        for idx, event in enumerate(events, 1):

            print(
                f"[{idx}] "
                f"{event['date']} | "
                f"{event['location']} -> "
                f"{event['milestone']}"
            )

        # ========================================================
        # FINAL STRUCTURED RESPONSE
        # ========================================================

        result = {
            "line": "maersk",
            "container_number": container_number,
            "summary_fields": summary_fields,
            "event_columns": EVENT_COLUMNS,
            "events": events,
            "source_url": TRACKING_URL,
        }

        # Note: deliberately not calling browser.close() here. For a
        # browser obtained via connect_over_cdp, Playwright sends it the
        # CDP Browser.close command - which actually quits the remote
        # Chrome, not just this script's connection to it. That would
        # undo the whole point of leaving Chrome running for reuse. The
        # `with sync_playwright()` block exiting is enough to end this
        # script's own connection.

        return result


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":

    result = process_active_chrome_tab()

    print("\n\n========== RETURN VALUE ==========")

    print(result)