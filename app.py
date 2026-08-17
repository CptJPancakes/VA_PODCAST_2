import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

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

TIMEFRAME_RANGES = ("today", "24h", "3d", "7d", "14d")
DEFAULT_TIMEFRAME = "today"


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

# A hunt is deliberately manual: importing or starting this app never runs the
# crawler. POST /api/refresh is the only place that starts one. The job itself
# runs in a daemon thread so the request returns immediately and the browser can
# poll the status endpoint while the last successful dashboard remains visible.
_hunt_state_lock = threading.Lock()
_hunt_thread = None


def _idle_hunt_state():
    return {
        "job_id": None,
        "status": "idle",
        "phase": "idle",
        "message": "Ready to go hunting.",
        "total": 0,
        "completed": 0,
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "current_source": None,
        "current_timeframe": None,
        "completed_timeframes": 0,
        "total_timeframes": len(TIMEFRAME_RANGES),
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": 0,
        "eta_seconds": None,
        "error": None,
        "dashboard_unchanged": False,
        # Internal monotonic time is never returned from the API.
        "_started_monotonic": None,
    }


_hunt_state = _idle_hunt_state()

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


def normalize_timeframe(value):
    """Return a supported timeframe without letting arbitrary query values
    leak into the template or bundle lookup."""
    return value if value in TIMEFRAME_RANGES else DEFAULT_TIMEFRAME


def select_timeframe_data(stored_data, timeframe):
    """Select one dashboard view from the V2 bundle.

    Existing/legacy data.json files contain the dashboard directly at the
    root. They continue to work until the first successful hunt publishes a
    multi-timeframe bundle. A malformed or incomplete V2 bundle falls back to
    its declared default (then Today) instead of turning the dashboard into a
    500 response.
    """
    if not isinstance(stored_data, dict):
        return stored_data

    timeframes = stored_data.get("timeframes")
    if not isinstance(timeframes, dict):
        return stored_data

    requested = normalize_timeframe(timeframe)
    fallback = stored_data.get("default_range")
    candidates = (requested, fallback, DEFAULT_TIMEFRAME)
    selected = next(
        (timeframes.get(key) for key in candidates if isinstance(timeframes.get(key), dict)),
        None,
    )
    if selected is None:
        selected = next((view for view in timeframes.values() if isinstance(view, dict)), {})

    # Copy so rendering never mutates the in-memory object returned by
    # load_data. The bundle owns the successful-hunt timestamp; tolerate an
    # older producer that also left updated_at inside an individual view.
    result = dict(selected)
    if stored_data.get("updated_at"):
        result["updated_at"] = stored_data["updated_at"]
    return result


@app.route("/")
def index():
    try:
        stored_data = load_data()
    except (FileNotFoundError, ValueError) as exc:
        return render_template("error.html", error=str(exc)), 500

    active_range = normalize_timeframe(request.args.get("range"))
    data = select_timeframe_data(stored_data, active_range)
    current_date = format_eastern_now()
    return render_template(
        "index.html",
        data=data,
        regions=REGIONS,
        current_date=current_date,
        updated_at=format_updated_at(data.get("updated_at")),
        active_range=active_range,
    )


@app.route("/api/data")
def api_data():
    try:
        stored_data = load_data()
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 500
    active_range = normalize_timeframe(request.args.get("range"))
    data = select_timeframe_data(stored_data, active_range)
    return jsonify(data)


def _status_snapshot_locked():
    """Build the public status payload while _hunt_state_lock is held."""
    snapshot = {key: value for key, value in _hunt_state.items() if not key.startswith("_")}

    if _hunt_state["status"] == "running" and _hunt_state["_started_monotonic"] is not None:
        elapsed = max(0.0, time.monotonic() - _hunt_state["_started_monotonic"])
        snapshot["elapsed_seconds"] = round(elapsed, 1)
        completed = _hunt_state["completed"]
        total = _hunt_state["total"]
        if _hunt_state["phase"] == "fetching_sources" and completed > 0 and total > completed:
            snapshot["eta_seconds"] = round((elapsed / completed) * (total - completed), 1)
        elif _hunt_state["phase"] in {"checking_source_health", "ranking_timeframes", "publishing"}:
            snapshot["eta_seconds"] = None

    return snapshot


def get_hunt_status():
    with _hunt_state_lock:
        return _status_snapshot_locked()


def _update_hunt(job_id, **changes):
    """Update only the currently active job (guards against stale threads)."""
    with _hunt_state_lock:
        if _hunt_state.get("job_id") != job_id:
            return
        _hunt_state.update(changes)


def _crawler_progress(job_id, event):
    if not isinstance(event, dict):
        return

    changes = {
        "phase": "fetching_sources",
        "message": "Checking local news and government sources…",
    }
    for key in ("total", "completed", "attempted", "succeeded", "failed"):
        value = event.get(key)
        if isinstance(value, int) and value >= 0:
            changes[key] = value

    current_source = event.get("current_source")
    if not isinstance(current_source, dict) and any(event.get(key) for key in ("source_id", "source_name", "region")):
        current_source = {
            "id": event.get("source_id"),
            "name": event.get("source_name"),
            "region": event.get("region"),
        }
    if isinstance(current_source, dict):
        changes["current_source"] = current_source

    _update_hunt(job_id, **changes)


def _source_health_error(summary):
    """Return a user-safe health-gate explanation, or None when healthy."""
    attempted = int(summary.get("attempted") or 0)
    succeeded = int(summary.get("succeeded") or 0)
    failures = []

    if attempted <= 0:
        failures.append("no enabled sources were attempted")
    elif succeeded < math.ceil(attempted / 2):
        failures.append(f"only {succeeded} of {attempted} sources succeeded")

    failed_regions = []
    regions = summary.get("regions")
    if isinstance(regions, dict):
        for region, region_stats in regions.items():
            if not isinstance(region_stats, dict):
                continue
            if int(region_stats.get("attempted") or 0) > 0 and int(region_stats.get("succeeded") or 0) < 1:
                failed_regions.append(str(region).replace("_", " ").title())
    if failed_regions:
        failures.append("no sources succeeded in " + ", ".join(failed_regions))

    if summary.get("saved") is False and not failures:
        failures.append("the crawler could not safely save its results")

    if not failures:
        return None
    return "Source health check failed: " + "; ".join(failures) + "."


def _validate_dashboard_view(view, timeframe):
    if not isinstance(view, dict):
        raise ValueError(f"Topic Engine returned invalid data for {timeframe}")
    for region in REGIONS:
        if not isinstance(view.get(region), dict):
            raise ValueError(f"Topic Engine omitted {region} for {timeframe}")


def _run_hunt(job_id):
    """Crawler + five deterministic views + one atomic dashboard publish."""
    try:
        crawl_summary = crawler_module.run_crawl(
            progress_callback=lambda event: _crawler_progress(job_id, event),
            max_workers=8,
            # Collection is staged in memory first.  The crawler may replace
            # items.json only when the same source-health policy that guards
            # dashboard publishing passes, so a rejected hunt leaves both
            # the evidence store and the visible dashboard untouched.
            commit_predicate=lambda summary: _source_health_error(summary) is None,
        )
        if not isinstance(crawl_summary, dict):
            raise RuntimeError("Crawler returned an invalid summary")

        attempted = int(crawl_summary.get("attempted") or 0)
        succeeded = int(crawl_summary.get("succeeded") or 0)
        failed = int(crawl_summary.get("failed") or 0)
        _update_hunt(
            job_id,
            phase="checking_source_health",
            message="Verifying source coverage before updating the dashboard…",
            attempted=attempted,
            completed=attempted,
            total=attempted,
            succeeded=succeeded,
            failed=failed,
            current_source=None,
        )

        health_error = _source_health_error(crawl_summary)
        if health_error:
            raise RuntimeError(health_error)

        views = {}
        # One clock value keeps all five inclusive cutoffs consistent, even if
        # generation happens to cross midnight or an hour boundary.
        pipeline_now = datetime.now(timezone.utc)
        for index, timeframe in enumerate(TIMEFRAME_RANGES):
            _update_hunt(
                job_id,
                phase="ranking_timeframes",
                message=f"Ranking stories for {timeframe}…",
                current_timeframe=timeframe,
                completed_timeframes=index,
            )
            result = engine_module.run_pipeline(timeframe=timeframe, now=pipeline_now)
            if not isinstance(result, tuple) or len(result) < 1:
                raise ValueError(f"Topic Engine returned invalid results for {timeframe}")
            view = dict(result[0]) if isinstance(result[0], dict) else result[0]
            _validate_dashboard_view(view, timeframe)
            # The bundle owns one common publish timestamp.
            view.pop("updated_at", None)
            views[timeframe] = view

        published_at = pipeline_now.isoformat(timespec="seconds")
        bundle = {
            "schema_version": 2,
            "updated_at": published_at,
            "default_range": DEFAULT_TIMEFRAME,
            "timeframes": views,
        }
        _update_hunt(
            job_id,
            phase="publishing",
            message="Publishing the completed dashboard…",
            current_timeframe=None,
            completed_timeframes=len(TIMEFRAME_RANGES),
        )
        # write_data uses a same-directory temporary file + replace, so all
        # five views become visible together or the prior dashboard survives.
        engine_module.write_data(bundle, path=DATA_PATH)

        with _hunt_state_lock:
            if _hunt_state.get("job_id") == job_id:
                elapsed = max(0.0, time.monotonic() - _hunt_state["_started_monotonic"])
                _hunt_state.update(
                    status="succeeded",
                    phase="complete",
                    message="The hunt is complete. Your updated dashboard is ready.",
                    finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    elapsed_seconds=round(elapsed, 1),
                    eta_seconds=0,
                    error=None,
                    dashboard_unchanged=False,
                )
        app.logger.info("Hunt completed — %s", crawl_summary)
    except Exception as exc:
        app.logger.exception("Hunt failed")
        safe_error = str(exc) if str(exc).startswith("Source health check failed:") else "The hunt could not be completed."
        with _hunt_state_lock:
            if _hunt_state.get("job_id") == job_id:
                started = _hunt_state.get("_started_monotonic")
                elapsed = max(0.0, time.monotonic() - started) if started is not None else 0.0
                _hunt_state.update(
                    status="failed",
                    phase="failed",
                    message="The hunt stopped. Your previous dashboard is unchanged.",
                    finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    elapsed_seconds=round(elapsed, 1),
                    eta_seconds=None,
                    error=safe_error,
                    dashboard_unchanged=True,
                )


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Start exactly one manual background hunt and return immediately."""
    global _hunt_thread, _hunt_state

    with _hunt_state_lock:
        if _hunt_state["status"] == "running":
            payload = _status_snapshot_locked()
            payload.update(success=False, error="A hunt is already in progress.")
            return jsonify(payload), 409

        job_id = uuid.uuid4().hex
        _hunt_state = _idle_hunt_state()
        _hunt_state.update(
            job_id=job_id,
            status="running",
            phase="starting",
            message="Preparing to hunt for new local stories…",
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            _started_monotonic=time.monotonic(),
        )
        _hunt_thread = threading.Thread(
            target=_run_hunt,
            args=(job_id,),
            name=f"news-hunt-{job_id[:8]}",
            daemon=True,
        )
        _hunt_thread.start()
        payload = _status_snapshot_locked()

    payload["success"] = True
    return jsonify(payload), 202


@app.route("/api/refresh/status")
def api_refresh_status():
    return jsonify(get_hunt_status())


if __name__ == "__main__":
    # threaded=True so a long-running /api/refresh doesn't block the whole
    # server from handling other requests (e.g. the page itself, or a
    # second refresh click that the lock above needs to actually reject).
    app.run(host="127.0.0.1", port=PORT, debug=True, threaded=True)
