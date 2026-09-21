# Container Tracking

A small Flask app to look up a container's tracking status without needing an
account with the carrier.

Supported lines:

- **Hapag-Lloyd** — results are fetched live by driving a headless browser
  against Hapag-Lloyd's own public tracking page
  (https://www.hapag-lloyd.com/solutions/tracking/) and shown inline.
- **LDB (India Container Tracking)** — India's government-run Logistics Data
  Bank (NICDC Logistics Data Services, https://ldb.co.in). Exposes a plain
  unauthenticated JSON endpoint, so results are fetched with a direct HTTP
  request — no browser automation needed. Covers container movement through
  Indian ports, ICDs and CFSs; a container with no Indian port/rail activity
  won't have data here regardless of carrier.
- **Evergreen** — ShipmentLink's tracking system
  (https://ct.shipmentlink.com/servlet/TDB1_CargoTracking.do) is a classic
  server-rendered form with no bot protection, so results are fetched with a
  direct HTTP POST (same fields the page's own form submits). A
  container-number lookup only returns the container's latest move, not its
  full history — Evergreen only exposes the full timeline for a Bill of
  Lading search.
- **PIL (Pacific International Lines)** — their tracking widget
  (https://www.pilship.com/digital-solutions/?tab=customer&id=track-trace)
  calls a plain JSON API under the hood, so this hits that endpoint directly.
- **MSC** — MSC's site is protected by Akamai Bot Manager, which blocks the
  page outright when driven by Playwright's bundled test-Chromium build.
  Launching the machine's actual installed Google Chrome instead (Playwright's
  `channel="chrome"`) gets through with no challenge, so `scrapers/msc.py`
  loads the tracking page once with real Chrome, then calls MSC's own JSON
  API (`/api/feature/tools/TrackingInfo`) directly via `fetch()` from inside
  that page. **Requires Google Chrome to be installed on the machine running
  this app** (not just Playwright's bundled Chromium).

Not supported:

- **CMA CGM** — tracking page is protected by DataDome bot detection that
  blocks automated requests with a CAPTCHA challenge.
- **Maersk** — the main site (`www.maersk.com`) actually loads fine with real
  Chrome, same fix as MSC. But the tracking data itself comes from a separate
  subdomain, `api.maersk.com`, which has its own Akamai protection and
  returns a hard `403 Access Denied` even to that same real-Chrome session.
  Maersk's frontend silently swallows that error and shows a generic "No
  results found" instead of surfacing it, which is why this initially looked
  like a data-availability issue rather than a block.

This app does not attempt to bypass either of the above — both are explicit
Akamai/DataDome edge rejections aimed at stopping automated requests
outright, which is a different situation from MSC, where a normal Chrome
browser is let through with no challenge at all.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
playwright install chromium
```

Google Chrome must also be installed normally on the machine (for MSC's
scraper — see above). If it's not at the default install location, adjust
how `scrapers/msc.py` launches it (`channel="chrome"` relies on Playwright's
auto-detection of the system install).

## Run

```bash
python app.py
```

Then open http://127.0.0.1:5000/.

## Notes / limitations

- Hapag-Lloyd's tracking page is a JavaScript SPA with no public JSON API, so
  this app scrapes the rendered page with Playwright. If Hapag-Lloyd changes
  their page structure, the scraper in `scrapers/hapag.py` may need updating
  (selectors are centralized near the top of that file). Same caveat applies
  to Evergreen's HTML table structure and MSC's/PIL's/LDB's JSON response
  shapes if those providers change their sites or APIs.
- Each Hapag-Lloyd and MSC lookup launches a fresh browser, which takes a few
  seconds. LDB, Evergreen and PIL lookups are plain HTTP requests and are
  fast.
- No login/registration is required anywhere in this app or from any of the
  supported data sources.
- Container numbers are validated client- and server-side against the
  ISO 6346 check-digit format before a lookup is attempted.
