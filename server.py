"""
World Cup 2026 Pricing Engine — Flask Backend
Serves xG data from API-Football and match odds from The Odds API.
"""

import time
import os
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app, origins="*")

# ── Config ──────────────────────────────────────────────
ODDS_API_KEY     = os.environ.get("ODDS_API_KEY", "")
RAPIDAPI_KEY     = os.environ.get("RAPIDAPI_KEY", "")
ODDS_API_BASE    = "https://api.the-odds-api.com/v4"
APIFOOTBALL_BASE = "https://v3.football.api-sports.io"
WC_LEAGUE_ID     = 1
SEASON           = 2026

# ── Team IDs (API-Football national team IDs) ────────────
# These are fetched dynamically via /api/teams so we use a
# lookup cache rather than hardcoding all 48
TEAM_ID_CACHE = {}

def _headers():
    return {"x-apisports-key": RAPIDAPI_KEY}


def get_team_id(team_name):
    """Look up API-Football team ID for a national team."""
    if team_name in TEAM_ID_CACHE:
        return TEAM_ID_CACHE[team_name]

    resp = requests.get(
        f"{APIFOOTBALL_BASE}/teams",
        headers=_headers(),
        params={"name": team_name, "league": WC_LEAGUE_ID, "season": SEASON},
        timeout=15
    )
    teams = resp.json().get("response", [])
    if teams:
        team_id = teams[0]["team"]["id"]
        TEAM_ID_CACHE[team_name] = team_id
        return team_id

    # Fallback: search by name without league filter
    resp2 = requests.get(
        f"{APIFOOTBALL_BASE}/teams",
        headers=_headers(),
        params={"search": team_name},
        timeout=15
    )
    teams2 = resp2.json().get("response", [])
    # Filter to national teams only
    national = [t for t in teams2 if t["team"].get("national") is True]
    if national:
        team_id = national[0]["team"]["id"]
        TEAM_ID_CACHE[team_name] = team_id
        return team_id

    return None


def get_team_xg(team_name, last_n=10):
    """Fetch weighted average xG for a national team from recent fixtures."""
    team_id = get_team_id(team_name)
    if not team_id:
        raise ValueError(f"Could not find team ID for '{team_name}'.")

    # Fetch recent completed internationals — use last_n * 3 to allow venue filtering
    resp = requests.get(
        f"{APIFOOTBALL_BASE}/fixtures",
        headers=_headers(),
        params={"team": team_id, "last": 20},
        timeout=15
    )
    resp.raise_for_status()
    fixtures = resp.json().get("response", [])

    if not fixtures:
        raise ValueError(f"No recent fixtures found for {team_name}.")

    fixtures = fixtures[-last_n:] if len(fixtures) > last_n else fixtures

    xg_for_vals     = []
    xg_against_vals = []
    corner_for_vals     = []
    corner_against_vals = []
    yellow_for_vals     = []
    red_for_vals        = []
    shots_for_vals      = []
    fouls_for_vals      = []

    for fixture in fixtures:
        fixture_id = fixture.get("fixture", {}).get("id")
        teams      = fixture.get("teams", {})
        is_home    = teams.get("home", {}).get("id") == team_id

        stats_resp = requests.get(
            f"{APIFOOTBALL_BASE}/fixtures/statistics",
            headers=_headers(),
            params={"fixture": fixture_id},
            timeout=15
        )
        all_stats = stats_resp.json().get("response", [])

        team_xg = opp_xg = team_corners = opp_corners = None
        team_yellow = team_red = team_shots = team_fouls = None

        for team_stats in all_stats:
            tid = team_stats.get("team", {}).get("id")
            for stat in team_stats.get("statistics", []):
                stype = stat.get("type")
                val   = stat.get("value")
                def fval(v):
                    try: return float(v) if v and str(v) not in ("None", "") else None
                    except: return None

                if stype in ("Expected Goals", "expected_goals"):
                    if tid == team_id: team_xg = fval(val)
                    else:              opp_xg  = fval(val)
                elif stype == "Corner Kicks":
                    if tid == team_id: team_corners = fval(val)
                    else:              opp_corners  = fval(val)
                elif stype == "Yellow Cards":
                    if tid == team_id: team_yellow = fval(val)
                elif stype == "Red Cards":
                    if tid == team_id: team_red = fval(val)
                elif stype == "Total Shots":
                    if tid == team_id: team_shots = fval(val)
                elif stype == "Fouls":
                    if tid == team_id: team_fouls = fval(val)

        if team_xg is not None and opp_xg is not None:
            xg_for_vals.append(team_xg)
            xg_against_vals.append(opp_xg)
        if team_corners is not None and opp_corners is not None:
            corner_for_vals.append(team_corners)
            corner_against_vals.append(opp_corners)
        if team_yellow is not None: yellow_for_vals.append(team_yellow)
        if team_red    is not None: red_for_vals.append(team_red)
        if team_shots  is not None: shots_for_vals.append(team_shots)
        if team_fouls  is not None: fouls_for_vals.append(team_fouls)

        time.sleep(0.5)

    # If fewer than 3 games have xG data, treat as unreliable and use goals fallback
    if len(xg_for_vals) < 3:
        xg_for_vals = []
        xg_against_vals = []

    if not xg_for_vals:
        # Fallback: use goals scored/conceded as proxy for xG
        goals_for_vals     = []
        goals_against_vals = []
        for fixture in fixtures:
            teams  = fixture.get("teams", {})
            goals  = fixture.get("goals", {})
            is_home = teams.get("home", {}).get("id") == team_id
            if is_home:
                gf = goals.get("home")
                ga = goals.get("away")
            else:
                gf = goals.get("away")
                ga = goals.get("home")
            if gf is not None and ga is not None:
                goals_for_vals.append(float(gf))
                goals_against_vals.append(float(ga))

        if not goals_for_vals:
            raise ValueError(f"No data found for {team_name}.")

        def weighted_avg_goals(lst):
            if not lst: return None
            n = len(lst)
            weights = list(range(1, n + 1))
            total_weight = sum(weights)
            return round(sum(v * w for v, w in zip(lst, weights)) / total_weight, 3)

        def avg(lst):
            return round(sum(lst) / len(lst), 2) if lst else None

        return {
            "team":                 team_name,
            "xg_for":               weighted_avg_goals(goals_for_vals),
            "xg_against":           weighted_avg_goals(goals_against_vals),
            "matches_used":         len(goals_for_vals),
            "fallback":             True,
            "corners_for":          avg(corner_for_vals),
            "corners_against":      avg(corner_against_vals),
            "yellow_cards_for":     avg(yellow_for_vals),
            "red_cards_for":        avg(red_for_vals),
            "shots_for":            avg(shots_for_vals),
            "fouls_for":            avg(fouls_for_vals),
        }

    def weighted_avg(lst):
        """More recent games carry higher weight."""
        if not lst: return None
        n = len(lst)
        weights = list(range(1, n + 1))
        total_weight = sum(weights)
        return round(sum(v * w for v, w in zip(lst, weights)) / total_weight, 3)

    def avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else None

    return {
        "team":                 team_name,
        "xg_for":               weighted_avg(xg_for_vals),
        "xg_against":           weighted_avg(xg_against_vals),
        "matches_used":         len(xg_for_vals),
        "fallback":             False,
        "corners_for":          avg(corner_for_vals),
        "corners_against":      avg(corner_against_vals),
        "yellow_cards_for":     avg(yellow_for_vals),
        "red_cards_for":        avg(red_for_vals),
        "shots_for":            avg(shots_for_vals),
        "fouls_for":            avg(fouls_for_vals),
    }


# ── Odds API ─────────────────────────────────────────────

def get_match_odds(home_team, away_team):
    if not ODDS_API_KEY:
        return {"error": "No API key set. Add ODDS_API_KEY to Railway Variables."}

    # Try FIFA World Cup sport key
    for sport_key in ["soccer_fifa_world_cup", "soccer_world_cup"]:
        try:
            url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
            params = {
                "apiKey":     ODDS_API_KEY,
                "regions":    "uk",
                "markets":    "h2h",
                "oddsFormat": "decimal",
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            events = resp.json()

            home_search = home_team.lower()
            away_search = away_team.lower()

            for event in events:
                ht = event.get("home_team", "").lower()
                at = event.get("away_team", "").lower()

                if (home_search in ht or ht in home_search) and \
                   (away_search in at or at in away_search):

                    bookmakers = event.get("bookmakers", [])
                    if not bookmakers:
                        continue

                    preferred = ["betfair_ex_eu", "bet365", "williamhill", "paddypower"]
                    bm = next(
                        (b for b in bookmakers if b["key"] in preferred),
                        bookmakers[0]
                    )

                    markets  = {m["key"]: m for m in bm.get("markets", [])}
                    h2h      = markets.get("h2h", {})
                    outcomes = {o["name"].lower(): o["price"] for o in h2h.get("outcomes", [])}

                    home_odds = outcomes.get(ht) or outcomes.get(home_search)
                    away_odds = outcomes.get(at) or outcomes.get(away_search)
                    draw_odds = outcomes.get("draw")

                    return {
                        "home_team":  event["home_team"],
                        "away_team":  event["away_team"],
                        "home_odds":  home_odds,
                        "draw_odds":  draw_odds,
                        "away_odds":  away_odds,
                        "bookmaker":  bm["title"],
                        "commence":   event.get("commence_time", ""),
                    }
        except Exception:
            continue

    return {"error": f"No fixture found for {home_team} vs {away_team}. May not be scheduled yet."}


# ── Routes ───────────────────────────────────────────────

@app.route("/")
def index():
    return "OK", 200


@app.route("/api/fixtures")
def api_fixtures():
    """Return upcoming World Cup fixtures."""
    if not RAPIDAPI_KEY:
        return jsonify({"error": "RAPIDAPI_KEY not set."})
    try:
        resp = requests.get(
            f"{APIFOOTBALL_BASE}/fixtures",
            headers=_headers(),
            params={"league": WC_LEAGUE_ID, "season": SEASON, "next": 20},
            timeout=15
        )
        resp.raise_for_status()
        fixtures = resp.json().get("response", [])

        result = []
        for f in fixtures:
            home = f.get("teams", {}).get("home", {})
            away = f.get("teams", {}).get("away", {})
            result.append({
                "fixture_id": f.get("fixture", {}).get("id"),
                "home":       home.get("name"),
                "away":       away.get("name"),
                "date":       f.get("fixture", {}).get("date", ""),
                "venue":      f.get("fixture", {}).get("venue", {}).get("name", ""),
                "city":       f.get("fixture", {}).get("venue", {}).get("city", ""),
            })

        return jsonify({"fixtures": result})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/xg")
def api_xg():
    home   = request.args.get("home", "")
    away   = request.args.get("away", "")
    last_n = int(request.args.get("last_n", 10))

    if not home or not away:
        return jsonify({"error": "Provide home and away team names"}), 400

    try:
        home_data = get_team_xg(home, last_n=last_n)
        away_data = get_team_xg(away, last_n=last_n)

        home_lambda = round((home_data["xg_for"] + away_data["xg_against"]) / 2, 3)
        away_lambda = round((away_data["xg_for"]  + home_data["xg_against"]) / 2, 3)

        return jsonify({
            "home":        home_data,
            "away":        away_data,
            "home_lambda": home_lambda,
            "away_lambda": away_lambda,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/odds")
def api_odds():
    home = request.args.get("home", "")
    away = request.args.get("away", "")

    if not home or not away:
        return jsonify({"error": "Provide home and away team names"}), 400

    try:
        return jsonify(get_match_odds(home, away))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/match")
def api_match():
    home   = request.args.get("home", "")
    away   = request.args.get("away", "")
    last_n = int(request.args.get("last_n", 10))

    if not home or not away:
        return jsonify({"error": "Provide home and away team names"}), 400

    result = {}

    try:
        home_data   = get_team_xg(home, last_n=last_n)
        away_data   = get_team_xg(away, last_n=last_n)
        home_lambda = round((home_data["xg_for"] + away_data["xg_against"]) / 2, 3)
        away_lambda = round((away_data["xg_for"]  + home_data["xg_against"]) / 2, 3)
        result["xg"] = {
            "home":        home_data,
            "away":        away_data,
            "home_lambda": home_lambda,
            "away_lambda": away_lambda,
        }
    except Exception as e:
        result["xg"] = {"error": str(e)}

    try:
        result["odds"] = get_match_odds(home, away)
    except Exception as e:
        result["odds"] = {"error": str(e)}

    return jsonify(result)


if __name__ == "__main__":
    print("\n  WC 2026 Pricing Engine starting...\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
