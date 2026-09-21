"""Fetches container tracking data from PIL (Pacific International Lines).

Their public tracking widget at
https://www.pilship.com/digital-solutions/?tab=customer&id=track-trace calls
a plain JSON endpoint under the hood, so this hits that directly instead of
driving a browser — first fetching a short-lived "n" token, then the actual
container lookup, exactly like the page's own JS does.
"""

import time

import requests
from bs4 import BeautifulSoup

from .container_number import normalize
from .errors import ContainerNotFoundError

BASE_API = "https://www.pilship.com/wp-content/themes/hello-theme-child-master/pil-api"
REFERER = "https://www.pilship.com/digital-solutions/?tab=customer&id=track-trace"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": REFERER,
    "X-Requested-With": "XMLHttpRequest",
}


def _get_token() -> str:
    resp = requests.get(
        f"{BASE_API}/common/get-n.php",
        params={"timestamp": int(time.time() * 1000)},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("n"):
        raise ContainerNotFoundError("Couldn't reach PIL right now. Please try again in a moment.")
    return data["n"]


def track_container(raw_container_number: str) -> dict:
    container_number = normalize(raw_container_number)

    try:
        token = _get_token()
        resp = requests.get(
            f"{BASE_API}/trackntrace-containertnt.php",
            params={
                "module": "TrackContStatus",
                "refNo": container_number,
                "n": token,
                "timestamp": int(time.time() * 1000),
            },
            headers=HEADERS,
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ContainerNotFoundError(
            "Couldn't reach PIL right now. Please try again in a moment."
        ) from exc

    resp.raise_for_status()
    payload = resp.json()

    if payload.get("error") or not payload.get("success"):
        raise ContainerNotFoundError("No tracking data found for this container number on PIL.")

    soup = BeautifulSoup(payload.get("data", ""), "html.parser")

    # First table: route summary (Arrival/Delivery, Location, Vessel/Voyage, Next Location).
    # Each <td> uses <br> to separate several lines of info (e.g. port name + code).
    def _cell_lines(td):
        return [line for line in td.get_text("\n", strip=True).split("\n") if line]

    tables = soup.find_all("table")
    summary_fields = []
    if tables:
        route_rows = tables[0].find_all("tr", class_="resultrow")
        for row in route_rows:
            cells = row.find_all("td")
            if len(cells) != 4:
                continue
            dates, location, vessel, next_loc = (_cell_lines(td) for td in cells)
            label = location[0] if location else "Route"
            prefix = label.title().replace(" Port", "").strip() or "Route"

            port = " ".join(location[1:])
            vessel_text = " ".join(vessel)
            dates_text = " – ".join(dates)
            next_text = " ".join(next_loc)

            if port:
                summary_fields.append({"label": label, "value": port})
            if vessel_text:
                summary_fields.append({"label": f"{prefix} Vessel / Voyage", "value": vessel_text})
            if dates_text:
                summary_fields.append({"label": f"{prefix} Window", "value": dates_text})
            if next_text:
                summary_fields.append({"label": f"{prefix} Next Port", "value": next_text})

    # Second table: full event timeline (Vessel, Voyage, Event Date, Event Name, Event Place).
    event_columns = ["Vessel", "Voyage", "Event Date", "Event Name", "Event Place"]
    events = []
    if len(tables) > 1:
        body_rows = tables[1].find_all("tr", class_="text-fc-black")
        for row in body_rows:
            cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
            if len(cells) == len(event_columns):
                events.append(cells)

    if not summary_fields and not events:
        raise ContainerNotFoundError("No tracking data found for this container number on PIL.")

    return {
        "line": "pil",
        "container_number": container_number,
        "summary_fields": summary_fields,
        "event_columns": event_columns,
        "events": events,
        "source_url": REFERER,
    }
