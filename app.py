import json
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template

from topic_engine import engine as engine_module
from web_crawlers import crawler as crawler_module


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data.json")

# Port 5000 is macOS's default AirPlay Receiver port (hosted by
# ControlCenter.app) and gets intermittently reclaimed by the OS, causing
# the dev server to fail to bind or become unreachable even though this
# app's own process is otherwise healthy — confirmed directly during local
# runtime auditing. 5050 is not a common macOS system service port.
PORT = 5050

# The header date/time is always shown in US Eastern time regardless of
# what timezone the machine running the server is set to, so it reads
# correctly for the podcast's actual local audience.
EASTERN_TZ = ZoneInfo("America/New_York")

app = Flask(__name__)


def format_eastern_now():
    """e.g. "Sunday, August 16, 2026, 7:10:43 pm EDT" — %Z reflects EST/EDT
    correctly depending on whether daylight saving is in effect."""
    now = datetime.now(EASTERN_TZ)
    formatted = now.strftime("%A, %B %d, %Y, %-I:%M:%S %p %Z")
    return formatted.replace("AM", "am").replace("PM", "pm")


def format_updated_at(value):
    """Format an ISO timestamp for the dashboard in US Eastern time.

    Older data files contain timezone-naive timestamps that were intended
    to represent Eastern local time, so preserve that interpretation. New
    timezone-aware timestamps are converted to America/New_York, including
    the correct daylight-saving offset for their date.
    """
    if not isinstance(value, str) or not value.strip():
        return "Not available"

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        updated = datetime.fromisoformat(normalized)
    except ValueError:
        return "Not available"

    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=EASTERN_TZ)
    else:
        updated = updated.astimezone(EASTERN_TZ)

    date_part = updated.strftime("%A, %B")
    time_part = updated.strftime("%I:%M:%S %p").lstrip("0")
    return f"{date_part} {updated.day}, {updated.year}, at {time_part}"

# Real Refresh V1: this is a local, single-user app, so a plain in-process
# lock is enough to make sure only one refresh (crawler + Topic Engine) runs
# at a time — no queue/job-system needed. Non-blocking acquire: a second
# request while one is running is rejected immediately (409) rather than
# queued.
refresh_lock = threading.Lock()

REGIONS = {
    "shenandoah_valley": {
        "title": "SHENANDOAH VALLEY",
        "anchor": "shenandoah",
        "categories": [
            ("real_estate", "REAL ESTATE"),
            ("community", "COMMUNITY"),
            ("business_economy", "BUSINESS & ECONOMY"),
            ("money_finance", "MONEY & FINANCE"),
            ("food_lifestyle", "FOOD & LIFESTYLE"),
            ("faith_family", "FAITH & FAMILY"),
            ("news_conversation", "NEWS & CONVERSATION"),
        ],
    },
    "northern_virginia": {
        "title": "NORTHERN VIRGINIA",
        "anchor": "northern-virginia",
        "categories": [
            ("real_estate", "REAL ESTATE"),
            ("community", "COMMUNITY"),
            ("business_economy", "BUSINESS & ECONOMY"),
            ("money_finance", "MONEY & FINANCE"),
            ("food_lifestyle", "FOOD & LIFESTYLE"),
            ("faith_family", "FAITH & FAMILY"),
            ("news_conversation", "NEWS & CONVERSATION"),
        ],
    },
}


def load_data():
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise FileNotFoundError("data.json not found")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in data.json: {exc}") from exc


@app.route("/")
def index():
    try:
        data = load_data()
    except (FileNotFoundError, ValueError) as exc:
        return render_template("error.html", error=str(exc)), 500

    current_date = format_eastern_now()
    return render_template(
        "index.html",
        data=data,
        regions=REGIONS,
        current_date=current_date,
        updated_at=format_updated_at(data.get("updated_at")),
    )


@app.route("/api/data")
def api_data():
    try:
        data = load_data()
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(data)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Get the newest evidence now: run the existing web crawler (default
    collection window, default source list — nothing special-cased for
    Refresh), then, only if that succeeds, run the existing Topic Engine
    and write data.json. One refresh at a time; a second request while one
    is already running is rejected rather than queued or run concurrently.
    A failure at either step leaves the last successful data.json exactly
    as it was — this endpoint never blanks or deletes existing evidence."""
    if not refresh_lock.acquire(blocking=False):
        return jsonify({"success": False, "error": "Refresh already in progress"}), 409

    try:
        try:
            crawl_summary = crawler_module.run_crawl()
            app.logger.info("Refresh: crawler finished — %s", crawl_summary)
        except Exception:
            app.logger.exception("Refresh: web crawler failed")
            return jsonify({"success": False, "error": "Refresh failed"}), 500

        try:
            data, _selected_debug, stats = engine_module.run_pipeline()
            engine_module.write_data(data)
            app.logger.info("Refresh: topic engine finished — %s", stats)
        except Exception:
            app.logger.exception("Refresh: topic engine failed")
            return jsonify({"success": False, "error": "Refresh failed"}), 500

        return jsonify({"success": True})
    finally:
        refresh_lock.release()


if __name__ == "__main__":
    # threaded=True so a long-running /api/refresh doesn't block the whole
    # server from handling other requests (e.g. the page itself, or a
    # second refresh click that the lock above needs to actually reject).
    app.run(host="127.0.0.1", port=PORT, debug=True, threaded=True)
