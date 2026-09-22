"""Fetches container tracking data from SCI (Shipping Corporation of India)
via the public form at shipindia.com/frontcontroller/track_trace.

Plain server-rendered HTML behind a CSRF-token form: fetch the tracking page
once to grab a fresh csrf_scimain_tok, then POST the container number - no
browser automation needed.
"""

import requests
from bs4 import BeautifulSoup

from .container_number import normalize
from .errors import ContainerNotFoundError

TRACKING_PAGE_URL = "https://www.shipindia.com/frontcontroller/track_trace"
SUBMIT_URL = "https://www.shipindia.com/frontcontroller/track_trace1"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

EVENT_COLUMNS = ["Location", "Loc Type", "Description", "Date", "Vessel", "Voyage", "ATD"]


def track_container(raw_container_number: str) -> dict:
    container_number = normalize(raw_container_number)

    session = requests.Session()
    try:
        page = session.get(TRACKING_PAGE_URL, headers=HEADERS, timeout=15)
        page.raise_for_status()
        token_input = BeautifulSoup(page.text, "html.parser").find(
            "input", {"name": "csrf_scimain_tok"}
        )
        response = session.post(
            SUBMIT_URL,
            headers=HEADERS,
            data={
                "csrf_scimain_tok": token_input.get("value") if token_input else "",
                "type": "CT",
                "container_no": container_number,
                "go": "Track",
            },
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ContainerNotFoundError(
            "Couldn't reach SCI right now. Please try again in a moment."
        ) from exc

    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ContainerNotFoundError("No tracking data found for this container number on SCI.")

    # The results table has the event rows in the first <tbody>, followed by
    # a second, separate <thead>/row further down (POD / POD_ETA_ATA) that is
    # a one-row summary, not an event - handled separately below.
    events = []
    for row in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cells) == len(EVENT_COLUMNS) and any(cells):
            events.append(cells)

    if not events:
        raise ContainerNotFoundError("No tracking data found for this container number on SCI.")

    summary_fields = []
    latest = events[0]
    summary_fields.append({"label": "Latest Event", "value": latest[2] or "-"})
    summary_fields.append({"label": "Latest Location", "value": latest[0] or "-"})
    summary_fields.append({"label": "Latest Event Date", "value": latest[3] or "-"})

    # Malformed markup: the POD/POD_ETA_ATA values sit as bare <td>s directly
    # inside the same <thead> as the "POD" <th>, not inside their own <tr>.
    pod_header = table.find("th", string=lambda s: s and s.strip() == "POD")
    if pod_header:
        pod_thead = pod_header.find_parent("thead")
        pod_cells = [td.get_text(" ", strip=True) for td in pod_thead.find_all("td")] if pod_thead else []
        if pod_cells and pod_cells[0]:
            summary_fields.append({"label": "Port of Discharge", "value": pod_cells[0]})
        if len(pod_cells) > 1 and pod_cells[1]:
            summary_fields.append({"label": "POD ETA / ATA", "value": pod_cells[1]})

    return {
        "line": "sci",
        "container_number": container_number,
        "summary_fields": summary_fields,
        "event_columns": EVENT_COLUMNS,
        "events": events,
        "source_url": TRACKING_PAGE_URL,
    }
