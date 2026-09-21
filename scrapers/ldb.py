"""Fetches container tracking data from India's Logistics Data Bank (LDB),
operated by NICDC Logistics Data Services (formerly DMICDC). Covers container
movement through Indian ports, ICDs, CFSs and rail/road legs.

Unlike Hapag-Lloyd, LDB exposes a plain, unauthenticated JSON endpoint (the
same one their own tracking page at https://ldb.co.in/ldb/containersearch
calls), so no browser automation is needed here — just an HTTP GET request.
"""

import time

import requests

from .container_number import normalize
from .errors import ContainerNotFoundError

API_URL = "https://ldb.co.in/api/ldb/container/search"
SEARCH_TYPE_SINGLE = 39  # matches the "Single" search mode on the LDB site
HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _split_timestamp(ts, tz_abbr=""):
    if not ts:
        return "-", "-"
    date_part, _, time_part = ts.partition("T")
    time_part = time_part[:8]  # HH:MM:SS
    if tz_abbr:
        time_part = f"{time_part} {tz_abbr}"
    return date_part or "-", time_part or "-"


def track_container(raw_container_number: str) -> dict:
    container_number = normalize(raw_container_number)

    try:
        response = requests.get(
            API_URL,
            params={"cntrNo": container_number, "searchType": SEARCH_TYPE_SINGLE},
            headers=HEADERS,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ContainerNotFoundError(
            "Couldn't reach LDB right now. Please try again in a moment."
        ) from exc

    if response.status_code == 404:
        raise ContainerNotFoundError("No tracking data found for this container number on LDB.")

    if response.status_code != 200:
        raise ContainerNotFoundError(
            f"LDB returned an unexpected response (HTTP {response.status_code})."
        )

    data = response.json()
    detail = data.get("object")
    if not isinstance(detail, dict) or not (detail.get("cntrDetail") or detail.get("trackLog")):
        detail = None
    if detail is None:
        raise ContainerNotFoundError("No tracking data found for this container number on LDB.")

    cntr_detail = detail.get("cntrDetail") or {}
    track_log = detail.get("trackLog") or []
    last_event = detail.get("lastEvent")

    summary_fields = []
    if cntr_detail.get("size"):
        summary_fields.append({"label": "Size", "value": cntr_detail["size"]})
    if cntr_detail.get("containerType"):
        summary_fields.append({"label": "Type", "value": cntr_detail["containerType"]})
    if cntr_detail.get("isoCode"):
        summary_fields.append({"label": "ISO Code", "value": cntr_detail["isoCode"]})
    if last_event:
        date_str, time_str = _split_timestamp(
            last_event.get("timestampTimezone"), last_event.get("timeZoneAbvr", "")
        )
        latest = f"{last_event.get('eventName', '-')} at {last_event.get('currentLocation', '-')}"
        summary_fields.append({"label": "Latest Event", "value": latest})
        summary_fields.append({"label": "Latest Event Time", "value": f"{date_str} {time_str}"})

    events = []
    event_coords = []
    for ev in track_log:
        date_str, time_str = _split_timestamp(ev.get("timestampTimezone"), ev.get("timeZoneAbvr", ""))
        events.append([
            ev.get("eventName", "-"),
            ev.get("currentLocation", "-"),
            date_str,
            time_str,
            ev.get("transportmode", "-"),
        ])
        lat, lon = ev.get("latitude"), ev.get("longitude")
        event_coords.append([lat, lon] if lat is not None and lon is not None else None)

    return {
        "line": "ldb",
        "container_number": container_number,
        "summary_fields": summary_fields,
        "event_columns": ["Event", "Location", "Date", "Time", "Transport"],
        "events": events,
        "event_coords": event_coords,
        "source_url": f"https://ldb.co.in/ldb/containersearch/{SEARCH_TYPE_SINGLE}/{container_number}/{int(time.time() * 1000)}",
    }
