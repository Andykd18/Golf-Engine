"""
Golf Pricing Engine — Flask Backend
Uses ESPN unofficial API for player scores/results and The Odds API for markets.
Calculates DIY strokes gained by comparing player scores vs field average.
"""

import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app, origins="*")

ODDS_API_KEY  = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ESPN_BASE     = "https://site.web.api.espn.com/apis/site/v2/sports/golf"
ESPN_CORE     = "https://sports.core.api.espn.com/v2/sports/golf/leagues"

# Event tier weights for field strength adjustment
EVENT_TIER = {
    "masters": 1.5,
    "the masters": 1.5,
    "u.s. open": 1.5,
    "us open": 1.5,
    "open championship": 1.5,
    "the open": 1.5,
    "pga championship": 1.5,
    "the players": 1.3,
    "players championship": 1.3,
    "genesis": 1.2,
    "arnold palmer": 1.2,
    "memorial": 1.2,
    "travelers": 1.2,
    "rbc canadian": 1.2,
    "scottish open": 1.2,
    "bmw pga": 1.2,
}

def get_event_tier(event_name):
    name_lower = event_name.lower()
    for key, weight in EVENT_TIER.items():
        if key in name_lower:
            return weight
    return 1.0


# ── ESPN helpers ─────────────────────────────────────────

def espn_get(url, params=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError:
        print(f"[espn_get] HTTP {resp.status_code} for {resp.url}")
        return None
    except Exception as e:
        print(f"[espn_get] {type(e).__name__}: {e} — url={url} params={params}")
        return None


def get_upcoming_events(tour="pga"):
    """Get upcoming PGA/Euro Tour events."""
    data = espn_get(f"{ESPN_BASE}/{tour}/scoreboard")
    if not data:
        return []

    events = []
    for event in data.get("events", []):
        status = event.get("status", {}).get("type", {}).get("name", "")
        events.append({
            "id":     event.get("id"),
            "name":   event.get("name"),
            "date":   event.get("date"),
            "venue":  event.get("venue", {}).get("fullName", ""),
            "city":   event.get("venue", {}).get("address", {}).get("city", ""),
            "status": status,
            "tour":   tour,
        })
    return events


def _as_dict(value):
    """ESPN sometimes returns 'score'/'status' as a nested dict, and sometimes
    (usually for events that haven't teed off yet) as a plain string like '-'.
    Normalize so downstream .get() calls never blow up either way."""
    return value if isinstance(value, dict) else {}


def get_event_field(event_id, tour="pga"):
    """Get full field for an event with current scores."""
    data = espn_get(f"{ESPN_BASE}/{tour}/leaderboard/{event_id}")

    if not data:
        # /leaderboard/{id} 404s for events ESPN hasn't finalized the field for yet
        # (common when the tournament is still a week+ out). Fall back to the
        # scoreboard listing, which tends to carry the event earlier.
        print(f"[get_event_field] leaderboard miss for {event_id}, falling back to scoreboard")
        sb = espn_get(f"{ESPN_BASE}/{tour}/scoreboard")
        if not sb:
            return [], 0
        data = next(
            (e for e in sb.get("events", []) if str(e.get("id")) == str(event_id)),
            None
        )
        if not data:
            return [], 0
        data = {"events": [data]}  # normalize to the shape the code below expects

    players = []
    leaderboard = data.get("events", [{}])[0].get("competitions", [{}])[0].get("competitors", [])

    scores = []
    for comp in leaderboard:
        score_to_par = _as_dict(comp.get("score")).get("value")
        if score_to_par is not None:
            try:
                scores.append(float(score_to_par))
            except:
                pass

    field_avg = sum(scores) / len(scores) if scores else 0

    for comp in leaderboard:
        athlete = comp.get("athlete", {})
        status = _as_dict(comp.get("status"))
        players.append({
            "id":           athlete.get("id"),
            "name":         athlete.get("displayName"),
            "country":      athlete.get("flag", {}).get("alt", ""),
            "world_ranking": status.get("rank"),
            "score_to_par": _as_dict(comp.get("score")).get("displayValue", "E"),
            "position":     _as_dict(status.get("position")).get("displayName", ""),
        })

    return players, field_avg


def get_player_recent_results(player_id, tour="pga", last_n=5):
    """
    Get player's last N tournament results from ESPN event log.
    Returns list of {event_name, score_to_par, field_avg, position, tier_weight}
    """
    season = 2026
    data = espn_get(
        f"{ESPN_CORE}/{tour}/seasons/{season}/athletes/{player_id}/eventlog",
        params={"lang": "en", "region": "us"}
    )

    if not data:
        return []

    results = []
    events = data.get("events", {}).get("items", [])

    for event_ref in events:
        # Each item is a reference URL — fetch it
        event_url = event_ref.get("$ref")
        if not event_url:
            continue

        event_data = espn_get(event_url)
        if not event_data:
            continue

        # Check event is completed
        status = event_data.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("completed", False)
        if not status:
            continue

        event_name = event_data.get("name", "")
        tier_weight = get_event_tier(event_name)

        # Get player's score from the leaderboard
        competitors = event_data.get("competitions", [{}])[0].get("competitors", [])
        all_scores = []
        player_score = None
        player_position = None

        for comp in competitors:
            score_val = None
            try:
                score_val = float(comp.get("score", {}).get("value", 0))
                all_scores.append(score_val)
            except:
                pass

            if str(comp.get("athlete", {}).get("id")) == str(player_id):
                player_score = score_val
                player_position = comp.get("status", {}).get("position", {}).get("displayName", "")

        if player_score is None or not all_scores:
            continue

        field_avg = sum(all_scores) / len(all_scores)
        sg_vs_field = field_avg - player_score  # positive = better than field

        results.append({
            "event_name":   event_name,
            "event_id":     event_data.get("id"),
            "score_to_par": player_score,
            "field_avg":    round(field_avg, 2),
            "sg_vs_field":  round(sg_vs_field, 2),
            "position":     player_position,
            "tier_weight":  tier_weight,
        })

        if len(results) >= last_n:
            break

        time.sleep(0.3)

    return results


def calculate_player_rating(results):
    """
    Weighted average of SG vs field over last N events.
    More recent = higher weight, higher tier = higher weight.
    """
    if not results:
        return None

    n = len(results)
    recency_weights = list(range(1, n + 1))  # [1, 2, ..., n] oldest to newest

    total_weight = 0
    weighted_sum = 0

    for i, result in enumerate(results):
        recency_w = recency_weights[i]
        tier_w    = result["tier_weight"]
        combined  = recency_w * tier_w
        weighted_sum += result["sg_vs_field"] * combined
        total_weight += combined

    if total_weight == 0:
        return None

    return round(weighted_sum / total_weight, 3)


# ── Odds API ─────────────────────────────────────────────

def get_golf_odds(tournament_name=""):
    """Fetch golf outright odds from The Odds API."""
    if not ODDS_API_KEY:
        return {"error": "No ODDS_API_KEY set"}

    sport_keys = ["golf_pga_championship", "golf_masters_tournament_winner",
                  "golf_us_open_winner", "golf_the_open_championship_winner",
                  "golf_pga_tour_winner"]

    for sport_key in sport_keys:
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                params={
                    "apiKey":     ODDS_API_KEY,
                    "regions":    "uk",
                    "markets":    "outrights",
                    "oddsFormat": "decimal",
                },
                timeout=15
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            events = resp.json()
            if events:
                # Find matching tournament
                for event in events:
                    if not tournament_name or tournament_name.lower() in event.get("sport_title", "").lower():
                        bookmakers = event.get("bookmakers", [])
                        if not bookmakers:
                            continue
                        preferred = ["paddypower", "bet365", "williamhill", "betfair_ex_eu"]
                        bm = next((b for b in bookmakers if b["key"] in preferred), bookmakers[0])
                        markets = {m["key"]: m for m in bm.get("markets", [])}
                        outrights = markets.get("outrights", {})
                        outcomes = outrights.get("outcomes", [])
                        return {
                            "bookmaker": bm["title"],
                            "sport_key": sport_key,
                            "players": [
                                {"name": o["name"], "odds": o["price"]}
                                for o in outcomes
                            ]
                        }
        except Exception:
            continue

    return {"error": "No golf odds found — tournament may not be available yet"}


# ── Routes ───────────────────────────────────────────────

@app.route("/")
def index():
    return "OK", 200


@app.route("/api/events")
def api_events():
    """Return upcoming PGA and DP World Tour events."""
    try:
        pga_events  = get_upcoming_events("pga")
        euro_events = get_upcoming_events("eur")
        all_events  = pga_events + euro_events
        # Sort by date
        all_events.sort(key=lambda x: x.get("date", ""))
        return jsonify({"events": all_events[:20]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/field")
def api_field():
    """Return field for a given event."""
    event_id = request.args.get("event_id", "")
    tour     = request.args.get("tour", "pga")
    if not event_id:
        return jsonify({"error": "Provide event_id"}), 400
    try:
        players, field_avg = get_event_field(event_id, tour)
        return jsonify({"players": players, "field_avg": field_avg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/player")
def api_player():
    """Return player rating based on last N results."""
    player_id   = request.args.get("player_id", "")
    player_name = request.args.get("name", "")
    tour        = request.args.get("tour", "pga")
    last_n      = int(request.args.get("last_n", 5))

    if not player_id:
        return jsonify({"error": "Provide player_id"}), 400

    try:
        results = get_player_recent_results(player_id, tour, last_n)
        rating  = calculate_player_rating(results)

        return jsonify({
            "player_id":   player_id,
            "player_name": player_name,
            "rating":      rating,
            "results":     results,
            "events_used": len(results),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/odds")
def api_odds():
    """Return golf outright odds."""
    tournament = request.args.get("tournament", "")
    try:
        return jsonify(get_golf_odds(tournament))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analyse")
def api_analyse():
    """
    Analyse full field for an event.
    Returns all players with their rating and bookmaker odds for value comparison.
    """
    event_id = request.args.get("event_id", "")
    tour     = request.args.get("tour", "pga")
    last_n   = int(request.args.get("last_n", 5))

    if not event_id:
        return jsonify({"error": "Provide event_id"}), 400

    try:
        players, field_avg = get_event_field(event_id, tour)

        # Get event name for odds lookup
        event_data = espn_get(f"{ESPN_BASE}/{tour}/leaderboard/{event_id}")
        event_name = event_data.get("events", [{}])[0].get("name", "") if event_data else ""

        # Get odds
        odds_data = get_golf_odds(event_name)
        odds_map  = {}
        if "players" in odds_data:
            for p in odds_data["players"]:
                odds_map[p["name"].lower()] = p["odds"]

        # Calculate ratings for each player
        results = []
        for player in players[:50]:  # Limit to top 50 to avoid rate limits
            pid  = player.get("id")
            name = player.get("name", "")
            if not pid:
                continue

            recent = get_player_recent_results(pid, tour, last_n)
            rating = calculate_player_rating(recent)

            # Match odds
            player_odds = None
            for odds_name, odds_val in odds_map.items():
                if any(part in odds_name for part in name.lower().split()):
                    player_odds = odds_val
                    break

            results.append({
                "player_id":    pid,
                "name":         name,
                "country":      player.get("country", ""),
                "rating":       rating,
                "events_used":  len(recent),
                "recent":       recent,
                "odds":         player_odds,
                "implied_prob": round(1 / player_odds * 100, 2) if player_odds else None,
            })

            time.sleep(0.5)

        # Sort by rating descending
        results.sort(key=lambda x: x["rating"] or -99, reverse=True)

        return jsonify({
            "event_name": event_name,
            "tour":       tour,
            "players":    results,
            "odds_source": odds_data.get("bookmaker", ""),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("\n  Golf Pricing Engine starting...\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
