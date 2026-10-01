import re

from flask import Flask, jsonify, render_template, request, Response

from scrapers.container_number import is_valid_container_number, normalize
from scrapers.errors import ContainerNotFoundError, InvalidContainerNumberError
from scrapers.evergreen import track_container as track_evergreen
from scrapers.ldb import track_container as track_ldb
from scrapers.msc import track_container as track_msc
from scrapers.pil import track_container as track_pil
from scrapers.sitc import track_container as track_sitc
from scrapers.sci import track_container as track_sci
from scrapers.samudera import track_container as track_samudera
from scrapers.maersk import process_active_chrome_tab
from scrapers.route import build_route
from scrapers.analytics import build_analytics
from mailer import send_tracking_email, is_valid_email, EmailConfigError, EmailSendError
from exporter import build_xlsx_bytes, build_pdf_bytes

app = Flask(__name__)

# Security headers (same VAPT-hardening pattern as the bjk-sports project):
# a strict Content-Security-Policy plus the usual companion headers.
#
# Leaflet and Chart.js are vendored locally under static/vendor/ (see
# static/download_vendor_assets.sh) rather than loaded from unpkg/jsdelivr
# at runtime, so script-src/style-src can stay locked to 'self' with no
# third-party hosts and no per-request nonce machinery: this app has zero
# inline <script> tags, and the one place that used to need inline style
# (the Leaflet popup markup in app.js) was moved to CSS classes instead of
# reaching for 'unsafe-inline'.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: https://*.tile.openstreetmap.org; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; form-action 'self'; object-src 'none'; base-uri 'self';"
)


@app.after_request
def set_security_headers(resp):
    resp.headers["Content-Security-Policy"] = _CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(404)
def not_found(_e):
    return jsonify({"error": "Not found."}), 404


@app.errorhandler(500)
def server_error(_e):
    return jsonify({"error": "Internal server error."}), 500


LINES = {
    "ldb": "LDB (India Container Tracking)",
    "evergreen": "Evergreen",
    "pil": "PIL (Pacific International Lines)",
    "msc": "MSC",
    "sitc": "SITC",
    "sci": "SCI (Shipping Corporation of India)",
    "samudera": "Samudera Shipping Line",
    "maersk": "Maersk"
}

TRACKERS = {
    "ldb": track_ldb,
    "evergreen": track_evergreen,
    "pil": track_pil,
    "msc": track_msc,
    "sitc": track_sitc,
    "sci": track_sci,
    "maersk": process_active_chrome_tab
}


@app.route("/")
def index():
    return render_template("index.html", lines=LINES)


class TrackerError(Exception):
    """A user-facing tracking failure: `message` is safe to show as-is,
    `status_code` is the HTTP status the route should reply with."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def run_tracker(line, payload):
    """Runs the right carrier tracker for `line` using `payload` (same
    shape as /api/track's JSON body) and returns the enriched result dict,
    or raises TrackerError. Shared by /api/track and /api/share so sharing
    always re-fetches through this same validated path rather than trusting
    client-supplied tracking data for the email body - otherwise the share
    endpoint could be used to send arbitrary content to arbitrary addresses.
    """
    line = (line or "").strip().lower()
    if line not in LINES:
        raise TrackerError("Please select a valid shipping line.", 400)

    # Samudera's public tool is BL-driven (with an optional container filter),
    # not container-number-driven like every other line, so it needs its own
    # required-field checks and a different call signature.
    if line == "samudera":
        bl_number = normalize(payload.get("bl_number") or "")
        search_type = (payload.get("search_type") or "bl_container").strip().lower()
        raw_container = payload.get("container") or ""
        container_number = normalize(raw_container) if raw_container else None

        if not bl_number:
            raise TrackerError("Please enter a BL number.", 400)
        if search_type == "bl_container" and not container_number:
            raise TrackerError("Please enter a container number.", 400)

        try:
            result = track_samudera(bl_number, container_number)
        except ContainerNotFoundError as exc:
            raise TrackerError(str(exc), 404) from exc
        except Exception as exc:
            app.logger.exception("Tracking lookup failed for line=samudera")
            raise TrackerError(
                "Couldn't fetch tracking data from Samudera right now. "
                "Their site may be temporarily unavailable or has changed. Please try again.",
                502,
            ) from exc
    else:
        raw_container = payload.get("container") or ""
        container_number = normalize(raw_container)

        if not container_number:
            raise TrackerError("Please enter a container number.", 400)

        if not is_valid_container_number(container_number):
            raise TrackerError(
                f"'{container_number}' doesn't look like a valid container number "
                "(expected format: 4 letters + 7 digits, e.g. HLXU1234561).",
                400,
            )

        try:
            result = TRACKERS[line](container_number)
        except InvalidContainerNumberError as exc:
            raise TrackerError(str(exc), 400) from exc
        except ContainerNotFoundError as exc:
            raise TrackerError(str(exc), 404) from exc
        except Exception as exc:
            app.logger.exception("Tracking lookup failed for line=%s", line)
            raise TrackerError(
                f"Couldn't fetch tracking data from {LINES[line]} right now. "
                "Their site may be temporarily unavailable or has changed. Please try again.",
                502,
            ) from exc

    result["mode"] = "inline"
    result["line_name"] = LINES[line]
    route = build_route(result)
    if route:
        result["route"] = route
    analytics = build_analytics(result, line)
    if analytics:
        result["analytics"] = analytics
    return result


@app.route("/api/track", methods=["POST"])
def api_track():
    payload = request.get_json(silent=True) or {}
    try:
        result = run_tracker(payload.get("line"), payload)
        return jsonify(result)
    except TrackerError as exc:
        return jsonify({"error": exc.message}), exc.status_code


@app.route("/api/share", methods=["POST"])
def api_share():
    payload = request.get_json(silent=True) or {}
    to_email = (payload.get("to_email") or "").strip()
    note = (payload.get("note") or "").strip()[:500]

    if not is_valid_email(to_email):
        return jsonify({"error": "Please enter a valid email address."}), 400

    try:
        result = run_tracker(payload.get("line"), payload)
    except TrackerError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    try:
        send_tracking_email(to_email, result, note)
    except EmailConfigError as exc:
        app.logger.error("Email share unavailable: %s", exc)
        return jsonify({"error": str(exc)}), 503
    except EmailSendError:
        app.logger.exception("Email share failed")
        return jsonify({"error": "Couldn't send the email right now. Please try again."}), 502

    return jsonify({"ok": True})


_EXPORT_MIME_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


@app.route("/api/export/<fmt>", methods=["POST"])
def api_export(fmt):
    if fmt not in _EXPORT_MIME_TYPES:
        return jsonify({"error": "Unsupported export format."}), 400

    payload = request.get_json(silent=True) or {}
    try:
        result = run_tracker(payload.get("line"), payload)
    except TrackerError as exc:
        return jsonify({"error": exc.message}), exc.status_code

    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", str(result.get("container_number") or "container"))[:40]

    try:
        if fmt == "xlsx":
            data = build_xlsx_bytes(result)
        else:
            data = build_pdf_bytes(result)
    except Exception:
        app.logger.exception("Failed to build %s export", fmt)
        return jsonify({"error": "Couldn't generate that file right now. Please try again."}), 500

    return Response(
        data,
        mimetype=_EXPORT_MIME_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.{fmt}"'},
    )


if __name__ == "__main__":
    # NOTE: this is Werkzeug's development server - Flask's own docs say not
    # to use it in production (no protection against slow clients, limited
    # concurrency, verbose error pages if debug is ever turned on). The
    # version_string patch below is a stopgap for the "Server" header leak;
    # the real fix for a production deployment is running this through a
    # real WSGI server instead, e.g.: `pip install waitress` then
    # `waitress-serve --host=0.0.0.0 --port=5005 app:app`.
    #
    # Werkzeug's HTTP layer writes its own "Server: Werkzeug/x.x Python/x.x"
    # header directly (not through Flask's response object), so a
    # response-header override doesn't replace it - only patching the
    # method that generates it does.
    import werkzeug.serving
    werkzeug.serving.WSGIRequestHandler.version_string = lambda self: "webserver"

    app.run(port=5005, host="0.0.0.0")
