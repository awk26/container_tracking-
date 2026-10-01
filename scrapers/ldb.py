"""Fetches container tracking data from India's Logistics Data Bank (LDB),
operated by NICDC Logistics Data Services (formerly DMICDC). Covers container
movement through Indian ports, ICDs, CFSs and rail/road legs.

Unlike Hapag-Lloyd, LDB exposes a plain, unauthenticated JSON endpoint (the
same one their own tracking page at https://ldb.co.in/ldb/containersearch
calls), so no browser automation is needed here — just an HTTP GET request.
"""

import re
import time
from datetime import datetime, timedelta, timezone

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


# LDB sends every timestamp as a UTC instant ("...T04:33:49.000+00:00") and
# names the event's local zone separately in timeZoneAbvr. Reading the clock
# digits as if they were already local made every time look ~5.5h early
# (and date-only events, stored as midnight IST, land on the previous day).
_ZONE_OFFSETS = {"IST": timedelta(hours=5, minutes=30)}


def _localize(ts, tz_abbr=""):
    """(local datetime, zone label) for one LDB timestamp, or None if unusable."""
    if not ts:
        return None

    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return None

    offset = _ZONE_OFFSETS.get(tz_abbr)
    if offset is not None:
        return moment.astimezone(timezone(offset)), tz_abbr

    # Zone we can't convert: show the UTC value honestly labelled.
    return moment.astimezone(timezone.utc), "UTC"


def _split_timestamp(ts, tz_abbr=""):
    if not ts:
        return "-", "-"

    localized = _localize(ts, tz_abbr)
    if localized is None:
        date_part, _, time_part = ts.partition("T")
        time_part = time_part[:8]  # HH:MM:SS
        if tz_abbr:
            time_part = f"{time_part} {tz_abbr}"
        return date_part or "-", time_part or "-"

    local, label = localized
    return local.strftime("%Y-%m-%d"), f"{local.strftime('%H:%M:%S')} {label}"


# ---------------------------------------------------------------------------
# Full event table
#
# Besides trackLog (13 events), the API returns trackingInfoSearchDownload:
# every event LDB knows about (15 here, including the vessel arrived/departed
# rows and the DPD/DPE notes), with the truck number, import/export type,
# terminal, expected next event and coordinates. The table is built from that
# so nothing LDB shows is left out; trackLog is only the fallback.
# ---------------------------------------------------------------------------

_COLUMNS = [
    "Event", "Location", "Terminal / Station", "Date", "Time", "Movement",
    "Transport", "Truck No.", "Vessel", "Cargo", "Time at Station",
    "Expected Next", "Avg. Lead Time", "Coordinates", "Remarks",
]
_MOVEMENT = {"I": "Import", "E": "Export", "T": "Transit"}
_CARGO = {"N": "Laden", "Y": "Empty"}


def _clean(text):
    return " ".join(str(text).split()) if text else ""


def _base_name(name):
    """'PORT OUT - E' -> 'PORT OUT' (the E/I suffix is shown as Movement)."""
    return re.sub(r"\s+-\s+[A-Za-z]$", "", name or "").strip()


def _display_datetime(ts, tz_abbr=""):
    localized = _localize(ts, tz_abbr)
    if localized is None:
        return None
    local, label = localized
    return f"{local.strftime('%d-%m-%Y %H:%M:%S')} {label}"


def _format_duration(delta):
    """'8Days10Hr.56Min.' style, matching LDB's own badges."""
    total_minutes = int(delta.total_seconds() // 60)
    days, rest = divmod(total_minutes, 1440)
    hours, minutes = divmod(rest, 60)
    text = f"{hours}Hr.{minutes}Min."
    if days:
        text = f"{days}Day{'s' if days != 1 else ''}" + text
    return text


def _station_dwells(detail):
    """How long the container sat at each station, as LDB's badges show it:
    the span between the first and last event grouped under one station in
    cntrInTransit. Keyed by (timestamp, event name) so it can be matched back
    onto the flat event rows."""
    dwells = {}
    for group in detail.get("cntrInTransit") or []:
        items = [i for i in group.get("containerGroupingList") or [] if i.get("datatyp") != "info"]

        instants = []
        for item in items:
            try:
                instants.append(datetime.fromisoformat(item.get("timestampTimezone") or ""))
            except ValueError:
                continue
        if len(instants) < 2:
            continue

        span = max(instants) - min(instants)
        if span < timedelta(minutes=1):
            continue

        text = _format_duration(span)
        for item in items:
            dwells[(item.get("timestampTimezone"), _base_name(item.get("eventName")).upper())] = text
    return dwells


def _shipping_lines(detail):
    """(eventId, cycleId) -> shipping line, from the vesselStatus* blocks."""
    lines = {}
    for key, value in detail.items():
        if key.startswith("vesselStatus") and isinstance(value, dict) and value.get("shippingline"):
            lines[(value.get("eventid"), value.get("cntrcycleid"))] = _clean(value["shippingline"])
    return lines


def _full_event_table(detail):
    rows = detail.get("trackingInfoSearchDownload") or []
    if not rows:
        return None

    dwells = _station_dwells(detail)
    shipping_lines = _shipping_lines(detail)

    def instant(row):
        try:
            return datetime.fromisoformat(row.get("timestampTimezone") or "")
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    events, coords, modes = [], [], []

    for r in sorted(rows, key=lambda r: (instant(r), r.get("serialNo") or 0), reverse=True):
        name = r.get("eventName") or "-"
        base = _base_name(name)
        tz = r.get("timeZoneAbvr", "")
        is_vessel = r.get("datatyp") == "vessel"
        is_info = r.get("datatyp") == "info"
        location = r.get("currentLocation") or ""

        date_str, time_str = _split_timestamp(r.get("timestampTimezone"), tz)
        transport = "VESSEL" if is_vessel else (r.get("transportmode") or "")

        # For vessel rows LDB reuses the isEmpty field to carry the vessel name.
        vessel = _clean(r.get("isEmpty")) if is_vessel else ""
        cargo = "" if is_vessel else _CARGO.get(r.get("isEmpty"), "")

        expected_next = ""
        if r.get("expectedEventName"):
            when = _display_datetime(r.get("expectedEventTime"), tz)
            expected_next = f"{r['expectedEventName']} by {when}" if when else r["expectedEventName"]

        lead_time = ""
        if r.get("avgLeadTime"):
            lead_time = _format_duration(timedelta(milliseconds=r["avgLeadTime"]))

        lat, lon = r.get("latitude"), r.get("longitude")
        has_coords = lat is not None and lon is not None

        remarks = []
        if is_info and base.upper().endswith("DPD"):
            remarks.append(f"Received as {name}")
            if location:
                remarks.append(f"next delivery to {location}")
        elif is_info and base.upper().endswith("DPE"):
            remarks.append(f"Dispatched as {name}")
        if is_vessel:
            line = shipping_lines.get((r.get("eventId"), r.get("cntrCycleId")))
            if line:
                remarks.append(f"Shipping line: {line}")
            if base.upper() == "ETD":
                remarks.append("Expected departure time")

        events.append([
            base or "-",
            location or "-",
            r.get("superorg") or "",
            date_str,
            time_str,
            _MOVEMENT.get(r.get("type"), ""),
            transport,
            r.get("truck_number") or "",
            vessel,
            cargo,
            dwells.get((r.get("timestampTimezone"), base.upper()), ""),
            expected_next,
            lead_time,
            f"{lat}, {lon}" if has_coords else "",
            "; ".join(remarks),
        ])
        coords.append([lat, lon] if has_coords else None)
        modes.append(transport.upper() or None)

    return events, coords, modes


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

    full = _full_event_table(detail)
    if full is not None:
        event_columns = _COLUMNS
        events, event_coords, event_modes = full
    else:
        # Older/partial responses without the full list: the 13-event trackLog.
        event_columns = ["Event", "Location", "Date", "Time", "Transport"]
        events, event_coords, event_modes = [], [], []
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
            event_modes.append((ev.get("transportmode") or "").upper() or None)

    return {
        "line": "ldb",
        "container_number": container_number,
        "summary_fields": summary_fields,
        "event_columns": event_columns,
        "events": events,
        "event_coords": event_coords,
        "event_modes": event_modes,
        # LDB supplies its own coordinates; a name-based city guess would pin
        # events without them (vessel rows, notes) to the wrong place.
        "geocode_fallback": False,
        "source_url": f"https://ldb.co.in/ldb/containersearch/{SEARCH_TYPE_SINGLE}/{container_number}/{int(time.time() * 1000)}",
    }
