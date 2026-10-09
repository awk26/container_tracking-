"""Lookup from a free-text port/terminal name to (lat, lon).

Carrier tracking pages describe locations in free text ("Shanghai · Yangshan
Terminal", "SHANGHAI, CHINA", "Nhava Sheva CFS"), not coordinates. LDB's raw
API is the one exception (it returns exact lat/lon per event), so callers
should prefer any coordinates already present on the event and only fall
back to this lookup when none are available.

Resolution order for a name:
  1. the small built-in PORT_COORDS table (major ports; instant, unambiguous)
  2. the on-disk cache (data/geocode_cache.json) - remembers hits *and* misses
  3. OpenStreetMap Nominatim, so any other port in the world resolves
     dynamically (and is then cached)

A miss is normal - callers treat "no match" as "skip this point on the map".
"""

import json
import os
import re
import threading
import time

import requests

CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "geocode_cache.json")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent and <= 1 request/second.
_HEADERS = {"User-Agent": "container-tracking-app/1.0 (port geocoding)"}
_MIN_INTERVAL = 1.1
_MAX_QUERIES_PER_LOOKUP = 4

_lock = threading.Lock()
_cache = None
_last_request = 0.0

# (lat, lon) for major container ports. Keys are matched case-insensitively
# as whole words/phrases against free-text location strings.
PORT_COORDS = {
    "shanghai": (31.2, 121.5),
    "ningbo": (29.9, 121.6),
    "shenzhen": (22.5, 114.1),
    "guangzhou": (23.1, 113.3),
    "qingdao": (36.1, 120.3),
    "tianjin": (39.0, 117.7),
    "xiamen": (24.5, 118.1),
    "dalian": (38.9, 121.6),
    "hong kong": (22.3, 114.2),
    "kaohsiung": (22.6, 120.3),
    "taipei": (25.1, 121.4),
    "keelung": (25.1, 121.7),
    "busan": (35.1, 129.0),
    "incheon": (37.5, 126.6),
    "tokyo": (35.6, 139.8),
    "yokohama": (35.4, 139.7),
    "kobe": (34.7, 135.2),
    "nagoya": (35.1, 136.9),
    "osaka": (34.7, 135.4),
    "singapore": (1.3, 103.8),
    "port klang": (3.0, 101.4),
    "port kelang": (3.0, 101.4),
    "klang": (3.0, 101.4),
    "yangon": (16.8, 96.2),
    "tanjung pelepas": (1.4, 103.6),
    "laem chabang": (13.1, 100.9),
    "bangkok": (13.7, 100.6),
    "ho chi minh": (10.8, 106.7),
    "cai mep": (10.6, 107.0),
    "haiphong": (20.9, 106.7),
    "manila": (14.6, 120.9),
    "jakarta": (6.1, 106.9),
    "tanjung priok": (6.1, 106.9),
    "surabaya": (7.2, 112.7),
    "colombo": (6.9, 79.8),
    "nhava sheva": (18.9, 72.9),
    "jnpt": (18.9, 72.9),
    "mumbai": (18.9, 72.8),
    "chennai": (13.1, 80.3),
    "kolkata": (22.6, 88.3),
    "cochin": (9.9, 76.3),
    "kochi": (9.9, 76.3),
    "mundra": (22.8, 69.7),
    "chittagong": (22.3, 91.8),
    "karachi": (24.8, 66.9),
    "jebel ali": (25.0, 55.1),
    "dubai": (25.3, 55.3),
    "abu dhabi": (24.5, 54.4),
    "khor fakkan": (25.3, 56.4),
    "dammam": (26.4, 50.1),
    "jeddah": (21.5, 39.2),
    "salalah": (17.0, 54.1),
    "bandar abbas": (27.2, 56.3),
    "aqaba": (29.5, 35.0),
    "haifa": (32.8, 35.0),
    "ashdod": (31.8, 34.6),
    "beirut": (33.9, 35.5),
    "istanbul": (41.0, 28.9),
    "mersin": (36.8, 34.6),
    "izmit": (40.8, 29.9),
    "piraeus": (37.9, 23.6),
    "constanta": (44.2, 28.7),
    "odessa": (46.5, 30.7),
    "novorossiysk": (44.7, 37.8),
    "malta": (35.8, 14.5),
    "valletta": (35.9, 14.5),
    "genoa": (44.4, 8.9),
    "la spezia": (44.1, 9.8),
    "gioia tauro": (38.4, 15.9),
    "naples": (40.8, 14.3),
    "trieste": (45.6, 13.8),
    "marseille": (43.3, 5.4),
    "fos": (43.4, 4.9),
    "barcelona": (41.3, 2.2),
    "valencia": (39.4, -0.3),
    "algeciras": (36.1, -5.4),
    "las palmas": (28.1, -15.4),
    "lisbon": (38.7, -9.1),
    "sines": (37.9, -8.9),
    "leixoes": (41.2, -8.7),
    "rotterdam": (51.9, 4.5),
    "antwerp": (51.3, 4.4),
    "zeebrugge": (51.3, 3.2),
    "amsterdam": (52.4, 4.9),
    "bremerhaven": (53.5, 8.6),
    "hamburg": (53.5, 10.0),
    "wilhelmshaven": (53.5, 8.1),
    "le havre": (49.5, 0.1),
    "dunkirk": (51.0, 2.4),
    "felixstowe": (51.9, 1.3),
    "southampton": (50.9, -1.4),
    "london gateway": (51.5, 0.5),
    "liverpool": (53.4, -3.0),
    "gothenburg": (57.7, 11.9),
    "gdansk": (54.4, 18.6),
    "gdynia": (54.5, 18.5),
    "st petersburg": (59.9, 30.3),
    "aarhus": (56.1, 10.2),
    "helsinki": (60.2, 25.0),
    "new york": (40.7, -74.0),
    "newark": (40.7, -74.1),
    "savannah": (32.1, -81.1),
    "charleston": (32.8, -79.9),
    "norfolk": (36.8, -76.3),
    "baltimore": (39.3, -76.6),
    "miami": (25.8, -80.2),
    "houston": (29.7, -95.2),
    "new orleans": (29.9, -90.1),
    "los angeles": (33.7, -118.3),
    "long beach": (33.8, -118.2),
    "oakland": (37.8, -122.3),
    "seattle": (47.6, -122.3),
    "tacoma": (47.3, -122.4),
    "vancouver": (49.3, -123.1),
    "prince rupert": (54.3, -130.3),
    "manzanillo": (19.1, -104.3),
    "lazaro cardenas": (17.9, -102.2),
    "veracruz": (19.2, -96.1),
    "kingston": (18.0, -76.8),
    "freeport": (26.5, -78.7),
    "cartagena": (10.4, -75.5),
    "colon": (9.4, -79.9),
    "balboa": (8.9, -79.6),
    "guayaquil": (-2.2, -79.9),
    "callao": (-12.0, -77.1),
    "valparaiso": (-33.0, -71.6),
    "san antonio": (-33.6, -71.6),
    "buenos aires": (-34.6, -58.4),
    "montevideo": (-34.9, -56.2),
    "santos": (-23.96, -46.3),
    "rio de janeiro": (-22.9, -43.2),
    "paranagua": (-25.5, -48.5),
    "itajai": (-26.9, -48.7),
    "durban": (-29.9, 31.0),
    "cape town": (-33.9, 18.4),
    "port elizabeth": (-33.96, 25.6),
    "ngqura": (-33.8, 25.7),
    "mombasa": (-4.1, 39.7),
    "dar es salaam": (-6.8, 39.3),
    "lagos": (6.5, 3.4),
    "apapa": (6.5, 3.4),
    "tema": (5.6, -0.0),
    "abidjan": (5.3, -4.0),
    "dakar": (14.7, -17.4),
    "tangier": (35.8, -5.8),
    "casablanca": (33.6, -7.6),
    "alexandria": (31.2, 29.9),
    "port said": (31.3, 32.3),
    "damietta": (31.4, 31.8),
    "sydney": (-33.9, 151.2),
    "melbourne": (-37.8, 144.9),
    "brisbane": (-27.4, 153.2),
    "fremantle": (-32.1, 115.7),
    "auckland": (-36.8, 174.8),
    "tauranga": (-37.7, 176.2),
}

# Longest names first so "port klang" matches before the shorter "klang".
_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in sorted(PORT_COORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _load_cache():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as fh:
                _cache = json.load(fh)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache():
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_cache, fh, ensure_ascii=False, indent=0)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass  # cache is only an optimisation


def _candidates(text):
    """Search phrases to try, most specific first: the cleaned text, then each
    piece split on , / · - ( ) (terminal/CFS words dropped)."""
    text = re.sub(r"(container|terminals?|cfs|icd|depot|yard|gate|port of)", " ", text, flags=re.I)
    pieces = [p.strip(" .") for p in re.split(r"[,/·()|;\-]", text)]
    pieces = [re.sub(r"\s+", " ", p) for p in pieces if len(p.strip()) > 2]
    seen, out = set(), []
    for p in [re.sub(r"\s+", " ", re.sub(r"[,/·()|;]", " ", text)).strip()] + pieces:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def _nominatim(query, port_only=False):
    """(lat, lon) for one query or None. With port_only, only harbour/port
    style hits count (so "Santos port" can't resolve to a town called Santos)."""
    global _last_request
    wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": 5},
        headers=_HEADERS,
        timeout=6,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    portish = [r for r in results if r.get("category") in ("harbour", "landuse", "industrial") or "port" in (r.get("type") or "")]
    if port_only and not portish:
        return None
    best = (portish or results)[0]
    return round(float(best["lat"]), 5), round(float(best["lon"]), 5)


def _dynamic_lookup(location_text):
    key = " ".join(location_text.lower().split())
    cache = _load_cache()
    if key in cache:
        hit = cache[key]
        return tuple(hit) if hit else None

    found = None
    network_ok = True
    for phrase in _candidates(location_text)[:_MAX_QUERIES_PER_LOOKUP]:
        try:
            found = _nominatim(phrase + " port", port_only=True) or _nominatim(phrase)
        except (requests.RequestException, ValueError, KeyError):
            network_ok = False
            break
        if found:
            break

    if network_ok:  # don't remember a miss caused by being offline
        cache[key] = list(found) if found else None
        _save_cache()
    return found


def find_coords(location_text):
    """Return (lat, lon) for the port named in location_text, or None."""
    if not location_text:
        return None
    location_text = str(location_text)

    # Known major ports resolve instantly and unambiguously from the table;
    # everything else is looked up dynamically.
    match = _PATTERN.search(location_text)
    if match:
        return PORT_COORDS[match.group(1).lower()]

    with _lock:
        return _dynamic_lookup(location_text)
