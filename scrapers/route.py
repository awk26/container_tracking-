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

import searoute as sr

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


def _sea_path(coord_a, coord_b):
    """Waypoints from coord_a to coord_b that follow sea lanes (via the
    offline `searoute` package) instead of a straight line, so the route
    drawn on the map doesn't cut across land. Falls back to a plain
    two-point straight line if searoute can't route between them (e.g.
    identical points, or an inland/unroutable coordinate) for any reason.
    """
    try:
        geo_route = sr.searoute([coord_a[1], coord_a[0]], [coord_b[1], coord_b[0]])
        coords = geo_route.geometry["coordinates"]
        if len(coords) >= 2:
            return [(lat, lon) for lon, lat in coords]
    except Exception:
        pass
    return [coord_a, coord_b]


def build_route(result):
    """Returns {"points": [...], "current_index": int, "path": [...]} or
    None if no event in the result could be placed on a map. "path" is the
    full sea-following line to draw between the points, in travel order."""
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

    # Draw (and pick "current" from) points in actual travel order, not
    # whatever order the carrier's raw event list happened to use. Events
    # with no parseable date (e.g. "Information Not Available") can't be
    # placed by their own timestamp - forward/backward-fill them from the
    # nearest dated neighbor in the original order instead of collapsing
    # them all to the very front (which would zigzag the route all over
    # the map). Only if *no* event has a real date do we fall back to the
    # original order outright.
    sort_keys = [p["_sort_dt"] for p in points]
    last_known = None
    for i, dt in enumerate(sort_keys):
        if dt is not None:
            last_known = dt
        elif last_known is not None:
            sort_keys[i] = last_known
    next_known = None
    for i in range(len(sort_keys) - 1, -1, -1):
        if sort_keys[i] is not None:
            next_known = sort_keys[i]
        elif next_known is not None:
            sort_keys[i] = next_known

    order = sorted(range(len(points)), key=lambda i: sort_keys[i] if sort_keys[i] is not None else i)
    points = [points[i] for i in order]

    dated_indices = [i for i in range(len(points)) if points[i]["_sort_dt"] is not None]
    current_index = (
        max(dated_indices, key=lambda i: points[i]["_sort_dt"]) if dated_indices else len(points) - 1
    )

    for p in points:
        del p["_sort_dt"]

    path = []
    for a, b in zip(points, points[1:]):
        leg = _sea_path((a["lat"], a["lon"]), (b["lat"], b["lon"]))
        if path:
            leg = leg[1:]  # don't duplicate the join point with the previous leg
        path.extend(leg)
    if not path:
        path = [(points[0]["lat"], points[0]["lon"])]

    return {
        "points": points,
        "current_index": current_index,
        "path": [[lat, lon] for lat, lon in path],
    }
