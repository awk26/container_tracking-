"""Scrapes Hapag-Lloyd's public container tracking page (no login required).

The tracking page is a client-rendered SPA, so we drive a real headless
browser: open the page, type the container number into the search box the
same way a visitor would, then read the rendered result table and the
per-event timeline once the summary row is expanded.
"""

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from .container_number import normalize
from .errors import ContainerNotFoundError, InvalidContainerNumberError

TRACKING_URL = "https://www.hapag-lloyd.com/solutions/tracking/#/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_REMOVE_OVERLAYS_JS = """() => {
    document.getElementById('onetrust-consent-sdk')?.remove();
    document.querySelectorAll('[id^="q-portal--dialog"]').forEach(el => el.remove());
}"""

_EXTRACT_SUMMARY_JS = """() => {
    const row = document.querySelector('table.q-table tbody tr');
    if (!row) return null;
    const cells = row.querySelectorAll('td');
    if (cells.length < 6) return null;
    return {
        container_no: cells[0].innerText.trim(),
        tare_kg: cells[1].innerText.trim(),
        payload_kg: cells[2].innerText.trim(),
        type: cells[3].innerText.trim(),
        latest_event: cells[4].innerText.trim(),
        planned: cells[5].innerText.trim(),
    };
}"""

_EXTRACT_EVENTS_JS = """() => {
    const rows = document.querySelectorAll('.hal-event-tracking .hal-event__inline');
    return Array.from(rows).map(row => {
        const cols = Array.from(row.querySelectorAll(':scope > .hal-event__col'))
            .map(col => col.innerText.trim());
        return {
            event: cols[0] || '',
            location: cols[1] || '',
            date: cols[2] || '',
            time: cols[3] || '',
            transport: cols[4] || '',
            voyage: cols[5] || '',
        };
    });
}"""


def track_container(raw_container_number: str) -> dict:
    container_number = normalize(raw_container_number)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(TRACKING_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector('[data-cy="tracking-search-input"]', timeout=20000)
            except PlaywrightTimeoutError:
                if "Verify you are human" in page.content():
                    raise ContainerNotFoundError(
                        "Hapag-Lloyd is showing a security check (Cloudflare) instead of the "
                        "tracking page right now, so this lookup can't complete automatically. "
                        "Please try again later or look it up directly at hapag-lloyd.com."
                    )
                raise ContainerNotFoundError(
                    "Hapag-Lloyd's tracking page didn't load in time. Please try again."
                )

            # The cookie-consent banner and an onboarding dialog can inject in
            # asynchronously after the search box appears, so remove them
            # a couple of times with a short pause rather than once.
            page.evaluate(_REMOVE_OVERLAYS_JS)
            page.wait_for_timeout(1200)
            page.evaluate(_REMOVE_OVERLAYS_JS)
            page.keyboard.press("Escape")

            search_box = page.locator('[data-cy="tracking-search-input"]')
            search_box.click(force=True)
            search_box.fill(container_number)
            search_box.press("Enter")

            # The site does its own client-side format validation on submit.
            invalid_message = page.locator("text=Number is not valid.")
            result_row = page.locator("table.q-table tbody tr")

            try:
                page.wait_for_function(
                    """() => {
                        const invalid = Array.from(document.querySelectorAll('div,span'))
                            .some(el => el.textContent.trim() === 'Number is not valid.' && el.offsetParent !== null);
                        const hasRow = !!document.querySelector('table.q-table tbody tr');
                        const noData = Array.from(document.querySelectorAll('div'))
                            .some(el => el.textContent.trim() === 'No data available');
                        return invalid || hasRow || noData;
                    }""",
                    timeout=20000,
                )
            except PlaywrightTimeoutError:
                raise ContainerNotFoundError(
                    "Hapag-Lloyd did not return a result in time. Try again in a moment."
                )

            if invalid_message.count() > 0 and invalid_message.first.is_visible():
                raise InvalidContainerNumberError("Hapag-Lloyd rejected this container number format.")

            if result_row.count() == 0:
                raise ContainerNotFoundError("No tracking data found for this container number.")

            # Expand the row to load the full event timeline.
            page.evaluate(_REMOVE_OVERLAYS_JS)
            result_row.first.click(force=True)
            page.wait_for_selector(".hal-event-tracking .hal-event__inline", timeout=10000)
            page.wait_for_timeout(300)  # let the last event row finish rendering

            summary = page.evaluate(_EXTRACT_SUMMARY_JS)
            raw_events = page.evaluate(_EXTRACT_EVENTS_JS)

            if not summary:
                raise ContainerNotFoundError("No tracking data found for this container number.")

            summary_fields = [
                {"label": "Tare (kg)", "value": summary["tare_kg"]},
                {"label": "Payload (kg)", "value": summary["payload_kg"]},
                {"label": "Type", "value": summary["type"]},
                {"label": "Latest Event", "value": summary["latest_event"]},
                {"label": "Planned", "value": summary["planned"]},
            ]
            events = [
                [ev["event"], ev["location"], ev["date"], ev["time"], ev["transport"], ev["voyage"]]
                for ev in raw_events
            ]

            return {
                "line": "hapag",
                "container_number": container_number,
                "summary_fields": summary_fields,
                "event_columns": ["Event", "Location", "Date", "Time", "Transport", "Voyage No."],
                "events": events,
                "source_url": f"https://www.hapag-lloyd.com/solutions/tracking/#/{container_number}",
            }
        finally:
            browser.close()
