"""Fetches container tracking data from MSC's public tracking page.

MSC's site is protected by Akamai Bot Manager, which blocks the page outright
("Access Denied") when driven by Playwright's bundled "Chrome for Testing"
build. Launching the machine's actual installed Google Chrome instead (via
Playwright's `channel="chrome"`) gets through with no challenge at all - so
this drives that. Once the tracking page has loaded once (letting Akamai's
JS sensor run and approve the session), we call MSC's own JSON API directly
via `fetch()` from inside that page - the same endpoint their search box
calls - instead of clicking through the UI and scraping rendered HTML.

Requires a real Google Chrome installation on the machine running this app.
"""

from playwright.sync_api import sync_playwright

from .container_number import normalize
from .errors import ContainerNotFoundError

TRACKING_PAGE_URL = "https://www.msc.com/en/track-a-shipment"
API_PATH = "/api/feature/tools/TrackingInfo"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

EVENT_COLUMNS = ["Date", "Location", "Description", "Vessel / Voyage"]

_FETCH_TRACKING_JS = """async (containerNumber) => {
    const resp = await fetch('%s', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify({ trackingNumber: containerNumber, trackingMode: '0' }),
    });
    let body = null;
    try { body = await resp.json(); } catch (e) { body = null; }
    return { status: resp.status, body };
}""" % API_PATH


def track_container(raw_container_number: str) -> dict:
    container_number = normalize(raw_container_number)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(TRACKING_PAGE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(6000)  # let Akamai's bot-detection JS finish and set cookies

            result = page.evaluate(_FETCH_TRACKING_JS, container_number)
        finally:
            browser.close()

    body = (result or {}).get("body") or {}
    if not body.get("IsSuccess") or not body.get("Data", {}).get("BillOfLadings"):
        raise ContainerNotFoundError("No tracking data found for this container number on MSC.")

    data = body["Data"]

    container_info = None
    bol_number = None
    for bol in data["BillOfLadings"]:
        for container in bol.get("ContainersInfo", []):
            if (container.get("ContainerNumber") or "").upper() == container_number:
                container_info = container
                bol_number = bol.get("BillOfLadingNumber")
                break
        if container_info:
            break

    if container_info is None:
        raise ContainerNotFoundError("No tracking data found for this container number on MSC.")

    summary_fields = [
        {"label": "Bill of Lading", "value": bol_number or "-"},
        {"label": "Container Type", "value": container_info.get("ContainerType") or "-"},
        {"label": "Latest Move", "value": container_info.get("LatestMove") or "-"},
        {"label": "ETA at Destination", "value": container_info.get("PodEtaDate") or "-"},
    ]

    events = []
    for ev in sorted(container_info.get("Events") or [], key=lambda e: e.get("Order", 0)):
        vessel_voyage = " / ".join(d for d in (ev.get("Detail") or []) if d)
        events.append([
            ev.get("Date") or "-",
            ev.get("Location") or "-",
            ev.get("Description") or "-",
            vessel_voyage or "-",
        ])

    return {
        "line": "msc",
        "container_number": container_number,
        "summary_fields": summary_fields,
        "event_columns": EVENT_COLUMNS,
        "events": events,
        "source_url": TRACKING_PAGE_URL,
    }
