"""Fetches container tracking data from Evergreen's public ShipmentLink
tracking system (https://ct.shipmentlink.com/servlet/TDB1_CargoTracking.do).

This is a classic server-rendered form (no SPA, no bot protection observed),
so a plain HTTP POST reproducing what the page's own `frmSubmit()` JS does
is enough — no browser automation needed.

Note: a container-number lookup here only returns the container's latest
move, not its full movement history (Evergreen requires a Bill of Lading
number for that).
"""

import requests
from bs4 import BeautifulSoup

from .container_number import normalize
from .errors import ContainerNotFoundError

TRACKING_URL = "https://ct.shipmentlink.com/servlet/TDB1_CargoTracking.do"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded",
}

_NOT_FOUND_TEXT = "No information on Container No"
EVENT_COLUMNS = ["Container No.", "Size/Type", "Date", "Container Moves", "Location", "Vessel Voyage", "Method", "VGM"]


def track_container(raw_container_number: str) -> dict:
    container_number = normalize(raw_container_number)

    try:
        resp = requests.post(
            TRACKING_URL,
            data={"BL": "", "CNTR": container_number, "bkno": "", "TYPE": "CNTR"},
            headers=HEADERS,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ContainerNotFoundError(
            "Couldn't reach Evergreen (ShipmentLink) right now. Please try again in a moment."
        ) from exc

    resp.raise_for_status()

    if _NOT_FOUND_TEXT in resp.text:
        raise ContainerNotFoundError("No tracking data found for this container number on Evergreen.")

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="ec-table")
    if table is None:
        raise ContainerNotFoundError("No tracking data found for this container number on Evergreen.")

    rows = []
    for tr in table.find_all("tr")[1:]:  # skip header row
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) == len(EVENT_COLUMNS):
            rows.append(cells)

    if not rows:
        raise ContainerNotFoundError("No tracking data found for this container number on Evergreen.")

    summary_fields = [
        {"label": label, "value": value}
        for label, value in zip(EVENT_COLUMNS[1:], rows[0][1:])
        if value
    ]

    return {
        "line": "evergreen",
        "container_number": container_number,
        "summary_fields": summary_fields,
        "event_columns": EVENT_COLUMNS,
        "events": rows,
        "source_url": TRACKING_URL,
    }
