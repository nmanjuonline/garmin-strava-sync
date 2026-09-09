import os
import json
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from garminconnect import Garmin

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# Config (all via environment variables — see .env.example)
# ---------------------------------------------------------------------------
GARMIN_EMAIL = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]

STRAVA_CLIENT_ID = os.environ["STRAVA_CLIENT_ID"]
STRAVA_CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
STRAVA_REFRESH_TOKEN = os.environ["STRAVA_REFRESH_TOKEN"]

# Optional mapping of Garmin gear display name -> Strava gear_id
# e.g. {"Nike Pegasus 40": "g12345678"}
GEAR_MAP = json.loads(os.environ.get("GEAR_MAP_JSON", "{}"))

MATCH_TOLERANCE_MINUTES = int(os.environ.get("MATCH_TOLERANCE_MINUTES", "10"))

# ---------------------------------------------------------------------------
# Garmin client (session cached in memory for the life of the process)
# ---------------------------------------------------------------------------
_garmin_client = None


def get_garmin_client():
    global _garmin_client
    if _garmin_client is None:
        _garmin_client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        _garmin_client.login()
    return _garmin_client


def get_garmin_activity_gear(client, activity_id):
    """Low-level call — Garmin's unofficial API, endpoint may change over time.
    See: connect.garmin.com/modern/proxy/gear-service/gear/filterGear?activityId=...
    """
    try:
        gear_data = client.garth.connectapi(
            "/gear-service/gear/filterGear", params={"activityId": activity_id}
        )
        return [g.get("displayName") or g.get("customMakeModel") for g in (gear_data or [])]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Strava client helpers
# ---------------------------------------------------------------------------
_strava_token_cache = {"access_token": None, "expires_at": 0}


def get_strava_access_token():
    if _strava_token_cache["access_token"] and _strava_token_cache["expires_at"] > time.time() + 60:
        return _strava_token_cache["access_token"]

    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "refresh_token": STRAVA_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _strava_token_cache["access_token"] = data["access_token"]
    _strava_token_cache["expires_at"] = data["expires_at"]
    return data["access_token"]


def strava_headers():
    return {"Authorization": f"Bearer {get_strava_access_token()}"}


def fetch_strava_activities(days):
    after_ts = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    resp = requests.get(
        "https://www.strava.com/api/v3/athlete/activities",
        headers=strava_headers(),
        params={"after": after_ts, "per_page": 100},
        timeout=15,
    )
    resp.raise_for_status()
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "start_date": a["start_date_local"],
            "type": a["type"],
            "distance": a.get("distance"),
        }
        for a in resp.json()
    ]


def find_matching_strava_activity(garmin_start_time_local, strava_activities):
    g_time = datetime.fromisoformat(garmin_start_time_local)
    best, best_diff = None, None
    for s in strava_activities:
        s_time = datetime.fromisoformat(s["start_date"].replace("Z", ""))
        diff = abs((g_time - s_time).total_seconds())
        if diff <= MATCH_TOLERANCE_MINUTES * 60:
            if best_diff is None or diff < best_diff:
                best, best_diff = s, diff
    return best


def resolve_gear_id(gear_names):
    for name in gear_names:
        if name in GEAR_MAP:
            return GEAR_MAP[name]
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/activities")
def list_garmin_activities():
    """List recent Garmin activities with name + gear, for the UI to display."""
    limit = int(request.args.get("limit", 20))
    # client = get_garmin_client()
    # activities = client.get_activities(0, limit)
    activities = fetch_strava_activities(3)

    result = []
    for act in activities:
        activity_id = act.get("activityId")
        gear_names = get_garmin_activity_gear(client, activity_id)
        result.append(
            {
                "id": activity_id,
                "name": act.get("activityName"),
                "startTimeLocal": act.get("startTimeLocal"),
                "type": (act.get("activityType") or {}).get("typeKey"),
                "distanceMeters": act.get("distance"),
                "gear": gear_names,
            }
        )
    return jsonify(result)


@app.route("/api/strava/gear")
def list_strava_gear():
    """List the user's registered Strava gear, to help build GEAR_MAP_JSON."""
    resp = requests.get("https://www.strava.com/api/v3/athlete", headers=strava_headers(), timeout=15)
    resp.raise_for_status()
    athlete = resp.json()
    gear = (athlete.get("shoes") or []) + (athlete.get("bikes") or [])
    return jsonify([{"id": g["id"], "name": g["name"]} for g in gear])


@app.route("/api/sync", methods=["POST"])
def sync_activities():
    """
    Body: { "garmin_activity_ids": [123, 456], "strava_lookback_days": 14 }
    For each Garmin activity: finds the matching Strava activity by start time,
    then updates its name + gear_id (if a gear mapping exists).
    """
    body = request.get_json(force=True) or {}
    garmin_ids = body.get("garmin_activity_ids", [])
    days = int(body.get("strava_lookback_days", 14))

    if not garmin_ids:
        return jsonify({"error": "garmin_activity_ids is required"}), 400

    client = get_garmin_client()
    # Pull enough recent Garmin activities to cover the requested ids
    all_garmin = client.get_activities(0, 200)
    garmin_by_id = {a["activityId"]: a for a in all_garmin}

    strava_activities = fetch_strava_activities(days)

    results = []
    for gid in garmin_ids:
        garmin_act = garmin_by_id.get(gid)
        if not garmin_act:
            results.append({"garmin_id": gid, "status": "error", "message": "Garmin activity not found in recent history"})
            continue

        gear_names = get_garmin_activity_gear(client, gid)
        match = find_matching_strava_activity(garmin_act["startTimeLocal"], strava_activities)

        if not match:
            results.append({"garmin_id": gid, "status": "error", "message": "No matching Strava activity found within time tolerance"})
            continue

        update = {"name": garmin_act.get("activityName")}
        gear_id = resolve_gear_id(gear_names)
        if gear_id:
            update["gear_id"] = gear_id

        put_resp = requests.put(
            f"https://www.strava.com/api/v3/activities/{match['id']}",
            headers=strava_headers(),
            data=update,
            timeout=15,
        )
        if put_resp.status_code == 200:
            results.append(
                {
                    "garmin_id": gid,
                    "strava_id": match["id"],
                    "status": "ok",
                    "name": update["name"],
                    "gear_id": update.get("gear_id"),
                    "gear_names_seen": gear_names,
                }
            )
        else:
            results.append(
                {
                    "garmin_id": gid,
                    "strava_id": match["id"],
                    "status": "error",
                    "message": put_resp.text,
                }
            )

    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
