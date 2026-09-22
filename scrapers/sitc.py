"""Fetches container tracking data from SITC's public cargo-tracking API.

SITC's e-business portal (ebusiness.sitcline.com) has a "Cargo Track" page
that is reachable without logging in. It calls a plain JSON endpoint that
needs no auth or session cookies - just a container number - so no browser
automation is needed here, only an HTTP POST request.
"""

import requests

from .container_number import normalize
from .errors import ContainerNotFoundError

API_URL = "https://ebusiness.sitcline.com/api/equery/cmContainerHistory/movementSearch"
TRACKING_PAGE_URL = "https://ebusiness.sitcline.com/#/topMenu/cargoTrack"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://ebusiness.sitcline.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

EVENT_COLUMNS = ["Date", "Location", "Description", "Vessel / Voyage"]


def track_container(raw_container_number: str) -> dict:
    container_number = normalize(raw_container_number)

    try:
        response = requests.post(
            API_URL,
            params={"containerNo": container_number},
            headers=HEADERS,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ContainerNotFoundError(
            "Couldn't reach SITC right now. Please try again in a moment."
        ) from exc

    if response.status_code != 200:
        raise ContainerNotFoundError(
            f"SITC returned an unexpected response (HTTP {response.status_code})."
        )

    body = response.json()
    records = ((body or {}).get("data") or {}).get("list") or []
    if not body.get("success") or not records:
        raise ContainerNotFoundError("No tracking data found for this container number on SITC.")

    events = []
    for ev in records:
        vessel_voyage = " / ".join(v for v in (ev.get("vesselCode"), ev.get("voyageNo")) if v)
        events.append([
            ev.get("eventDate") or "-",
            ev.get("eventPort") or "-",
            ev.get("movementCode") or "-",
            vessel_voyage or "-",
        ])

    latest = records[0]
    summary_fields = [
        {"label": "Latest Event", "value": latest.get("movementCode") or "-"},
        {"label": "Latest Location", "value": latest.get("eventPort") or "-"},
        {"label": "Latest Event Time", "value": latest.get("eventDate") or "-"},
    ]

    return {
        "line": "sitc",
        "container_number": container_number,
        "summary_fields": summary_fields,
        "event_columns": EVENT_COLUMNS,
        "events": events,
        "source_url": TRACKING_PAGE_URL,
    }
