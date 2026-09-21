"""Builds a map-friendly route (ordered points + which one is "current") from
a tracker result dict, for the frontend to draw on a Leaflet map.

The six scrapers don't agree on event ordering (some are newest-first, some
oldest-first, some just mirror whatever order the source site rendered), so
we can't trust array position to say which event is "latest". Instead we
parse each event's own date/time and pick the one that actually parses to
the latest moment. When nothing parses, we fall back to the last item in
the array rather than guessing further.
"""

from datetime import datetime

from . import geocode

_LOCATION_FIELDS = ["Location", "Event Place"]
_DATE_FIELDS = ["Date", "Event Date"]
_TIME_FIELDS = ["Time"]
_EVENT_FIELDS = ["Event", "Milestone", "Event Name", "Description", "Container Moves"]

_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%b %d",
    "%d %b",
]

# Some carriers (e.g. PIL) pack date and time into one field ("12-Aug-2026
# 23:35:00") instead of separate columns. Tried against the whole date_text
# before falling back to date-only parsing plus a separate time field.
_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%b-%Y %H:%M:%S",
    "%d-%b-%Y %H:%M",
    "%d %b %Y %H:%M:%S",
    "%d %b %Y %H:%M",
]


def get_field(row, event_columns, names):
    """Read a field by any of `names` from a row, whether it's a list (matched
    against event_columns) or a dict (matched by key, case-insensitively)."""
    if isinstance(row, dict):
        lower = {k.lower(): v for k, v in row.items()}
        for name in names:
            if name.lower() in lower:
                value = lower[name.lower()]
                if value not in (None, "", "-"):
                    return value
        return None
    if isinstance(row, (list, tuple)):
        for name in names:
            try:
                idx = next(i for i, c in enumerate(event_columns) if c.lower() == name.lower())
            except StopIteration:
                continue
            if idx < len(row) and row[idx] not in (None, "", "-"):
                return row[idx]
        return None
    return None


def parse_event_datetime(date_text, time_text=None):
    if not date_text:
        return None
    date_text = str(date_text).strip().lstrip("*").strip()
    time_text = str(time_text or "").strip()
    time_part = time_text.split(" ")[0] if time_text and time_text != "-" else ""

    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(date_text, fmt)
        except ValueError:
            continue

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(date_text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=datetime.now().year)
        if time_part:
            for tfmt in ("%H:%M:%S", "%H:%M"):
                try:
                    t = datetime.strptime(time_part, tfmt).time()
                except ValueError:
                    continue
                dt = dt.replace(hour=t.hour, minute=t.minute, second=t.second)
                break
        return dt
    return None


def build_route(result):
    """Returns {"points": [...], "current_index": int} or None if no event
    in the result could be placed on a map."""
    events = result.get("events") or []
    if not events:
        return None

    columns = result.get("event_columns") or []
    provided_coords = result.get("event_coords")

    points = []
    for i, row in enumerate(events):
        coord = None
        if provided_coords and i < len(provided_coords) and provided_coords[i]:
            lat, lon = provided_coords[i]
            if lat is not None and lon is not None:
                coord = (lat, lon)

        location_text = get_field(row, columns, _LOCATION_FIELDS)
        if coord is None:
            coord = geocode.find_coords(location_text)
        if coord is None:
            continue

        date_text = get_field(row, columns, _DATE_FIELDS)
        time_text = get_field(row, columns, _TIME_FIELDS)
        event_text = get_field(row, columns, _EVENT_FIELDS)
        dt = parse_event_datetime(date_text, time_text)

        points.append({
            "lat": coord[0],
            "lon": coord[1],
            "label": location_text or "-",
            "event": event_text or "-",
            "date": date_text or "-",
            "time": time_text or "-",
            "_sort_dt": dt,
        })

    if not points:
        return None

    dated = [p for p in points if p["_sort_dt"] is not None]
    current_index = points.index(max(dated, key=lambda p: p["_sort_dt"])) if dated else len(points) - 1

    for p in points:
        del p["_sort_dt"]

    return {"points": points, "current_index": current_index}
