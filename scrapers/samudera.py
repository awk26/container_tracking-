"""Fetches shipment tracking data from Samudera Shipping Line's public
Container Tracking System (CTS) at ssl-cts.samudera.id.

Unlike the other carriers, Samudera's public tool is BL-driven: you look up
a Bill of Lading number and get back the shared vessel/voyage and port
ETD/ETA details for that BL, plus the list of containers riding under it -
there's no per-container terminal-move event log like MSC/PIL/SITC/SCI
expose, just the voyage-level facts. This calls that JSON endpoint directly
(no browser automation needed) and turns the handful of port/date facts
into the same events-table shape the rest of the app expects.

Note: the marketing site at samudera.id is behind Incapsula bot protection
and is deliberately not scraped. This CTS subdomain is a separate, public,
unprotected system - reachable over HTTPS only (the plain-HTTP port serves
an Incapsula/nginx error, not the app).
"""

import requests

from .container_number import normalize
from .errors import ContainerNotFoundError

API_URL = "https://ssl-cts.samudera.id:3000/api/byBlNumber"
TRACKING_PAGE_URL = "https://ssl-cts.samudera.id:3000/"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

EVENT_COLUMNS = ["Date", "Location", "Description", "Vessel / Voyage"]


def track_container(bl_number: str, raw_container_number: str = None) -> dict:
    bl_number = normalize(bl_number)
    container_number = normalize(raw_container_number) if raw_container_number else None

    try:
        response = requests.get(
            API_URL, params={"byBlNumber": bl_number}, headers=HEADERS, timeout=15
        )
    except requests.RequestException as exc:
        raise ContainerNotFoundError(
            "Couldn't reach Samudera right now. Please try again in a moment."
        ) from exc

    if response.status_code != 200:
        raise ContainerNotFoundError("No tracking data found for this BL number on Samudera.")

    body = response.json()
    data = (body or {}).get("data") or {}
    header = data.get("header") or {}
    details = data.get("details") or []

    if not body.get("ok") or not header or not details:
        raise ContainerNotFoundError("No tracking data found for this BL number on Samudera.")

    matched_container = None
    if container_number:
        for d in details:
            if (d.get("containerNumber") or "").upper() == container_number:
                matched_container = d
                break
        if matched_container is None:
            raise ContainerNotFoundError(
                f"Container {container_number} was not found under BL {bl_number} on Samudera."
            )

    vessel_voyage = " / ".join(
        v for v in (header.get("vesselName"), header.get("voyageNo")) if v
    )

    summary_fields = [
        {"label": "BL Number", "value": header.get("blNumber") or bl_number},
        {"label": "Vessel / Voyage", "value": vessel_voyage or "-"},
        {"label": "Port of Load", "value": header.get("portOfLoad") or "-"},
        {"label": "Port of Discharge", "value": header.get("portOfDischarge") or "-"},
        {"label": "ETD Port of Load", "value": header.get("polETD") or "-"},
        {"label": "ETA Port of Discharge", "value": header.get("podETA") or "-"},
    ]


    if matched_container:
        size = matched_container.get("containerSize") or ""
        ctype = matched_container.get("containerType") or ""
        summary_fields.append({"label": "Container Type", "value": f"{size}' {ctype}".strip()})
    else:
        all_numbers = ", ".join(d.get("containerNumber", "-") for d in details)
        summary_fields.append({"label": "Containers in this BL", "value": all_numbers or "-"})

    # No per-container move history is exposed publicly - only voyage-level
    # port/date facts - so the "events" here are those facts turned into the
    # same table shape the rest of the app renders (map/timeline/charts).
    events = []
    if header.get("polETD"):
        events.append([
            header["polETD"], header.get("portOfLoad") or "-",
            "Departed Port of Load", vessel_voyage or "-",
        ])
    if header.get("portOfTransshipment") and (header.get("potETA") or header.get("potETD")):
        events.append([
            header.get("potETA") or header.get("potETD"), header["portOfTransshipment"],
            "Transshipment", vessel_voyage or "-",
        ])
    if header.get("podETA"):
        events.append([
            header["podETA"], header.get("portOfDischarge") or "-",
            "Arrival at Port of Discharge (ETA)", vessel_voyage or "-",
        ])
    if header.get("podETD"):
        events.append([
            header["podETD"], header.get("portOfDischarge") or "-",
            "Departure from Port of Discharge", vessel_voyage or "-",
        ])

    if not events:
        raise ContainerNotFoundError("No tracking data found for this BL number on Samudera.")

    display_number = matched_container["containerNumber"] if matched_container else f"BL {bl_number}"

    return {
        "line": "samudera",
        "container_number": display_number,
        "summary_fields": summary_fields,
        "event_columns": EVENT_COLUMNS,
        "events": events,
        "source_url": TRACKING_PAGE_URL,
    }
