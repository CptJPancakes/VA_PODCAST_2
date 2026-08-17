import json
import os
from datetime import datetime

from flask import Flask, jsonify, render_template


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data.json")

# Port 5000 is macOS's default AirPlay Receiver port (hosted by
# ControlCenter.app) and gets intermittently reclaimed by the OS, causing
# the dev server to fail to bind or become unreachable even though this
# app's own process is otherwise healthy — confirmed directly during local
# runtime auditing. 5050 is not a common macOS system service port.
PORT = 5050

app = Flask(__name__)

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

    current_date = datetime.now().strftime("%A, %B %d, %Y")
    return render_template(
        "index.html",
        data=data,
        regions=REGIONS,
        current_date=current_date,
        updated_at=data.get("updated_at", "Not available"),
    )


@app.route("/api/data")
def api_data():
    try:
        data = load_data()
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=True)
