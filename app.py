from flask import Flask, jsonify, render_template, request

from scrapers.container_number import is_valid_container_number, normalize
from scrapers.errors import ContainerNotFoundError, InvalidContainerNumberError
from scrapers.evergreen import track_container as track_evergreen
from scrapers.hapag import track_container as track_hapag
from scrapers.ldb import track_container as track_ldb
from scrapers.msc import track_container as track_msc
from scrapers.pil import track_container as track_pil
from scrapers.sitc import track_container as track_sitc
from scrapers.sci import track_container as track_sci
from scrapers.samudera import track_container as track_samudera
from scrapers.maersk import process_active_chrome_tab
from scrapers.route import build_route
from scrapers.analytics import build_analytics

app = Flask(__name__)

LINES = {
    "hapag": "Hapag-Lloyd",
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
    "hapag": track_hapag,
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


@app.route("/api/track", methods=["POST"])
def api_track():
    payload = request.get_json(silent=True) or {}
    line = (payload.get("line") or "").strip().lower()

    if line not in LINES:
        return jsonify({"error": "Please select a valid shipping line."}), 400

    # Samudera's public tool is BL-driven (with an optional container filter),
    # not container-number-driven like every other line, so it needs its own
    # required-field checks and a different call signature.
    if line == "samudera":
        bl_number = normalize(payload.get("bl_number") or "")
        search_type = (payload.get("search_type") or "bl_container").strip().lower()
        raw_container = payload.get("container") or ""
        container_number = normalize(raw_container) if raw_container else None

        if not bl_number:
            return jsonify({"error": "Please enter a BL number."}), 400
        if search_type == "bl_container" and not container_number:
            return jsonify({"error": "Please enter a container number."}), 400

        try:
            result = track_samudera(bl_number, container_number)
            result["mode"] = "inline"
            result["line_name"] = LINES[line]
            route = build_route(result)
            if route:
                result["route"] = route
            analytics = build_analytics(result, line)
            if analytics:
                result["analytics"] = analytics
            return jsonify(result)
        except ContainerNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception:
            app.logger.exception("Tracking lookup failed for line=samudera")
            return jsonify({
                "error": (
                    "Couldn't fetch tracking data from Samudera right now. "
                    "Their site may be temporarily unavailable or has changed. Please try again."
                )
            }), 502

    raw_container = payload.get("container") or ""
    container_number = normalize(raw_container)

    if not container_number:
        return jsonify({"error": "Please enter a container number."}), 400

    if not is_valid_container_number(container_number):
        return jsonify({
            "error": (
                f"'{container_number}' doesn't look like a valid container number "
                "(expected format: 4 letters + 7 digits, e.g. HLXU1234561)."
            )
        }), 400

    try:
        result = TRACKERS[line](container_number)
        result["mode"] = "inline"
        result["line_name"] = LINES[line]
        route = build_route(result)
        if route:
            result["route"] = route
        analytics = build_analytics(result, line)
        if analytics:
            result["analytics"] = analytics
        return jsonify(result)
    except InvalidContainerNumberError as exc:
        return jsonify({"error": str(exc)}), 400
    except ContainerNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception:
        app.logger.exception("Tracking lookup failed for line=%s", line)
        return jsonify({
            "error": (
                f"Couldn't fetch tracking data from {LINES[line]} right now. "
                "Their site may be temporarily unavailable or has changed. Please try again."
            )
        }), 502


if __name__ == "__main__":
    app.run(port=5005,host="0.0.0.0")
