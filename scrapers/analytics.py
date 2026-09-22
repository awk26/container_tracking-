"""Turns a tracker result into chart/summary-friendly data for the frontend:

- milestones: the "important" events worth showing in a compact summary,
  picked per shipping line (falls back to "all events" if nothing matches).
- leg_durations: days between each consecutive milestone, for a Gantt-style
  timeline and a simple bar-length comparison.
- mode_breakdown: how many events were vessel moves vs terminal/yard moves,
  for a donut chart.

Each shipping line's data shape is different (see LINE_CONFIGS), so this is
configured per line rather than guessed generically. A line with no entry
here still gets a reasonable generic result via DEFAULT_CONFIG.
"""

from collections import Counter

from .route import get_field, parse_event_datetime

# Per-line field names and the keywords (matched case-insensitively as a
# substring of the event/description text) that mark a "milestone" worth
# surfacing in the compact summary, plus how to read the vessel/transport
# mode for that line's Vessel/Voyage-style column.
LINE_CONFIGS = {
    "msc": {
        "date_fields": ["Date"],
        "time_fields": ["Time"],
        "location_fields": ["Location"],
        "event_fields": ["Description"],
        "vessel_fields": ["Vessel / Voyage"],
        "milestone_keywords": [
            "empty to shipper",
            "export received",
            "loaded on vessel",
            "transhipment discharged",
            "transhipment loaded",
            "estimated time of arrival",
            "gate",
        ],
    },
    "pil": {
        "date_fields": ["Event Date"],
        "time_fields": [],
        "location_fields": ["Event Place"],
        "event_fields": ["Event Name"],
        "vessel_fields": ["Vessel"],
        "milestone_keywords": [
            "empty container released",
            "gate in",
            "vessel loading",
            "vessel discharge",
            "gate out",
            "empty container returned",
        ],
    },
    "sitc": {
        "date_fields": ["Date"],
        "time_fields": [],
        "location_fields": ["Location"],
        "event_fields": ["Description"],
        "vessel_fields": ["Vessel / Voyage"],
        "milestone_keywords": [
            "gate-in",
            "gate in",
            "pickup",
            "in cy",
            "out cy",
            "load",
            "discharge",
            "empty",
        ],
    },
    "sci": {
        "date_fields": ["Date"],
        "time_fields": [],
        "location_fields": ["Location"],
        "event_fields": ["Description"],
        "vessel_fields": ["Vessel"],
        "milestone_keywords": [
            "gate in empty",
            "gate out full",
            "gate in full",
            "loaded full",
            "discharge",
            "gate out empty",
        ],
    },
    "samudera": {
        "date_fields": ["Date"],
        "time_fields": [],
        "location_fields": ["Location"],
        "event_fields": ["Description"],
        "vessel_fields": ["Vessel / Voyage"],
        "milestone_keywords": [
            "departed port of load",
            "transshipment",
            "arrival at port of discharge",
            "departure from port of discharge",
        ],
    },
}

DEFAULT_CONFIG = {
    "date_fields": ["Date", "Event Date"],
    "time_fields": ["Time"],
    "location_fields": ["Location", "Event Place"],
    "event_fields": ["Event", "Milestone", "Event Name", "Description", "Container Moves"],
    "vessel_fields": ["Vessel", "Vessel / Voyage", "Vessel Voyage", "Voyage No."],
    "milestone_keywords": [
        "gate in",
        "gate out",
        "loaded",
        "departed",
        "arrival",
        "arrived",
        "discharged",
        "empty",
    ],
}


def _classify_mode(vessel_text, event_text):
    vessel_text = (vessel_text or "").strip().upper()
    event_text = (event_text or "").lower()
    if vessel_text in ("EMPTY", "LADEN", "-", ""):
        if "vessel" in event_text or "discharged" in event_text or "loaded" in event_text:
            return "Vessel"
        return "Terminal / Yard"
    if "/" in vessel_text or any(c.isalpha() for c in vessel_text):
        return "Vessel"
    return "Other"


def build_analytics(result, line):
    events = result.get("events") or []
    if not events:
        return None

    columns = result.get("event_columns") or []
    config = LINE_CONFIGS.get(line, DEFAULT_CONFIG)

    parsed = []
    for row in events:
        date_text = get_field(row, columns, config["date_fields"])
        time_text = get_field(row, columns, config.get("time_fields", []))
        location = get_field(row, columns, config["location_fields"])
        event_text = get_field(row, columns, config["event_fields"])
        vessel_text = get_field(row, columns, config.get("vessel_fields", []))
        dt = parse_event_datetime(date_text, time_text)
        parsed.append({
            "event": event_text or "-",
            "location": location or "-",
            "date": date_text or "-",
            "time": time_text or "-",
            "vessel": vessel_text,
            "mode": _classify_mode(vessel_text, event_text),
            "dt": dt,
        })

    dated = sorted((p for p in parsed if p["dt"] is not None), key=lambda p: p["dt"])
    if not dated:
        # Nothing parses to a real date — still surface something rather than nothing.
        dated = parsed

    keywords = config.get("milestone_keywords") or []
    milestones = [p for p in dated if any(k in p["event"].lower() for k in keywords)]
    if len(milestones) < 2:
        milestones = dated

    leg_durations = []
    for a, b in zip(milestones, milestones[1:]):
        if a["dt"] and b["dt"]:
            days = round((b["dt"] - a["dt"]).total_seconds() / 86400, 2)
        else:
            days = None
        leg_durations.append({
            "from_event": a["event"],
            "to_event": b["event"],
            "from_date": a["dt"].isoformat() if a["dt"] else a["date"],
            "to_date": b["dt"].isoformat() if b["dt"] else b["date"],
            "days": days,
        })

    mode_breakdown = dict(Counter(p["mode"] for p in dated))

    def strip(p):
        return {
            "event": p["event"],
            "location": p["location"],
            "date": p["date"],
            "time": p["time"],
        }

    return {
        "milestones": [strip(p) for p in milestones],
        "leg_durations": leg_durations,
        "mode_breakdown": mode_breakdown,
    }
