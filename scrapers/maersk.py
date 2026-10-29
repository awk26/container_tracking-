
import os
import platform
import shutil
import signal
import subprocess
import time
import requests

from playwright.sync_api import sync_playwright

from .errors import ContainerNotFoundError


# ============================================================
# CONFIGURATION
# ============================================================

TRACKING_URL = "https://www.maersk.com/tracking/"
DEBUG_PORT = int(os.getenv("MAERSK_DEBUG_PORT", "9222"))
DEBUG_URL = f"http://127.0.0.1:{DEBUG_PORT}"

EVENT_COLUMNS = ["date", "location", "milestone"]

CHROME_STARTUP_TIMEOUT = int(
    os.getenv("MAERSK_CHROME_STARTUP_TIMEOUT", "20")
)

TRACKING_TIMEOUT = int(
    os.getenv("MAERSK_TRACKING_TIMEOUT", "45000")
)

RESULT_WAIT_SECONDS = int(
    os.getenv("MAERSK_RESULT_WAIT_SECONDS", "20")
)

_xvfb_process = None


# ============================================================
# CHROME PATH
# ============================================================

def _find_chrome_path() -> str:
    """Find Chrome/Chromium, allowing an environment override."""

    env_override = os.getenv("MAERSK_CHROME_PATH")

    if env_override:
        if os.path.isfile(env_override):
            return env_override

        found = shutil.which(env_override)

        if found:
            return found

        raise FileNotFoundError(
            f"MAERSK_CHROME_PATH does not exist: {env_override}"
        )

    system = platform.system()

    if system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(
                r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
            ),
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
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
        if os.path.isfile(path):
            return path

    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        found = shutil.which(name)

        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium was not found. Install Chrome or set "
        "MAERSK_CHROME_PATH to its executable."
    )


def _default_data_dir() -> str:
    """Return the persistent Chrome profile directory."""

    override = os.getenv("MAERSK_CHROME_DATA_DIR")

    if override:
        return os.path.abspath(os.path.expanduser(override))

    if platform.system() == "Windows":
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "maersk_chrome_debug",
        )

    return os.path.expanduser("~/.maersk_chrome_debug")


CHROME_PATH = _find_chrome_path()
DATA_DIR = _default_data_dir()


# ============================================================
# COOKIE CONSENT
# ============================================================

def handle_maersk_cookie_consent(page) -> None:
    """Handle a visible cookie banner using its available controls."""

    overlay = page.locator("#coiOverlay")
    wrapper = page.locator("#cookie-information-template-wrapper")

    visible = False

    for locator in (wrapper, overlay):
        try:
            locator.wait_for(state="visible", timeout=1500)
            visible = True
            break
        except Exception:
            continue

    if not visible:
        print("[INFO] No visible cookie-consent overlay.")
        return

    print("[INFO] Cookie-consent overlay detected.")

    consent_root = wrapper if wrapper.count() else overlay

    # Prefer declining optional cookies. Accept-all is only a fallback.
    preferred_terms = [
        "reject all",
        "reject optional",
        "deny all",
        "only necessary",
        "necessary only",
        "strictly necessary",
        "essential only",
        "accept all cookies",
        "accept all",
    ]

    buttons = consent_root.locator(
        "button, [role='button'], input[type='button'], input[type='submit']"
    )

    visible_labels = []

    for i in range(buttons.count()):
        button = buttons.nth(i)

        try:
            if not button.is_visible() or not button.is_enabled():
                continue

            label = " ".join(
                (button.inner_text(timeout=500) or "").split()
            )

            aria = (
                button.get_attribute("aria-label") or ""
            ).strip()

            title = (
                button.get_attribute("title") or ""
            ).strip()

            value = (
                button.get_attribute("value") or ""
            ).strip()

            combined = " ".join(
                item for item in (label, aria, title, value) if item
            ).strip()

            if combined:
                visible_labels.append(combined)

        except Exception:
            continue

    selected = None

    for term in preferred_terms:
        for i in range(buttons.count()):
            button = buttons.nth(i)

            try:
                if not button.is_visible() or not button.is_enabled():
                    continue

                label = " ".join(
                    (button.inner_text(timeout=300) or "").split()
                )

                aria = (
                    button.get_attribute("aria-label") or ""
                ).strip()

                title = (
                    button.get_attribute("title") or ""
                ).strip()

                value = (
                    button.get_attribute("value") or ""
                ).strip()

                combined = " ".join(
                    item
                    for item in (label, aria, title, value)
                    if item
                ).casefold()

                if term in combined:
                    button.click(timeout=3000)
                    selected = combined
                    break

            except Exception:
                continue

        if selected:
            break

    if not selected:
        raise RuntimeError(
            "Maersk cookie banner is visible, but no recognized consent "
            f"button was found. Available buttons: {visible_labels!r}"
        )

    print(f"[INFO] Selected cookie option: {selected}")

    try:
        page.wait_for_timeout(500)
    except Exception:
        pass

    for locator in (wrapper, overlay):
        try:
            if locator.count() and locator.is_visible():
                locator.wait_for(state="hidden", timeout=3000)
        except Exception:
            pass

    # Do not click through a still-blocking overlay.
    for locator in (wrapper, overlay):
        try:
            if locator.count() and locator.is_visible():
                raise RuntimeError(
                    "Maersk cookie overlay remains visible after consent."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

    print("[INFO] Cookie-consent handling completed.")


# ============================================================
# VIRTUAL DISPLAY
# ============================================================

def _ensure_display() -> None:
    """Start Xvfb on Linux if no usable display is configured."""

    global _xvfb_process

    if platform.system() == "Windows":
        return

    force_xvfb = os.getenv(
        "MAERSK_FORCE_XVFB", ""
    ).strip().lower() in ("1", "true", "yes")

    inherited_display = os.environ.get("DISPLAY")

    if inherited_display and not force_xvfb:
        return

    if (
        _xvfb_process is not None
        and _xvfb_process.poll() is None
    ):
        os.environ["DISPLAY"] = os.getenv(
            "MAERSK_XVFB_DISPLAY", ":99"
        )
        return

    xvfb_path = shutil.which("Xvfb")

    if not xvfb_path:
        print(
            "[WARN] Xvfb is not installed. If Chrome cannot start because "
            "there is no display, install it with: "
            "sudo dnf install -y xorg-x11-server-Xvfb"
        )
        return

    display_num = os.getenv("MAERSK_XVFB_DISPLAY", ":99")

    print(f"[INFO] Starting virtual display {display_num}.")

    _xvfb_process = subprocess.Popen(
        [
            xvfb_path,
            display_num,
            "-screen",
            "0",
            "1920x1080x24",
            "-nolisten",
            "tcp",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    time.sleep(1)

    if _xvfb_process.poll() is not None:
        raise RuntimeError(
            "Xvfb exited during startup. Check whether the configured "
            "display number is already in use."
        )

    os.environ["DISPLAY"] = display_num


# ============================================================
# CHROME DEBUGGING ENDPOINT
# ============================================================

def _chrome_debug_ready() -> bool:
    """Return True when Chrome's remote debugging endpoint responds."""

    try:
        response = requests.get(
            f"{DEBUG_URL}/json/version",
            timeout=2,
        )

        return response.status_code == 200

    except requests.RequestException:
        return False


# ============================================================
# START / CONNECT TO CHROME
# ============================================================

def ensure_chrome_running():
    """
    Reuse an existing Chrome debugging session or launch Chrome.

    Returns the Popen handle if this function launched Chrome;
    otherwise returns None.
    """

    if _chrome_debug_ready():
        print(
            f"[INFO] Existing Chrome detected on port {DEBUG_PORT}."
        )
        return None

    print("[INFO] Remote Chrome not detected. Launching Chrome...")

    _ensure_display()

    os.makedirs(DATA_DIR, exist_ok=True)

    if platform.system() != "Windows":
        popen_kwargs = {"start_new_session": True}
    else:
        popen_kwargs = {}

    command = [
        CHROME_PATH,
        f"--remote-debugging-port={DEBUG_PORT}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--start-minimized",
    ]

    chrome_process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **popen_kwargs,
    )

    deadline = time.monotonic() + CHROME_STARTUP_TIMEOUT

    while time.monotonic() < deadline:
        if _chrome_debug_ready():
            print("[INFO] Chrome launched and ready.")
            return chrome_process

        if chrome_process.poll() is not None:
            raise RuntimeError(
                "Chrome exited before its debugging endpoint became ready. "
                "Check the Chrome path, profile lock, display configuration, "
                "and server permissions."
            )

        time.sleep(0.5)

    # The endpoint never became ready. Clean up the process we started.
    close_chrome(chrome_process)

    raise RuntimeError(
        f"Chrome did not become ready within "
        f"{CHROME_STARTUP_TIMEOUT} seconds on port {DEBUG_PORT}."
    )


# ============================================================
# CLOSE CHROME
# ============================================================

def close_chrome(chrome_process) -> None:
    """Close only the Chrome process launched by this script."""

    if chrome_process is None:
        print("[INFO] Chrome was pre-existing; leaving it running.")
        return

    if chrome_process.poll() is not None:
        return

    print("[INFO] Closing Chrome launched by this script...")

    try:
        if platform.system() == "Windows":
            subprocess.run(
                [
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(chrome_process.pid),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        else:
            os.killpg(
                os.getpgid(chrome_process.pid),
                signal.SIGTERM,
            )

            try:
                chrome_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(
                    os.getpgid(chrome_process.pid),
                    signal.SIGKILL,
                )

        try:
            chrome_process.wait(timeout=5)
        except Exception:
            pass

        print("[INFO] Chrome process cleanup completed.")

    except ProcessLookupError:
        pass

    except Exception as exc:
        print(f"[WARN] Chrome cleanup issue: {exc}")

        try:
            chrome_process.kill()
        except Exception:
            pass


# ============================================================
# DIAGNOSTICS
# ============================================================

def _save_diagnostics(page) -> None:
    """Save the current rendered page when tracking results are missing."""

    try:
        page.screenshot(
            path="maersk_timeout.png",
            full_page=True,
            timeout=10000,
        )

        with open(
            "maersk_timeout.html",
            "w",
            encoding="utf-8",
        ) as html_file:
            html_file.write(page.content())

        print(
            "[INFO] Saved maersk_timeout.png and maersk_timeout.html."
        )

    except Exception as exc:
        print(f"[WARN] Could not save diagnostics: {exc}")


def _page_body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


# ============================================================
# RESULT DETECTION
# ============================================================

def _find_result_container(page, requested_container_number: str):
    """
    Wait for a tracking result.

    Prefer a result matching the requested container number. This helps
    prevent parsing stale information from a previous lookup.
    """

    selectors = [
        # New "ocean design" layout.
        '[data-test="container-design-accordion-item"]',
        'div[data-test="container"]',
        '[data-test="container-details"]',
        '[data-test^="transport-plan-item-"]',
        '[data-testid*="container"]',
        '[data-testid*="tracking"]',
    ]

    deadline = time.monotonic() + RESULT_WAIT_SECONDS

    while time.monotonic() < deadline:
        for selector in selectors:
            try:
                candidates = page.locator(selector)
                count = candidates.count()

                for index in range(min(count, 20)):
                    candidate = candidates.nth(index)

                    if not candidate.is_visible():
                        continue

                    # Prefer the legacy container card when present.
                    if selector == 'div[data-test="container"]':
                        card = candidate
                    else:
                        card = candidate

                        try:
                            parent = candidate.locator(
                                'xpath=ancestor::div[@data-test="container"][1]'
                            )

                            if parent.count() and parent.first.is_visible():
                                card = parent.first
                        except Exception:
                            pass

                    try:
                        card_text = card.inner_text(timeout=1000)
                    except Exception:
                        card_text = ""

                    normalized_text = " ".join(
                        card_text.upper().split()
                    )

                    # If the page displays the requested number, prefer
                    # that card rather than an old result.
                    if requested_container_number.upper() in normalized_text:
                        return card, selector

                    # A card with actual transport events can be useful
                    # even when its container number is rendered elsewhere.
                    try:
                        if card.locator(
                            '[data-test^="transport-plan-item-"]'
                        ).count():
                            return card, selector
                    except Exception:
                        pass

            except Exception:
                continue

        try:
            page.wait_for_timeout(500)
        except Exception:
            break

    return None, None


# ============================================================
# PARSING HELPERS
# ============================================================

def _text_or_empty(locator, timeout=1500) -> str:
    try:
        return " ".join(
            locator.inner_text(timeout=timeout).split()
        ).strip()
    except Exception:
        return ""


def _first_text(root, selectors) -> str:
    for selector in selectors:
        try:
            locator = root.locator(selector).first

            if locator.count():
                value = _text_or_empty(locator)

                if value:
                    return value

        except Exception:
            continue

    return ""


def _parse_container_details(result_container, requested_number):
    # New layout: separate id / type elements.
    new_number = _first_text(
        result_container, ['[data-test="accordion-container-id"]']
    )
    new_type = _first_text(
        result_container, ['[data-test="accordion-container-type"]']
    )

    if new_number or new_type:
        return new_number or requested_number, new_type

    detail_text = _first_text(
        result_container,
        [
            '[data-test="container-details"]',
            '[data-testid*="container-details"]',
        ],
    )

    container_number = requested_number
    container_type = ""

    if detail_text:
        parts = [
            part.strip()
            for part in detail_text.split("|")
            if part.strip()
        ]

        if parts:
            # Do not replace the requested number with an unrelated
            # string if the page's details layout changes.
            if parts[0]:
                container_number = parts[0]

        if len(parts) >= 2:
            container_type = parts[1]

    # Fall back to searching the result card if the details format changes.
    if not container_type:
        try:
            card_text = result_container.inner_text(timeout=1500)

            for line in card_text.splitlines():
                line = line.strip()

                if "|" in line:
                    parts = [p.strip() for p in line.split("|")]

                    if len(parts) >= 2:
                        if requested_number.upper() in parts[0].upper():
                            container_number = parts[0] or requested_number
                            container_type = parts[1]
                            break

        except Exception:
            pass

    return container_number, container_type


def _parse_eta(result_container) -> str:
    # New layout: the arrival date lives in the notification chip when it
    # has changed; otherwise fall back to the last listed event date.
    new_eta = _first_text(
        result_container,
        [
            '[data-test="accordion-arrival-date-update"]',
            '[data-test="accordion-arrival-date"]',
        ],
    )

    if new_eta:
        return new_eta

    if result_container.locator(
        '[data-test="container-event-list"]'
    ).count():
        try:
            return _text_or_empty(
                result_container.locator(
                    '[data-test="event-date"]'
                ).last
            )
        except Exception:
            return ""

    eta_text = _first_text(
        result_container,
        [
            '[data-test="container-eta"]',
            '[data-testid*="container-eta"]',
            '[data-testid*="eta"]',
        ],
    )

    if not eta_text:
        return ""

    lines = [
        line.strip()
        for line in eta_text.splitlines()
        if line.strip()
    ]

    # Keep the previous scraper's convention: the last line is usually
    # the ETA value. If the element has only one line, use that line.
    return lines[-1] if lines else ""


def _parse_events_new_layout(result_container) -> list:
    """Parse the new layout: location headers followed by event rows."""

    # Completed (past) events are collapsed behind a toggle.
    try:
        toggle = result_container.locator(
            '[data-test="completed-events-toggle"]'
        ).first

        if toggle.count() and toggle.is_visible():
            toggle.click(timeout=3000)
            result_container.page.wait_for_timeout(800)
    except Exception:
        pass

    rows = result_container.evaluate(
        """el => {
            const out = [];
            let location = "";
            const nodes = el.querySelectorAll(
                '[data-test="event-location-city"],' +
                '[data-test="event-name"],' +
                '[data-test="event-date"]'
            );
            const clean = t => (t || "").replace(/\\s+/g, " ").trim();
            for (const n of nodes) {
                const kind = n.getAttribute("data-test");
                if (kind === "event-location-city") {
                    const city = clean(n.querySelector("p")?.textContent);
                    const term = clean(
                        n.querySelector('[data-test="event-location-terminal"]')
                            ?.textContent
                    );
                    location = !term || !city || term.startsWith(city)
                        ? (term || city || clean(n.textContent))
                        : city + " - " + term;
                } else if (kind === "event-name") {
                    const clone = n.cloneNode(true);
                    clone.querySelectorAll("mc-tag").forEach(t => t.remove());
                    out.push({
                        location: location,
                        milestone: clean(clone.textContent),
                        date: ""
                    });
                } else if (out.length && !out[out.length - 1].date) {
                    out[out.length - 1].date = clean(n.textContent);
                }
            }
            return out;
        }"""
    )

    events = []

    for row in rows:
        events.append({
            "location": row.get("location", ""),
            "milestone": row.get("milestone", ""),
            "date": row.get("date", ""),
        })

    print(f"[INFO] Parsing {len(events)} tracking events...")

    for index, event in enumerate(events, start=1):
        print(
            f"[INFO] Event {index}: {event['date']} | "
            f"{event['location']} | {event['milestone']}"
        )

    return events


def _parse_events(result_container) -> list:
    if result_container.locator(
        '[data-test="container-event-list"], '
        '[data-test="completed-events-toggle"]'
    ).count():
        return _parse_events_new_layout(result_container)

    events = []

    event_items = result_container.locator(
        '[data-test^="transport-plan-item-"]'
    )

    count = event_items.count()

    print(f"[INFO] Parsing {count} tracking events...")

    for index in range(count):
        item = event_items.nth(index)

        location = _first_text(
            item,
            [
                '[data-test="location-name"]',
                '[data-testid*="location-name"]',
            ],
        )

        milestone = _first_text(
            item,
            [
                '[data-test="milestone"]',
                '[data-testid*="milestone"]',
                '[data-testid*="status"]',
            ],
        )

        date = _first_text(
            item,
            [
                '[data-test="milestone-date"]',
                '[data-testid*="milestone-date"]',
                'time',
            ],
        )

        event = {
            "location": location,
            "milestone": milestone,
            "date": date,
        }

        events.append(event)

        print(
            f"[INFO] Event {index + 1}: "
            f"{date} | {location} | {milestone}"
        )

    return events


# ============================================================
# MAIN PUBLIC FUNCTION
# ============================================================

def process_active_chrome_tab(container_number: str = None) -> dict:
    """
    Fetch Maersk tracking data.

    Args:
        container_number: Container number to track.

    Returns:
        Dictionary containing container details and tracking events.
    """

    if not container_number or not container_number.strip():
        raise ValueError(
            "container_number is required. "
            "Example: process_active_chrome_tab('SLZU2577558')"
        )

    chrome_process = ensure_chrome_running()

    try:
        return _track_container(container_number.strip())

    finally:
        close_chrome(chrome_process)


# ============================================================
# TRACK CONTAINER
# ============================================================

def _track_container(container_number: str = None) -> dict:
    requested_number = (container_number or "").strip()

    if not requested_number:
        raise ValueError("A container number must be supplied.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            DEBUG_URL,
            timeout=15000,
        )

        if not browser.contexts:
            raise RuntimeError(
                "Connected to Chrome, but no browser context is available."
            )

        context = browser.contexts[0]

        # Find an existing Maersk page.
        page = None

        for existing_page in context.pages:
            if "maersk.com" in existing_page.url.lower():
                page = existing_page
                break

        # Otherwise create a fresh tab.
        if page is None:
            page = context.new_page()

            page.goto(
                TRACKING_URL,
                wait_until="domcontentloaded",
                timeout=TRACKING_TIMEOUT,
            )

        print(f"[INFO] Attached to page: {page.url}")

        # Handle cookie consent before interacting with the form.
        handle_maersk_cookie_consent(page)

        container_input = page.locator(
            "#mc-input-track-input"
        )

        container_input.wait_for(
            state="visible",
            timeout=15000,
        )

        # Clear previous input and enter the requested container.
        container_input.fill(requested_number)

        print(
            f"[INFO] Entered container number: {requested_number}"
        )

        # Record the old result to help detect stale content.
        old_result_text = ""

        try:
            old_result_text = _text_or_empty(
                page.locator(
                    'div[data-test="container"]'
                ).first,
                timeout=1000,
            )
        except Exception:
            pass

        # Submit the tracking form.
        track_button = page.locator(
            'button[aria-label="Track"]'
        )

        track_button.wait_for(
            state="visible",
            timeout=10000,
        )

        track_button.click(timeout=10000)

        print("[INFO] Clicked Track. Waiting for Maersk response...")

        # Wait briefly for client-side navigation or API-driven rendering.
        try:
            page.wait_for_load_state(
                "domcontentloaded",
                timeout=5000,
            )
        except Exception:
            pass

        # Wait for a fresh result or an explicit no-result response.
        result_container = None
        result_selector = None

        deadline = time.monotonic() + RESULT_WAIT_SECONDS
        last_body_text = ""

        while time.monotonic() < deadline:
            last_body_text = _page_body_text(page)
            lowered = last_body_text.casefold()

            # Detect an explicit verification/access-block page.
            if any(
                phrase in lowered
                for phrase in (
                    "verify you are human",
                    "access denied",
                    "unusual traffic",
                    "captcha",
                )
            ):
                _save_diagnostics(page)

                raise RuntimeError(
                    "Maersk displayed a verification or access-block page. "
                    "The tracking result is unavailable until the page is "
                    "resolved through the site's supported verification flow."
                )

            # Detect a no-results message.
            if any(
                phrase in lowered
                for phrase in (
                    "no results found",
                    "no tracking results",
                    "couldn't find",
                    "could not find",
                    "container not found",
                )
            ):
                # Continue checking briefly in case the page is showing
                # an old message while a new request is being processed.
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    pass

                refreshed_body = _page_body_text(page).casefold()

                if any(
                    phrase in refreshed_body
                    for phrase in (
                        "no results found",
                        "no tracking results",
                        "couldn't find",
                        "could not find",
                        "container not found",
                    )
                ):
                    raise ContainerNotFoundError(
                        "No tracking data found for container "
                        f"{requested_number} on Maersk."
                    )

            result_container, result_selector = _find_result_container(
                page,
                requested_number,
            )

            if result_container is not None:
                current_result_text = _text_or_empty(
                    result_container,
                    timeout=1000,
                )

                # If a previous result existed and the current result is
                # identical, allow additional time for it to update.
                if (
                    old_result_text
                    and current_result_text == old_result_text
                    and requested_number.upper()
                    not in current_result_text.upper()
                ):
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

                    continue

                break

            try:
                page.wait_for_timeout(500)
            except Exception:
                break

        if result_container is None:
            _save_diagnostics(page)

            lowered = last_body_text.casefold()

            if any(
                phrase in lowered
                for phrase in (
                    "no results found",
                    "no tracking results",
                    "couldn't find",
                    "could not find",
                    "container not found",
                )
            ):
                raise ContainerNotFoundError(
                    f"No tracking data found for {requested_number}."
                )

            print(
                f"[ERROR] Maersk result not detected. "
                f"URL: {page.url}; "
                f"body: {last_body_text[:1200]!r}"
            )

            raise RuntimeError(
                "Maersk did not display a recognizable tracking result. "
                "Inspect maersk_timeout.png and maersk_timeout.html. "
                "The site's selectors or response layout may have changed."
            )

        print(
            f"[INFO] Tracking result found using selector: "
            f"{result_selector}"
        )

        # ====================================================
        # CONTAINER DETAILS
        # ====================================================

        parsed_number, container_type = _parse_container_details(
            result_container,
            requested_number,
        )

        # Preserve the requested value if the page omitted its number.
        container_number_value = parsed_number or requested_number

        # ====================================================
        # ETA
        # ====================================================

        eta = _parse_eta(result_container)

        # ====================================================
        # EVENTS
        # ====================================================

        events = _parse_events(result_container)

        # ====================================================
        # SUMMARY
        # ====================================================

        summary_fields = {
            "container_number": container_number_value,
            "container_type": container_type,
            "eta": eta,
            "total_events": len(events),
        }

        print("\n========== TRACKING SUMMARY ==========")
        print(f"Container No  : {container_number_value}")
        print(f"Container Type: {container_type}")
        print(f"ETA           : {eta}")
        print(f"Total Events  : {len(events)}")
        print("======================================")

        for index, event in enumerate(events, start=1):
            print(
                f"[{index}] "
                f"{event['date']} | "
                f"{event['location']} -> "
                f"{event['milestone']}"
            )

        # ====================================================
        # STRUCTURED RESPONSE
        # ====================================================

        result = {
            "line": "maersk",
            "container_number": container_number_value,
            "summary_fields": summary_fields,
            "event_columns": EVENT_COLUMNS,
            "events": events,
            "source_url": TRACKING_URL,
        }

        # Do not call browser.close() here. Closing a browser connected
        # through CDP may terminate the remote Chrome instance.
        # sync_playwright handles this script's Playwright connection.

        return result


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":
    test_container = (
        os.getenv("MAERSK_CONTAINER_NUMBER", "").strip()
    )

    if not test_container:
        test_container = input(
            "Enter Maersk container number: "
        ).strip()

    try:
        result = process_active_chrome_tab(test_container)

        print("\n========== RETURN VALUE ==========")

        import json

        print(
            json.dumps(
                result,
                indent=4,
                ensure_ascii=False,
            )
        )

    except ContainerNotFoundError as exc:
        print(f"[NOT FOUND] {exc}")

    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        raise

