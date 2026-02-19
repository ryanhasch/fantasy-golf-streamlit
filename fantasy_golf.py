"""
Fantasy Golf League Tracker
Run with:  uv run streamlit run fantasy_golf.py
Install:   uv add streamlit pandas requests beautifulsoup4 plotly
Python:    3.8+

Results storage format per golfer:
  { "prize": 1400000, "status": "scored" }
  { "prize": 0,       "status": "cut"    }
  { "prize": 0,       "status": "wd"     }
  { "prize": 0,       "status": "not_entered" }
  { "prize": 0,       "status": "unknown_absent" }  <- needs admin review
"""

import streamlit as st
import json
import os
import re
import requests
import pandas as pd
from bs4 import BeautifulSoup
from collections import defaultdict

DATA_FILE = "fantasy_golf_data.json"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard"
ESPN_LEADERBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/leaderboard"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

STATUS_EMOJI = {
    "scored":         "💰",
    "cut":            "✂️ CUT",
    "wd":             "🚫 WD/DQ",
    "not_entered":    "—",
    "unknown_absent": "❓ Review",
}

# ── Data persistence ──────────────────────────────────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
        if "tournament_order" not in d:
            d["tournament_order"] = list(d.get("tournaments", {}).keys())
        for t in d.get("tournaments", {}):
            if t not in d["tournament_order"]:
                d["tournament_order"].append(t)
        # Ensure payout keys are always integers (JSON converts them to strings)
        if "live_state" in d and "payout" in d["live_state"]:
            d["live_state"]["payout"] = {int(k): v for k, v in d["live_state"]["payout"].items()}
        return d
    return {"teams": {}, "tournaments": {}, "tournament_order": {}, "live_state": {"payout": {}, "tourney_name": ""}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def save_live_state(data, payout, tourney_name):
    """Persist the live payout to the JSON file so it survives Streamlit restarts."""
    data["live_state"] = {"payout": payout, "tourney_name": tourney_name}
    save_data(data)

def get_prize(entry):
    return entry.get("prize", 0) if isinstance(entry, dict) else entry

def get_status(entry):
    return entry.get("status", "scored") if isinstance(entry, dict) else "scored"

# ── Scrapers ──────────────────────────────────────────────────────────────────

def scrape_pga_payout_table(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    h1 = soup.find("h1")
    title_tag = soup.find("title")
    raw = h1.get_text(strip=True) if h1 else (title_tag.get_text(strip=True) if title_tag else "")
    tourney_name = raw.split("|")[0].strip()[:80]

    payout_map = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        # Auto-detect position column (col 0) and money column (first col with $ signs)
        money_col = None
        for sample_row in rows[1:6]:
            cells = sample_row.find_all(["td", "th"])
            for idx, cell in enumerate(cells):
                txt = cell.get_text(strip=True)
                if "$" in txt or (re.search(r"\d{5,}", txt) and idx > 0):
                    money_col = idx
                    break
            if money_col is not None:
                break

        if money_col is None:
            # Fallback: try column 1 and 2
            for fallback_col in [1, 2]:
                for sample_row in rows[1:6]:
                    cells = sample_row.find_all(["td", "th"])
                    if fallback_col < len(cells):
                        txt = re.sub(r"[^\d]", "", cells[fallback_col].get_text(strip=True))
                        if len(txt) >= 4:
                            money_col = fallback_col
                            break
                if money_col is not None:
                    break

        if money_col is None:
            continue

        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) <= money_col:
                continue
            pos_text = cells[0].get_text(strip=True)
            if pos_text.lower() in ("pos.", "pos", "position", "finish", "#"):
                continue
            # Strip T from tied positions like "T5"
            pos_clean = re.sub(r"[^\d]", "", pos_text)
            try:
                pos = int(pos_clean)
            except ValueError:
                continue
            amount_clean = re.sub(r"[^\d.]", "", cells[money_col].get_text(strip=True))
            try:
                amount = float(amount_clean)
            except ValueError:
                continue
            if amount > 0 and pos not in payout_map:
                payout_map[pos] = amount

        if payout_map:
            break

    if not payout_map:
        raise ValueError(
            "Couldn't parse payout amounts from that URL. "
            "Make sure it's a PGA Tour purse breakdown article with a table showing positions and prize money."
        )
    return payout_map, tourney_name


def scrape_pga_results_article(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    h1 = soup.find("h1")
    title_tag = soup.find("title")
    raw = h1.get_text(strip=True) if h1 else (title_tag.get_text(strip=True) if title_tag else "")
    tourney_name = raw.split("|")[0].strip()[:80]
    players = []
    for table in soup.find_all("table"):
        headers_t = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        has_name = any(h in ("player", "name", "golfer") for h in headers_t)
        has_money = any(h in ("money", "prize", "earnings", "amount", "prize money") for h in headers_t)
        if not (has_name or has_money):
            rows_t = table.find_all("tr")
            sample = " ".join(td.get_text() for r in rows_t[:5] for td in r.find_all("td"))
            if "$" not in sample:
                continue
        rows_t = table.find_all("tr")
        if len(rows_t) < 3:
            continue
        header_row = rows_t[0].find_all(["th", "td"])
        ht = [c.get_text(strip=True).lower() for c in header_row]
        pos_idx = next((i for i, h in enumerate(ht) if h in ("pos", "pos.", "position", "place", "fin", "finish")), None)
        name_idx = next((i for i, h in enumerate(ht) if h in ("player", "name", "golfer", "athlete")), None)
        money_idx = next((i for i, h in enumerate(ht) if h in ("money", "prize", "earnings", "amount", "prize money", "winnings", "purse")), None)
        if pos_idx is None and name_idx is None:
            for sr in rows_t[1:4]:
                cells = sr.find_all("td")
                if len(cells) >= 3:
                    first = cells[0].get_text(strip=True)
                    last = cells[-1].get_text(strip=True)
                    if re.match(r"^T?\d+$", first) and "$" in last:
                        pos_idx, name_idx, money_idx = 0, 1, len(cells) - 1
                        break
        if name_idx is None or money_idx is None:
            continue
        for row in rows_t[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(filter(lambda x: x is not None, [pos_idx, name_idx, money_idx])):
                continue
            name = cells[name_idx].get_text(strip=True) if name_idx < len(cells) else ""
            if not name or name.lower() in ("player", "name", "golfer"):
                continue
            money_clean = re.sub(r"[^\d.]", "", cells[money_idx].get_text(strip=True) if money_idx < len(cells) else "0")
            try:
                prize = float(money_clean)
            except ValueError:
                prize = 0.0
            pos_display, pos_int = "", 999
            if pos_idx is not None and pos_idx < len(cells):
                pos_display = cells[pos_idx].get_text(strip=True)
                try:
                    pos_int = int(re.sub(r"[^\d]", "", pos_display))
                except ValueError:
                    pass
            pd_lower = pos_display.lower()
            if "cut" in pd_lower or "mc" in pd_lower:
                status = "cut"
            elif any(x in pd_lower for x in ("wd", "dq", "w/d", "disq")):
                status = "wd"
            elif prize > 0 or pos_int < 999:
                status = "scored"
            else:
                status = "cut"
            players.append({"name": name, "position": pos_int, "position_display": pos_display, "prize": prize, "status": status})
        if players:
            break
    if not players:
        raise ValueError("Could not find a player results table. Use a 'Points and Payouts' article URL.")
    return players, tourney_name


def scrape_pga_leaderboard_status(url: str):
    """
    Scrape a PGA Tour tournament leaderboard page to get player statuses.
    URL format: pgatour.com/tournaments/2026/{slug}/{event-id}/leaderboard
    Returns dict: {player_name: "cut"|"wd"|"active"} for everyone in the field.
    Players NOT in this dict = not entered.
    """
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    status_map = {}

    # Try embedded JSON first (__NEXT_DATA__ is common in Next.js PGA Tour pages)
    for script in soup.find_all("script", {"id": "__NEXT_DATA__"}):
        try:
            next_data = json.loads(script.string)
            # Walk the JSON tree looking for player/competitor arrays
            def find_players(obj, depth=0):
                if depth > 12 or not obj:
                    return
                if isinstance(obj, list):
                    for item in obj:
                        find_players(item, depth + 1)
                elif isinstance(obj, dict):
                    # Look for objects that have a player name + status
                    name = (obj.get("displayName") or obj.get("playerName") or
                            obj.get("fullName") or obj.get("name") or "")
                    status_raw = (obj.get("status") or obj.get("playerStatus") or
                                  obj.get("tournamentStatus") or "")
                    if isinstance(status_raw, dict):
                        status_raw = status_raw.get("displayText") or status_raw.get("label") or ""
                    if name and isinstance(name, str) and len(name) > 3:
                        s = str(status_raw).lower()
                        if "cut" in s or "mc" in s:
                            status_map[name] = "cut"
                        elif any(x in s for x in ("wd", "withdraw", "dq", "disq")):
                            status_map[name] = "wd"
                        elif name not in status_map:
                            status_map[name] = "active"
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            find_players(v, depth + 1)
            find_players(next_data)
        except Exception:
            pass

    if status_map:
        return status_map

    # Fallback: parse HTML table rows
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 5:
            continue
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            row_text = " ".join(c.get_text(strip=True) for c in cells)
            # Find player name — look for cells that look like names (First Last)
            for cell in cells:
                text = cell.get_text(strip=True)
                # Name-like: two words, each capitalized
                if re.match(r'^[A-Z][a-z]+[ ][A-Z]', text) and len(text) > 5:
                    row_lower = row_text.lower()
                    if "cut" in row_lower:
                        status_map[text] = "cut"
                    elif any(x in row_lower for x in ("wd", "w/d", "dq")):
                        status_map[text] = "wd"
                    else:
                        status_map.setdefault(text, "active")
                    break

    return status_map


def apply_leaderboard_status(results, leaderboard_status_map):
    """
    For any result entry with status 'unknown_absent', look it up in the
    leaderboard status map and set the correct status.
    Players not found in the leaderboard at all = not_entered.
    Returns updated results dict.
    """
    for golfer, entry in results.items():
        if not isinstance(entry, dict) or entry.get("status") != "unknown_absent":
            continue
        lb_status = leaderboard_status_map.get(golfer)
        if lb_status == "cut":
            entry["status"] = "cut"
        elif lb_status == "wd":
            entry["status"] = "wd"
        elif lb_status == "active":
            # They played but earned $0 — treat as cut (made field, didn't cash)
            entry["status"] = "cut"
        else:
            # Not in leaderboard at all = DNP / not entered
            entry["status"] = "not_entered"
    return results


def _score_to_int(score_str):
    """Convert ESPN score string ('E', '-10', '+2', '72') to int for sorting."""
    s = str(score_str).strip()
    if s in ("E", "e", "EVEN", "even", "0"):
        return 0
    try:
        return int(s)
    except ValueError:
        return 9999  # WD/cut/unknown — push to bottom


def fetch_espn_leaderboard():
    raw = None
    for url in [ESPN_SCOREBOARD_URL, ESPN_LEADERBOARD_URL]:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            raw = r.json()
            break
        except Exception:
            continue
    if not raw:
        raise ConnectionError("Could not reach ESPN API.")
    events = raw.get("events", [])
    if not events:
        raise ValueError("No active PGA Tour event found on ESPN right now.")
    event = events[0]
    tournament_name = event.get("name", "Current Tournament") if isinstance(event, dict) else "Current Tournament"
    competitions = event.get("competitions", []) if isinstance(event, dict) else []
    if not competitions:
        raise ValueError("No competition data in ESPN response.")
    comp = competitions[0]
    if not isinstance(comp, dict):
        raise ValueError("Unexpected ESPN response format.")
    status_obj = comp.get("status", {}) if isinstance(comp.get("status"), dict) else {}
    round_num = status_obj.get("period", 0)
    type_obj = status_obj.get("type", {}) if isinstance(status_obj.get("type"), dict) else {}
    status_detail = type_obj.get("detail", "")
    status_message = f"Round {round_num} - {status_detail}" if round_num else status_detail

    players = []
    for c in comp.get("competitors", []):
        if not isinstance(c, dict):
            continue
        athlete = c.get("athlete", {})
        full_name = athlete.get("displayName", "Unknown") if isinstance(athlete, dict) else "Unknown"

        # ── Score ──────────────────────────────────────────────────────────
        score_obj = c.get("score", {})
        score_total = (score_obj.get("displayValue", "E") if isinstance(score_obj, dict)
                       else (score_obj if isinstance(score_obj, str) else "E"))

        # ── Thru ───────────────────────────────────────────────────────────
        linescores = c.get("linescores", [])
        thru_val = ""
        if isinstance(linescores, list) and linescores:
            last = linescores[-1]
            if isinstance(last, dict):
                period = last.get("period", {})
                if isinstance(period, dict):
                    t = period.get("number", "")
                    if t:
                        thru_val = f"Thru {t}"

        # ── Status — check ALL fields ESPN might use ───────────────────────
        c_status = c.get("status", {}) if isinstance(c.get("status"), dict) else {}
        type_obj2 = c_status.get("type", {}) if isinstance(c_status.get("type"), dict) else {}
        # Collect every status string we can find
        status_strings = []
        for field in ("name", "shortText", "description", "detail", "state"):
            val = type_obj2.get(field, "")
            if val:
                status_strings.append(str(val).lower())
        # Also check top-level competitor fields
        for field in ("statusName", "statusText", "statusShortText"):
            val = c.get(field, "")
            if val:
                status_strings.append(str(val).lower())
        # Check active flag — False often means cut/WD
        is_active = c.get("active", True)
        combined_status = " ".join(status_strings)

        if any(x in combined_status for x in ("wd", "withdraw", "withdrew", "dq", "disqualif")):
            espn_status = "wd"
        elif any(x in combined_status for x in ("cut", "mc", "mdf")):
            espn_status = "cut"
        elif not is_active and combined_status:
            # Not active and some non-playing status — treat as cut unless we know otherwise
            espn_status = "cut"
        else:
            espn_status = "active"

        # ── Position from ESPN (often blank — we'll recompute below) ──────
        position_obj = c_status.get("position", {})
        pos_display = (position_obj.get("displayName", "") if isinstance(position_obj, dict)
                       else (position_obj if isinstance(position_obj, str) else ""))

        players.append({
            "name": full_name,
            "position": 999,        # placeholder — computed below
            "position_display": pos_display,
            "score": score_total,
            "score_int": _score_to_int(score_total),
            "thru": thru_val,
            "espn_status": espn_status,
        })

    # ── Compute positions from scores (ESPN position field is unreliable) ──
    # Active players ranked by score; cut/WD pushed to bottom
    active = [p for p in players if p["espn_status"] == "active"]
    inactive = [p for p in players if p["espn_status"] != "active"]

    active.sort(key=lambda x: x["score_int"])
    # Assign positions with tie handling
    rank = 1
    for i, p in enumerate(active):
        if i > 0 and p["score_int"] == active[i-1]["score_int"]:
            p["position"] = active[i-1]["position"]
            p["position_display"] = active[i-1]["position_display"]
        else:
            p["position"] = rank
            p["position_display"] = f"T{rank}" if (
                i + 1 < len(active) and active[i+1]["score_int"] == p["score_int"]
            ) or (i > 0 and active[i-1]["score_int"] == p["score_int"]) else str(rank)
        rank += 1

    # Put cut/WD players at bottom with their original ESPN pos_display if available
    cut_rank = len(active) + 1
    for p in inactive:
        p["position"] = cut_rank
        if not p["position_display"]:
            p["position_display"] = "CUT" if p["espn_status"] == "cut" else "WD"

    all_players = active + inactive
    return all_players, tournament_name, status_message


def espn_status_to_league_status(espn_status, prize):
    """
    Map the espn_status field (now 'wd', 'cut', or 'active') to league status.
    Also handles legacy string values from old ESPN parsing.
    """
    s = str(espn_status).lower()
    if s == "wd" or any(x in s for x in ("withdraw", "withdrew", "dq", "disqualif")):
        return "wd"
    if s == "cut" or any(x in s for x in ("mc", "mdf")):
        return "cut"
    # 'active' or anything else — scored if they have prize money
    return "scored"


def build_results_from_espn(players, payout_map, all_league_golfers=None):
    """
    Saves ALL players from the article with their real status.
    League golfers NOT found in the article get 'unknown_absent' for admin review —
    instead of silently assuming 'not_entered'.
    """
    results = {}
    for p in players:
        pos = p["position"]
        prize = p.get("prize") or payout_map.get(pos, 0.0)
        espn_st = p.get("espn_status") or p.get("status", "")
        results[p["name"]] = {"prize": prize, "status": espn_status_to_league_status(espn_st, prize)}
    if all_league_golfers:
        for golfer in all_league_golfers:
            if golfer not in results:
                results[golfer] = {"prize": 0, "status": "unknown_absent"}
    return results

# ── League logic ───────────────────────────────────────────────────────────────

def get_team_earnings_for_tournament(team_golfers, tournament_results):
    earnings = [(g, get_prize(tournament_results[g])) for g in team_golfers
                if g in tournament_results and get_prize(tournament_results[g]) > 0]
    earnings.sort(key=lambda x: x[1], reverse=True)
    top3 = earnings[:3]
    return sum(e[1] for e in top3), top3

def get_ordered_tournaments(data):
    order = data.get("tournament_order", [])
    all_t = list(data.get("tournaments", {}).keys())
    return order + [t for t in all_t if t not in order]

def compute_standings(data):
    standings = {tn: {"total": 0, "tournaments": {}} for tn in data["teams"]}
    for t_name in get_ordered_tournaments(data):
        results = data["tournaments"].get(t_name, {}).get("results", {})
        for team_name, golfers in data["teams"].items():
            total, top3 = get_team_earnings_for_tournament(golfers, results)
            standings[team_name]["total"] += total
            standings[team_name]["tournaments"][t_name] = {"total": total, "top3": top3}
    return standings

def compute_earnings_history(data):
    """Cumulative prize money per team after each tournament, used for the chart."""
    ordered = get_ordered_tournaments(data)
    if not ordered:
        return {}
    teams = list(data["teams"].keys())
    cumulative = {t: 0.0 for t in teams}
    history = {t: [] for t in teams}
    for t_name in ordered:
        results = data["tournaments"].get(t_name, {}).get("results", {})
        for team_name in teams:
            total, _ = get_team_earnings_for_tournament(data["teams"][team_name], results)
            cumulative[team_name] += total
        sorted_t = sorted(teams, key=lambda t: cumulative[t], reverse=True)
        ranks = {t: i + 1 for i, t in enumerate(sorted_t)}
        for team_name in teams:
            history[team_name].append({"tournament": t_name, "cumulative": cumulative[team_name], "rank": ranks[team_name]})
    return history

def compute_live_team_standings(data, live_payout, live_players):
    # Ensure payout keys are int for consistent lookup
    payout = {int(k): v for k, v in live_payout.items()}
    name_to_prize = {}
    for p in live_players:
        espn_st = p.get("espn_status", "")
        pos = p.get("position", 999)
        if espn_st in ("cut", "wd") or pos == 999:
            name_to_prize[p["name"]] = 0
        else:
            name_to_prize[p["name"]] = payout.get(int(pos), 0.0)
    results = []
    for team_name, golfers in data["teams"].items():
        earnings = [(g, name_to_prize[g]) for g in golfers if g in name_to_prize and name_to_prize[g] > 0]
        earnings.sort(key=lambda x: x[1], reverse=True)
        top3 = earnings[:3]
        results.append((team_name, sum(e[1] for e in top3), top3))
    results.sort(key=lambda x: x[1], reverse=True)
    return results

def get_unowned_golfer_earnings(data):
    """Golfers in tournament results but not on any roster, ranked by total prize."""
    owned = set(g for gs in data["teams"].values() for g in gs)
    earnings = defaultdict(float)
    for t_info in data["tournaments"].values():
        for golfer, entry in t_info.get("results", {}).items():
            if golfer not in owned:
                prize = get_prize(entry)
                if prize > 0:
                    earnings[golfer] += prize
    return sorted(earnings.items(), key=lambda x: x[1], reverse=True)

def fmt_money(val):
    if not val:
        return "$0"
    return f"${float(val):,.0f}"

# ── App setup ─────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Fantasy Golf League", page_icon="🏌️", layout="wide")
st.markdown("""
<style>
    [data-testid="stSidebar"] { background: #1a2e1a; }
    [data-testid="stSidebar"] * { color: #e8f5e0 !important; }
    .big-number { font-size: 2.2rem; font-weight: 700; color: #2d6a2d; }
    .metric-card { background: #f0f7ee; border-left: 4px solid #3a7d3a; border-radius: 6px; padding: 1rem 1.2rem; margin-bottom: 0.5rem; }
    h1, h2, h3 { color: #1a3d1a; }
</style>
""", unsafe_allow_html=True)

for key, default in [("data", None), ("live_players", []), ("live_status", "")]:
    if key not in st.session_state:
        st.session_state[key] = load_data() if key == "data" else default

data = st.session_state.data

# Restore persisted payout from data file on first load
if "live_payout" not in st.session_state:
    ls = data.get("live_state", {})
    st.session_state.live_payout = ls.get("payout", {})
    st.session_state.live_tourney_name = ls.get("tourney_name", "")
if "live_tourney_name" not in st.session_state:
    st.session_state.live_tourney_name = data.get("live_state", {}).get("tourney_name", "")

try:
    ADMIN_PASSWORD = st.secrets["ADMIN_PASSWORD"]
except Exception:
    ADMIN_PASSWORD = "golf2026"

if "is_admin" not in st.session_state:
    st.session_state.is_admin = False

st.sidebar.title("🏌️ Fantasy Golf")
st.sidebar.markdown("---")

if st.session_state.is_admin:
    st.sidebar.success("🔓 Admin Mode")
    if st.sidebar.button("Log Out", key="logout"):
        st.session_state.is_admin = False
        st.rerun()
else:
    with st.sidebar.expander("🔒 Admin Login"):
        pw = st.text_input("Password", type="password", key="admin_pw")
        if st.button("Login", key="admin_login"):
            if pw == ADMIN_PASSWORD:
                st.session_state.is_admin = True
                st.rerun()
            else:
                st.error("Wrong password")

st.sidebar.markdown("---")
page_options = (["🏆 Standings", "🗓️ Tournaments", "👥 Teams", "📊 Player Stats", "🔴 Live Leaderboard", "⚙️ Setup"]
                if st.session_state.is_admin else
                ["🏆 Standings", "🗓️ Tournaments", "👥 Teams", "🔴 Live Leaderboard", "📊 Player Stats"])
page = st.sidebar.radio("Navigate", page_options)

# ─────────────────────────────────────────────
# PAGE: LIVE LEADERBOARD
# ─────────────────────────────────────────────

if page == "🔴 Live Leaderboard":
    st.title("🔴 Live Leaderboard")
    if not data["teams"]:
        st.info("No teams set up yet. Go to ⚙️ Setup first.")
        st.stop()

    mode = (st.radio("Mode", ["📥 Import a completed tournament", "🔴 Track a live/current tournament"],
                     horizontal=True, label_visibility="collapsed")
            if st.session_state.is_admin else "🔴 Track a live/current tournament")
    st.markdown("---")

    # ── MODE A: Import ────────────────────────────────────────────────────────
    if mode == "📥 Import a completed tournament":
        st.subheader("📥 Import Completed Tournament Results")
        st.markdown(
            "Paste the PGA Tour **'Points and Payouts'** article URL:\n\n"
            "`pgatour.com/article/news/betting-dfs/2026/.../points-payouts-...`\n\n"
            "Search: **`site:pgatour.com points payouts [tournament name] 2026`**"
        )
        col_url, col_btn = st.columns([4, 1])
        with col_url:
            results_url = st.text_input("URL", label_visibility="collapsed",
                                        placeholder="https://www.pgatour.com/article/...")
        with col_btn:
            fetch_results = st.button("Fetch", type="primary", key="fetch_results")

        if fetch_results and results_url:
            with st.spinner("Scraping..."):
                try:
                    result_players, tourney_name = scrape_pga_results_article(results_url)
                    st.session_state.live_players = [{**p, "espn_status": p["status"], "score": "", "thru": "F"} for p in result_players]
                    st.session_state.live_tourney_name = tourney_name
                    st.session_state.live_status = "Final"
                    st.session_state.live_payout = {p["position"]: p["prize"] for p in result_players if p["prize"] > 0}
                    st.success(f"Fetched **{tourney_name}** — {len(result_players)} players found.")
                except Exception as e:
                    st.error(f"Failed: {e}")

        if st.session_state.live_players and st.session_state.live_status == "Final":
            st.markdown("---")
            all_golfers = list(set(g for gs in data["teams"].values() for g in gs))
            results_preview = build_results_from_espn(st.session_state.live_players, st.session_state.live_payout, all_golfers)

            unknown_count = sum(1 for v in results_preview.values() if v["status"] == "unknown_absent")
            if unknown_count:
                st.warning(
                    f"⚠️ **{unknown_count} league player(s) weren't found in the payout article** (marked ❓ Review). "
                    "After saving, a form will appear below to classify them as cut, WD, or not entered."
                )

            st.subheader(f"Preview: {st.session_state.live_tourney_name}")
            preview_rows = sorted([{
                "Golfer": g,
                "Team": next((t for t, gs in data["teams"].items() if g in gs), "?"),
                "Status": STATUS_EMOJI.get(results_preview[g]["status"], results_preview[g]["status"]),
                "Prize": results_preview[g]["prize"],
            } for g in sorted(all_golfers)], key=lambda x: x["Prize"], reverse=True)
            st.dataframe(pd.DataFrame(preview_rows).style.format({"Prize": fmt_money}), width="stretch", hide_index=True)

            col1, col2 = st.columns([2, 1])
            with col1:
                import_name = st.text_input("Save as tournament name", value=st.session_state.live_tourney_name, key="import_name_completed")
            with col2:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("💾 Save to Season", type="primary", key="save_completed"):
                    if import_name:
                        if import_name not in data.get("tournament_order", []):
                            data.setdefault("tournament_order", []).append(import_name)
                        data["tournaments"][import_name] = {"results": results_preview}
                        save_data(data)
                        scored = sum(1 for v in results_preview.values() if v["status"] == "scored" and v["prize"] > 0)
                        cut = sum(1 for v in results_preview.values() if v["status"] == "cut")
                        wd = sum(1 for v in results_preview.values() if v["status"] == "wd")
                        not_in = sum(1 for v in results_preview.values() if v["status"] == "not_entered")
                        unk = sum(1 for v in results_preview.values() if v["status"] == "unknown_absent")
                        msg = f"Saved! {scored} scored · {cut} cut · {wd} WD/DQ · {not_in} not in field"
                        if unk:
                            msg += f" · **{unk} need review ⬇️**"
                        st.success(msg)
                        st.rerun()

            # ── Review unknown_absent ─────────────────────────────────────────
            if import_name and import_name in data["tournaments"]:
                saved_results = data["tournaments"][import_name]["results"]
                league_set = set(g for gs in data["teams"].values() for g in gs)
                unknowns = [g for g, v in saved_results.items()
                            if isinstance(v, dict) and v.get("status") == "unknown_absent" and g in league_set]
                if unknowns:
                    st.markdown("---")
                    st.subheader("❓ Classify Missing Players")
                    st.markdown(
                        "These league players weren't in the payout article. "
                        "They could have **missed the cut**, **withdrawn**, or **weren't in the field** at all."
                    )
                    with st.form("review_unknowns"):
                        updates = {}
                        for i in range(0, len(unknowns), 3):
                            rcols = st.columns(3)
                            for j, golfer in enumerate(unknowns[i:i+3]):
                                team = next((t for t, gs in data["teams"].items() if golfer in gs), "?")
                                with rcols[j]:
                                    st.markdown(f"**{golfer}**  \n_{team}_")
                                    updates[golfer] = st.selectbox(
                                        golfer,
                                        ["cut", "not_entered", "wd"],
                                        key=f"rev_{import_name}_{golfer}",
                                        label_visibility="collapsed",
                                        help="cut = was in field but missed cut | not_entered = wasn't in the field | wd = withdrew or DQ'd"
                                    )
                        if st.form_submit_button("✅ Save Classifications", type="primary"):
                            for golfer, status in updates.items():
                                saved_results[golfer]["status"] = status
                            data["tournaments"][import_name]["results"] = saved_results
                            save_data(data)
                            st.success("Saved!")
                            st.rerun()

    # ── MODE B: Live tracking ─────────────────────────────────────────────────
    else:
        st.subheader("🔴 Live Tournament Tracking")
        st.caption("ESPN's API only works for the current active tournament week.")

        st.markdown("**Step 1 — Load Payout Table**")
        if st.session_state.live_payout:
            payout_tn = st.session_state.live_tourney_name or "tournament"
            st.success(f"✅ Payout loaded: **{payout_tn}** ({len(st.session_state.live_payout)} positions, winner: {fmt_money(st.session_state.live_payout.get(1, 0))})")
            col_ep1, col_ep2 = st.columns([3, 1])
            with col_ep1:
                with st.expander("View / change payout table"):
                    st.dataframe(pd.DataFrame([{"Pos": k, "Prize": fmt_money(v)} for k, v in sorted(st.session_state.live_payout.items())]), width="stretch", hide_index=True)
                    new_url = st.text_input("Load a different payout URL", key="replace_payout_url",
                                            placeholder="https://www.pgatour.com/article/news/.../purse-breakdown-...")
                    if st.button("Load new payout", key="replace_payout_btn"):
                        with st.spinner("Scraping..."):
                            try:
                                pm, tn = scrape_pga_payout_table(new_url)
                                pm = {int(k): v for k, v in pm.items()}
                                st.session_state.live_payout = pm
                                st.session_state.live_tourney_name = tn
                                save_live_state(data, pm, tn)
                                st.success(f"Loaded **{tn}**")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")
            with col_ep2:
                if st.button("🗑️ Clear payout", key="clear_payout"):
                    st.session_state.live_payout = {}
                    st.session_state.live_tourney_name = ""
                    save_live_state(data, {}, "")
                    st.rerun()
        else:
            col_url, col_btn = st.columns([4, 1])
            with col_url:
                payout_url = st.text_input("Payout URL", label_visibility="collapsed",
                                           placeholder="https://www.pgatour.com/article/news/.../purse-breakdown-...")
            with col_btn:
                if st.button("Load", type="primary", key="load_payout"):
                    with st.spinner("Scraping..."):
                        try:
                            pm, tn = scrape_pga_payout_table(payout_url)
                            pm = {int(k): v for k, v in pm.items()}
                            st.session_state.live_payout = pm
                            st.session_state.live_tourney_name = tn
                            st.session_state.live_status = ""
                            save_live_state(data, pm, tn)
                            st.success(f"Loaded **{tn}** — winner: {fmt_money(pm.get(1, 0))}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed: {e}")

        st.markdown("**Step 2 — Fetch Live Standings from ESPN**")
        if st.button("🔄 Refresh Leaderboard", type="primary", key="refresh_live"):
            with st.spinner("Fetching..."):
                try:
                    players, t_name, status_msg = fetch_espn_leaderboard()
                    st.session_state.live_players = players
                    st.session_state.live_status = status_msg
                    if not st.session_state.live_tourney_name:
                        st.session_state.live_tourney_name = t_name
                    st.success(f"Fetched {len(players)} players — {status_msg}")
                except Exception as e:
                    st.error(f"Could not fetch: {e}")

        if st.session_state.live_players and st.session_state.live_status != "Final":
            tourney_label = st.session_state.live_tourney_name or "Current Tournament"
            st.markdown("---")
            st.subheader(f"{tourney_label} — {st.session_state.live_status}")

            if st.session_state.is_admin:
                with st.expander("💾 Import as Final Results"):
                    if not st.session_state.live_payout:
                        st.warning("Load payout table first.")
                    else:
                        c1, c2 = st.columns([2, 1])
                        with c1:
                            iname = st.text_input("Save as", value=tourney_label, key="import_live_name")
                        with c2:
                            st.markdown("<br>", unsafe_allow_html=True)
                            if st.button("💾 Save to Season", type="primary", key="save_live"):
                                all_g = list(set(g for gs in data["teams"].values() for g in gs))
                                res = build_results_from_espn(st.session_state.live_players, st.session_state.live_payout, all_g)
                                if iname not in data.get("tournament_order", []):
                                    data.setdefault("tournament_order", []).append(iname)
                                data["tournaments"][iname] = {"results": res}
                                save_data(data)
                                st.success(f"Saved {iname}!")
                                st.rerun()

            tab1, tab2 = st.tabs(["🏆 Team Projections", "📋 Full Leaderboard"])
            with tab1:
                if not st.session_state.live_payout:
                    st.warning("Load payout table to see projected earnings.")
                else:
                    proj = compute_live_team_standings(data, st.session_state.live_payout, st.session_state.live_players)
                    rank_labels = ["🥇", "🥈", "🥉"]
                    cols = st.columns(min(len(proj), 4))
                    for i, (tn, total, _) in enumerate(proj[:4]):
                        with cols[i]:
                            medal = rank_labels[i] if i < 3 else f"#{i+1}"
                            st.markdown(f'<div class="metric-card"><div style="font-size:1rem;font-weight:600;">{medal} {tn}</div><div class="big-number">{fmt_money(total)}</div><div style="font-size:0.8rem;color:#555;">projected</div></div>', unsafe_allow_html=True)
                    espn_by_name = {p["name"]: p for p in st.session_state.live_players}
                    for rank, (tn, total, top3) in enumerate(proj, 1):
                        medal = rank_labels[rank-1] if rank <= 3 else f"#{rank}"
                        with st.expander(f"{medal} {tn} — {fmt_money(total)} projected"):
                            rows = []
                            for g in sorted(data["teams"][tn]):
                                p = espn_by_name.get(g)
                                in_top3 = any(g == t[0] for t in top3)
                                if p:
                                    espn_st = p.get("espn_status", "")
                                    if espn_st == "cut": disp, prize = "✂️ CUT", 0
                                    elif espn_st == "wd": disp, prize = "🚫 WD/DQ", 0
                                    else: disp, prize = "🏌️ Playing", st.session_state.live_payout.get(int(p["position"]), 0)
                                    rows.append({"Golfer": g, "Pos": p["position_display"], "Score": p["score"], "Thru": p["thru"], "Status": disp, "Proj. Prize": prize, "Counts": "✅" if in_top3 else ""})
                                else:
                                    rows.append({"Golfer": g, "Pos": "—", "Score": "—", "Thru": "—", "Status": "Not in field", "Proj. Prize": 0, "Counts": ""})
                            st.dataframe(pd.DataFrame(rows).style.format({"Proj. Prize": fmt_money}), width="stretch", hide_index=True)
                    st.markdown("---")
                    st.subheader("Season If Tournament Ended Now")
                    base = compute_standings(data)
                    combined = sorted([{"Team": tn, "Season So Far": base.get(tn, {}).get("total", 0), "This Event (proj)": pt, "Total": base.get(tn, {}).get("total", 0) + pt} for tn, pt, _ in proj], key=lambda x: x["Total"], reverse=True)
                    for i, row in enumerate(combined):
                        row["Rank"] = rank_labels[i] if i < 3 else f"#{i+1}"
                    st.dataframe(pd.DataFrame(combined)[["Rank","Team","Season So Far","This Event (proj)","Total"]].style.format({c: fmt_money for c in ["Season So Far","This Event (proj)","Total"]}), width="stretch", hide_index=True)
            with tab2:
                all_team_golfers = set(g for gs in data["teams"].values() for g in gs)

                # ── Payout diagnostic ──────────────────────────────────────
                payout = {int(k): v for k, v in st.session_state.live_payout.items()} if st.session_state.live_payout else {}
                if payout:
                    min_pos, max_pos = min(payout), max(payout)
                    n_paying = len(payout)
                    st.caption(f"💰 Payout loaded — covers {n_paying} positions (#{min_pos}–#{max_pos}), winner earns {fmt_money(payout.get(1,0))}")
                else:
                    st.warning("No payout table loaded — proj. prizes will show $0. Go to Step 1 above to load the purse breakdown.")

                lb_rows = []
                for p in st.session_state.live_players:
                    espn_st = p.get("espn_status", "")
                    pos_int = int(p["position"]) if p["position"] != 999 else 999
                    if espn_st == "cut": sd, prize = "✂️ CUT", 0
                    elif espn_st == "wd": sd, prize = "🚫 WD/DQ", 0
                    elif pos_int == 999: sd, prize = "🏌️", 0
                    else:
                        sd = "🏌️"
                        prize = payout.get(pos_int, 0)
                    lb_rows.append({"⭐": "⭐" if p["name"] in all_team_golfers else "", "Pos": p["position_display"], "Player": p["name"], "Score": p["score"], "Thru": p["thru"], "Status": sd, "Proj. Prize": prize})
                st.dataframe(pd.DataFrame(lb_rows).style.format({"Proj. Prize": fmt_money}), width="stretch", hide_index=True)
                st.caption("⭐ = on a league team")

                # Show diagnostic if all prizes are 0 but there are active players
                active_with_prize = sum(1 for r in lb_rows if r["Proj. Prize"] > 0)
                active_playing = sum(1 for r in lb_rows if r["Status"] == "🏌️")
                if payout and active_playing > 0 and active_with_prize == 0:
                    with st.expander("⚠️ All prizes showing $0 — diagnostic info"):
                        espn_positions = sorted(set(int(p["position"]) for p in st.session_state.live_players if p["position"] != 999))
                        payout_positions = sorted(payout.keys())
                        st.markdown(f"**ESPN positions returned:** {espn_positions[:20]}")
                        st.markdown(f"**Payout table positions:** {payout_positions[:20]}")
                        overlap = [p for p in espn_positions if p in payout]
                        st.markdown(f"**Matching positions:** {overlap}")
                        if not overlap:
                            st.error("No overlap between ESPN positions and payout table! The payout URL may be wrong or the article format wasn't parsed correctly. Try reloading the payout table in Step 1.")

# ─────────────────────────────────────────────
# PAGE: STANDINGS
# ─────────────────────────────────────────────

elif page == "🏆 Standings":
    st.title("🏆 Season Standings")
    if not data["teams"]:
        st.info("No teams set up yet. Go to ⚙️ Setup.")
        st.stop()

    standings = compute_standings(data)
    sorted_teams = sorted(standings.items(), key=lambda x: x[1]["total"], reverse=True)
    rank_labels = ["🥇", "🥈", "🥉"]

    cols = st.columns(min(len(sorted_teams), 4))
    for i, (tn, info) in enumerate(sorted_teams[:4]):
        with cols[i]:
            medal = rank_labels[i] if i < 3 else f"#{i+1}"
            st.markdown(f'<div class="metric-card"><div style="font-size:1.1rem;font-weight:600;color:black;">{medal} {tn}</div><div class="big-number">{fmt_money(info["total"])}</div></div>', unsafe_allow_html=True)

    st.markdown("---")
    st.subheader("Full Standings Table")
    ordered_cols = get_ordered_tournaments(data)
    rows = []
    for rank, (tn, info) in enumerate(sorted_teams, 1):
        row = {"Rank": rank, "Team": tn, "Total Earnings": info["total"]}
        for t_name in ordered_cols:
            row[t_name] = info["tournaments"].get(t_name, {}).get("total", 0)
        rows.append(row)
    if rows:
        df = pd.DataFrame(rows)
        money_cols = [c for c in df.columns if c not in ["Rank", "Team"]]
        st.dataframe(df.style.format({c: fmt_money for c in money_cols}), width="stretch", hide_index=True)

    if len(sorted_teams) > 1:
        st.markdown("---")
        st.subheader("Gap to Leader")
        leader_total = sorted_teams[0][1]["total"]
        gap_rows = [{"Rank": rank, "Team": team, "Total": fmt_money(info["total"]),
                     "Gap to Leader": "LEADER" if info["total"] == leader_total else f"-{fmt_money(leader_total - info['total'])}"}
                    for rank, (team, info) in enumerate(sorted_teams, 1)]
        st.dataframe(pd.DataFrame(gap_rows), width="stretch", hide_index=True)

    # ── Earnings Over Time Chart ──────────────────────────────────────────────
    ordered_t = get_ordered_tournaments(data)
    if len(ordered_t) >= 1 and len(data["teams"]) >= 2:
        st.markdown("---")
        st.subheader("📈 Cumulative Prize Money Over Time")
        st.caption("Each line shows a team's running total prize money. Hover for details.")

        history = compute_earnings_history(data)
        teams_by_earnings = [t for t, _ in sorted_teams]
        colors = ["#2d6a2d", "#e07b2a", "#1a6fa8", "#a82828", "#7b3fa8", "#a8963f", "#2a9d8f", "#e63946", "#457b9d", "#f4a261"]
        color_map = {t: colors[i % len(colors)] for i, t in enumerate(teams_by_earnings)}

        try:
            import plotly.graph_objects as go

            fig = go.Figure()
            for team_name in teams_by_earnings:
                events = history[team_name]
                t_labels = [e["tournament"] for e in events]
                earnings_vals = [e["cumulative"] for e in events]
                ranks = [e["rank"] for e in events]
                final_earnings = earnings_vals[-1] if earnings_vals else 0
                final_rank = ranks[-1] if ranks else 0

                fig.add_trace(go.Scatter(
                    x=t_labels,
                    y=earnings_vals,
                    mode="lines+markers+text",
                    name=f"{team_name}",
                    line=dict(color=color_map[team_name], width=3),
                    marker=dict(size=10, color=color_map[team_name]),
                    # Rank + name label at the end of each line
                    text=[""] * (len(t_labels) - 1) + [f"  #{final_rank} {team_name}"],
                    textposition="middle right",
                    textfont=dict(color=color_map[team_name], size=12, family="Arial Black"),
                    hovertext=[
                        f"<b>{team_name}</b><br>After: {t}<br>Total: {fmt_money(e)}<br>Rank: #{r}"
                        for t, e, r in zip(t_labels, earnings_vals, ranks)
                    ],
                    hoverinfo="text",
                ))

            fig.update_layout(
                xaxis=dict(
                    title=dict(text="Tournament", font=dict(size=13)),
                    tickfont=dict(size=12),
                    showgrid=True,
                    gridcolor="#e0ece0",
                ),
                yaxis=dict(
                    title=dict(text="Cumulative Prize Money (USD)", font=dict(size=13)),
                    tickprefix="$",
                    tickformat=",.0f",
                    tickfont=dict(size=11),
                    rangemode="tozero",
                    showgrid=True,
                    gridcolor="#e0ece0",
                ),
                # Legend on right side, ordered by current earnings (top to bottom)
                legend=dict(
                    title=dict(text="Team", font=dict(size=12)),
                    orientation="v",
                    yanchor="top", y=1,
                    xanchor="left", x=1.02,
                    font=dict(size=11),
                    traceorder="normal",
                ),
                height=500,
                # Extra right margin for end-of-line team labels
                margin=dict(l=70, r=200, t=30, b=60),
                plot_bgcolor="#f8fdf6",
                paper_bgcolor="#ffffff",
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True)

        except ImportError:
            st.info("Install plotly: `uv add plotly`")
            pivot = pd.DataFrame([
                {"Tournament": e["tournament"], "Team": tn, "Earnings": e["cumulative"]}
                for tn, evs in history.items() for e in evs
            ]).pivot(index="Tournament", columns="Team", values="Earnings")
            st.dataframe(pivot.style.format(fmt_money), width="stretch")

# ─────────────────────────────────────────────
# PAGE: TOURNAMENTS
# ─────────────────────────────────────────────

elif page == "🗓️ Tournaments":
    st.title("🗓️ Tournaments")

    if not data["tournaments"]:
        st.info("No tournament results yet.")
    else:
        standings = compute_standings(data)
        selected_t = st.selectbox("Select Tournament", get_ordered_tournaments(data))
        if selected_t:
            results = data["tournaments"][selected_t].get("results", {})
            st.subheader(selected_t)
            sorted_teams_t = sorted(data["teams"].items(),
                                    key=lambda x: standings[x[0]]["tournaments"].get(selected_t, {}).get("total", 0), reverse=True)
            rank_labels = ["🥇", "🥈", "🥉"]
            team_cols = st.columns(min(len(data["teams"]), 3))
            for i, (tn, _) in enumerate(sorted_teams_t):
                t_data = standings[tn]["tournaments"].get(selected_t, {})
                medal = rank_labels[i] if i < 3 else f"#{i+1}"
                with team_cols[i % 3]:
                    st.markdown(f"**{medal} {tn}** — {fmt_money(t_data.get('total', 0))}")
                    for g, m in t_data.get("top3", []):
                        st.markdown(f"&nbsp;&nbsp;💰 {g}: {fmt_money(m)}")
                    if not t_data.get("top3"):
                        st.caption("No scoring golfers")
                    st.markdown("---")

            all_golfers_in_league = sorted(set(g for gs in data["teams"].values() for g in gs))

            # Classify unknown_absent players (admins only)
            if st.session_state.is_admin:
                missing = [g for g in all_golfers_in_league if g not in results]
                if missing:
                    st.warning(f"{len(missing)} golfer(s) missing from results: " + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""))
                    if st.button(f"🔄 Recalculate '{selected_t}'", type="primary"):
                        for g in missing:
                            results[g] = {"prize": 0, "status": "not_entered"}
                        data["tournaments"][selected_t]["results"] = results
                        save_data(data)
                        st.success(f"Updated!")
                        st.rerun()

                league_set = set(all_golfers_in_league)
                unknowns = [g for g, v in results.items()
                            if isinstance(v, dict) and v.get("status") == "unknown_absent" and g in league_set]
                if unknowns:
                    st.markdown("---")
                    st.subheader("❓ Classify Missing Players")
                    st.markdown(
                        f"**{len(unknowns)} player(s)** weren't found in the payout article — "
                        "they could have missed the cut, withdrawn, or not been in the field. "
                        "You can auto-classify by linking the tournament leaderboard, or set each one manually below."
                    )

                    # ── Auto-classify via leaderboard URL ─────────────────
                    with st.expander("🔗 Auto-classify using PGA Tour leaderboard (recommended)"):
                        st.markdown(
                            "Paste the tournament leaderboard URL. It shows everyone who played "
                            "and their status (CUT, WD, etc.).\n\n"
                            "Format: `pgatour.com/tournaments/2026/{tournament-name}/{event-id}/leaderboard`"
                        )
                        lb_url_col, lb_btn_col = st.columns([4, 1])
                        with lb_url_col:
                            lb_url = st.text_input(
                                "Leaderboard URL", key=f"lb_url_{selected_t}",
                                label_visibility="collapsed",
                                placeholder="https://www.pgatour.com/tournaments/2026/the-american-express/R2026002/leaderboard"
                            )
                        with lb_btn_col:
                            auto_classify = st.button("Auto-classify", type="primary", key=f"lb_auto_{selected_t}")

                        if auto_classify and lb_url:
                            with st.spinner("Scraping leaderboard..."):
                                try:
                                    lb_status = scrape_pga_leaderboard_status(lb_url)
                                    if not lb_status:
                                        st.error("Couldn't extract player statuses from that page. Check the URL and try manual classification below.")
                                    else:
                                        updated_results = apply_leaderboard_status(dict(results), lb_status)
                                        data["tournaments"][selected_t]["results"] = updated_results
                                        save_data(data)
                                        # Count outcomes
                                        newly_cut = sum(1 for g in unknowns if updated_results.get(g, {}).get("status") == "cut")
                                        newly_wd = sum(1 for g in unknowns if updated_results.get(g, {}).get("status") == "wd")
                                        newly_dnp = sum(1 for g in unknowns if updated_results.get(g, {}).get("status") == "not_entered")
                                        still_unk = sum(1 for g in unknowns if updated_results.get(g, {}).get("status") == "unknown_absent")
                                        st.success(
                                            f"Auto-classified {len(unknowns) - still_unk} players: "
                                            f"{newly_cut} cut · {newly_wd} WD/DQ · {newly_dnp} not entered"
                                            + (f" · {still_unk} still unknown" if still_unk else "")
                                        )
                                        st.rerun()
                                except Exception as e:
                                    st.error(f"Failed to scrape leaderboard: {e}")

                    # ── Manual classification form ─────────────────────────
                    # Re-fetch unknowns in case auto-classify resolved some
                    remaining_unknowns = [g for g, v in results.items()
                                          if isinstance(v, dict) and v.get("status") == "unknown_absent" and g in league_set]
                    if remaining_unknowns:
                        st.markdown("**Manual classification:**")
                        with st.form(f"classify_{selected_t}"):
                            updates = {}
                            for i in range(0, len(remaining_unknowns), 3):
                                rcols = st.columns(3)
                                for j, golfer in enumerate(remaining_unknowns[i:i+3]):
                                    team = next((t for t, gs in data["teams"].items() if golfer in gs), "?")
                                    with rcols[j]:
                                        st.markdown(f"**{golfer}**  \n_{team}_")
                                        updates[golfer] = st.selectbox(
                                            golfer,
                                            ["cut", "not_entered", "wd"],
                                            key=f"cls_{selected_t}_{golfer}",
                                            label_visibility="collapsed",
                                            help="cut = was in field but missed cut | not_entered = wasn't in field | wd = withdrew or DQ'd"
                                        )
                            if st.form_submit_button("✅ Save Manual Classifications", type="primary"):
                                for golfer, status in updates.items():
                                    results[golfer]["status"] = status
                                data["tournaments"][selected_t]["results"] = results
                                save_data(data)
                                st.success("Saved!")
                                st.rerun()

            if results:
                st.markdown("---")
                st.subheader("All Golfer Results")
                res_rows = sorted([{
                    "Golfer": g, "Team": next((t for t, gs in data["teams"].items() if g in gs), "?"),
                    "Status": STATUS_EMOJI.get(get_status(results.get(g, {})), get_status(results.get(g, {}))),
                    "Prize": get_prize(results.get(g, {})),
                } for g in all_golfers_in_league], key=lambda x: x["Prize"], reverse=True)
                st.dataframe(pd.DataFrame(res_rows).style.format({"Prize": fmt_money}), width="stretch", hide_index=True)

            # ── Re-classify tool (always available to admins) ──────────────
            if st.session_state.is_admin:
                st.markdown("---")
                with st.expander("🔗 Re-classify players using PGA Tour leaderboard"):
                    st.markdown(
                        "Use this to auto-set the correct status (cut / WD / not entered) for any "
                        "player in this tournament with $0 prize money. Works on both old and new imports.\n\n"
                        "**URL format:** `pgatour.com/tournaments/2026/{tournament-name}/{event-id}/leaderboard`"
                    )
                    lb_url_col, lb_btn_col = st.columns([4, 1])
                    with lb_url_col:
                        lb_url_t = st.text_input(
                            "Leaderboard URL",
                            key=f"lb_url_any_{selected_t}",
                            label_visibility="collapsed",
                            placeholder="https://www.pgatour.com/tournaments/2026/the-american-express/R2026002/leaderboard"
                        )
                    with lb_btn_col:
                        do_reclassify = st.button("Apply", type="primary", key=f"lb_apply_{selected_t}")

                    if do_reclassify and lb_url_t:
                        with st.spinner("Scraping leaderboard..."):
                            try:
                                lb_status = scrape_pga_leaderboard_status(lb_url_t)
                                if not lb_status:
                                    st.error("Couldn't extract player statuses from that page. Check the URL.")
                                else:
                                    # Convert ALL $0-prize league golfers to unknown_absent first, then re-classify
                                    refreshed = dict(results)
                                    for g in all_golfers_in_league:
                                        entry = refreshed.get(g, {"prize": 0, "status": "not_entered"})
                                        if get_prize(entry) == 0:
                                            refreshed[g] = {"prize": 0, "status": "unknown_absent"}

                                    refreshed = apply_leaderboard_status(refreshed, lb_status)
                                    data["tournaments"][selected_t]["results"] = refreshed
                                    save_data(data)

                                    newly_cut = sum(1 for g in all_golfers_in_league if refreshed.get(g, {}).get("status") == "cut")
                                    newly_wd  = sum(1 for g in all_golfers_in_league if refreshed.get(g, {}).get("status") == "wd")
                                    newly_dnp = sum(1 for g in all_golfers_in_league if refreshed.get(g, {}).get("status") == "not_entered")
                                    still_unk = sum(1 for g in all_golfers_in_league if refreshed.get(g, {}).get("status") == "unknown_absent")
                                    st.success(
                                        f"Re-classified {len(all_golfers_in_league)} players: "
                                        f"{newly_cut} cut · {newly_wd} WD/DQ · {newly_dnp} not entered"
                                        + (f" · ⚠️ {still_unk} still unknown (name mismatch?)" if still_unk else "")
                                    )
                                    st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")


elif page == "👥 Teams":
    st.title("👥 Teams")
    if not data["teams"]:
        st.info("No teams yet. Go to ⚙️ Setup.")
        st.stop()
    standings = compute_standings(data)
    sorted_teams = sorted(standings.items(), key=lambda x: x[1]["total"], reverse=True)
    selected_team = st.selectbox("Select Team", [t[0] for t in sorted_teams])
    if selected_team:
        team_info = standings[selected_team]
        golfers = data["teams"][selected_team]
        rank = next(i+1 for i, (t, _) in enumerate(sorted_teams) if t == selected_team)
        rank_labels = {1: "🥇", 2: "🥈", 3: "🥉"}
        col1, col2 = st.columns([1, 2])
        with col1:
            st.markdown(f'<div class="metric-card"><div style="font-size:1rem;color:#555;">Season Rank</div><div class="big-number">{rank_labels.get(rank, f"#{rank}")}</div></div><div class="metric-card"><div style="font-size:1rem;color:#555;">Total Earnings</div><div class="big-number">{fmt_money(team_info["total"])}</div></div>', unsafe_allow_html=True)
            st.markdown("**Drafted Golfers:**")
            for g in sorted(golfers):
                st.markdown(f"• {g}")
        with col2:
            st.subheader("Tournament Breakdown")
            t_rows = []
            for t_name in get_ordered_tournaments(data):
                if t_name not in team_info["tournaments"]:
                    continue
                t_data = team_info["tournaments"][t_name]
                top3 = t_data.get("top3", [])
                t_rows.append({"Tournament": t_name, "Top 3 Total": t_data["total"], "Scoring Golfers": ", ".join(f"{g} ({fmt_money(m)})" for g, m in top3) if top3 else "—"})
            if t_rows:
                st.dataframe(pd.DataFrame(t_rows).style.format({"Top 3 Total": fmt_money}), width="stretch", hide_index=True)
            st.subheader("Golfer Detail")
            ge = defaultdict(lambda: {"cashes": 0, "cuts": 0, "wds": 0, "not_entered": 0, "total_prize": 0, "counted": 0})
            for t_name, t_info_item in data["tournaments"].items():
                results = t_info_item.get("results", {})
                top3_names = {g for g, _ in team_info["tournaments"].get(t_name, {}).get("top3", [])}
                for g in golfers:
                    entry = results.get(g, {"prize": 0, "status": "not_entered"})
                    prize, status = get_prize(entry), get_status(entry)
                    if status == "cut": ge[g]["cuts"] += 1
                    elif status == "wd": ge[g]["wds"] += 1
                    elif status in ("not_entered", "unknown_absent"): ge[g]["not_entered"] += 1
                    elif prize > 0: ge[g]["cashes"] += 1; ge[g]["total_prize"] += prize
                    if g in top3_names: ge[g]["counted"] += prize
            ge_df = pd.DataFrame([{"Golfer": g, "Cashes": ge[g]["cashes"], "Cuts": ge[g]["cuts"], "WD/DQ": ge[g]["wds"], "Not Entered": ge[g]["not_entered"], "Total Prize": ge[g]["total_prize"], "Counted for Team": ge[g]["counted"]} for g in sorted(golfers)]).sort_values("Counted for Team", ascending=False)
            st.dataframe(ge_df.style.format({"Total Prize": fmt_money, "Counted for Team": fmt_money}), width="stretch", hide_index=True)

# ─────────────────────────────────────────────
# PAGE: PLAYER STATS
# ─────────────────────────────────────────────

elif page == "📊 Player Stats":
    st.title("📊 Player Stats")
    if not data["teams"]:
        st.info("No teams set up yet.")
        st.stop()

    tab_owned, tab_unowned = st.tabs(["👥 Rostered Players", "🆓 Best Unowned Golfers"])

    with tab_owned:
        all_golfers = sorted(set(g for gs in data["teams"].values() for g in gs))
        golfer_to_team = {g: t for t, gs in data["teams"].items() for g in gs}
        rows = []
        for golfer in all_golfers:
            team = golfer_to_team.get(golfer, "?")
            stats = {"cashes": 0, "cuts": 0, "wds": 0, "not_entered": 0, "total_prize": 0, "counted": 0}
            for t_name, t_info in data["tournaments"].items():
                results = t_info.get("results", {})
                entry = results.get(golfer, {"prize": 0, "status": "not_entered"})
                prize, status = get_prize(entry), get_status(entry)
                if status == "cut": stats["cuts"] += 1
                elif status == "wd": stats["wds"] += 1
                elif status in ("not_entered", "unknown_absent"): stats["not_entered"] += 1
                elif prize > 0: stats["cashes"] += 1; stats["total_prize"] += prize
                _, top3 = get_team_earnings_for_tournament(data["teams"].get(team, []), results)
                if golfer in {g2 for g2, _ in top3}: stats["counted"] += prize
            rows.append({"Golfer": golfer, "Team": team, "Cashes": stats["cashes"], "Cuts": stats["cuts"], "WD/DQ": stats["wds"], "Not Entered": stats["not_entered"], "Total Prize": stats["total_prize"], "Counted for Team": stats["counted"]})
        df = pd.DataFrame(rows)
        col1, col2 = st.columns(2)
        with col1:
            team_filter = st.multiselect("Filter by Team", sorted(data["teams"].keys()))
        with col2:
            sort_col = st.selectbox("Sort By", ["Counted for Team", "Total Prize", "Cashes", "Cuts"])
        if team_filter:
            df = df[df["Team"].isin(team_filter)]
        df = df.sort_values(sort_col, ascending=False)
        st.dataframe(df.style.format({"Total Prize": fmt_money, "Counted for Team": fmt_money}), width="stretch", hide_index=True)
        if len(df) > 0:
            st.markdown("---")
            st.subheader("🌟 Top Performers")
            full_df = pd.DataFrame(rows)
            c1, c2, c3 = st.columns(3)
            with c1:
                top = full_df.sort_values("Counted for Team", ascending=False).iloc[0]
                st.markdown("**Most Counted for Team**")
                st.metric(top["Golfer"], fmt_money(top["Counted for Team"]), f"({top['Team']})")
            with c2:
                top = full_df.sort_values("Total Prize", ascending=False).iloc[0]
                st.markdown("**Most Prize Money**")
                st.metric(top["Golfer"], fmt_money(top["Total Prize"]), f"({top['Team']})")
            with c3:
                top = full_df.sort_values("Cuts", ascending=False).iloc[0]
                st.markdown("**Most Cuts Made (bad)**")
                st.metric(top["Golfer"], f"{int(top['Cuts'])} cuts", f"({top['Team']})")

    with tab_unowned:
        st.subheader("🆓 Best Unowned Golfers This Season")
        st.caption("Golfers who appeared in tournament results but aren't on any roster, ranked by total prize money.")

        unowned = get_unowned_golfer_earnings(data)
        if not unowned:
            st.info("No unowned golfer data yet — import some tournaments first.")
        else:
            ordered_t = get_ordered_tournaments(data)
            medals = ["🥇", "🥈", "🥉"]

            # Top 3 callout cards
            top3_cards = unowned[:3]
            card_cols = st.columns(len(top3_cards))
            for i, (name, prize) in enumerate(top3_cards):
                with card_cols[i]:
                    st.metric(
                        label=f"{medals[i]} {name}",
                        value=fmt_money(prize),
                        delta="unowned · season total",
                        delta_color="off"
                    )

            st.markdown("---")
            show_n = st.slider("Show top N golfers", min_value=5, max_value=min(50, len(unowned)), value=min(20, len(unowned)), step=5)

            unowned_rows = []
            for golfer, total in unowned[:show_n]:
                row = {"Golfer": golfer, "Season Total": total}
                for t_name in ordered_t:
                    entry = data["tournaments"].get(t_name, {}).get("results", {}).get(golfer)
                    row[t_name] = get_prize(entry) if entry else 0
                unowned_rows.append(row)

            unowned_df = pd.DataFrame(unowned_rows)
            money_cols = [c for c in unowned_df.columns if c != "Golfer"]
            st.dataframe(unowned_df.style.format({c: fmt_money for c in money_cols}), width="stretch", hide_index=True)
            st.caption(f"Showing {min(show_n, len(unowned))} of {len(unowned)} unowned golfers with prize money this season.")

# ─────────────────────────────────────────────
# PAGE: SETUP
# ─────────────────────────────────────────────

elif page == "⚙️ Setup":
    if not st.session_state.is_admin:
        st.error("🔒 Admin access required.")
        st.stop()
    st.title("⚙️ League Setup")
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Add Team", "Edit Team Roster", "Tournament Order", "Manual Entry / Edit", "Import / Export"])

    with tab1:
        st.subheader("Create a New Team")
        new_team_name = st.text_input("Team / Owner Name")
        golfer_input = st.text_area("Golfers (one per line)", height=300, placeholder="Scottie Scheffler\nRory McIlroy\n...")
        if st.button("➕ Add Team", type="primary") and new_team_name and golfer_input:
            golfers = [g.strip() for g in golfer_input.strip().split("\n") if g.strip()]
            if new_team_name in data["teams"]:
                st.warning("Already exists — use Edit tab to modify.")
            else:
                data["teams"][new_team_name] = golfers
                save_data(data)
                st.success(f"Added '{new_team_name}' with {len(golfers)} golfers!")
                st.rerun()
        st.info("**Name matching:** Golfer names must match ESPN exactly. Check the Full Leaderboard tab — ⭐ marks matched players.")

    with tab2:
        if not data["teams"]:
            st.info("No teams yet.")
        else:
            edit_team = st.selectbox("Select Team", sorted(data["teams"].keys()))
            updated = st.text_area("Golfers (one per line)", value="\n".join(data["teams"].get(edit_team, [])), height=300)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("💾 Save", type="primary"):
                    data["teams"][edit_team] = [g.strip() for g in updated.strip().split("\n") if g.strip()]
                    save_data(data)
                    st.success("Saved!")
                    st.rerun()
            with c2:
                if st.button("🗑️ Delete Team", type="secondary"):
                    del data["teams"][edit_team]
                    save_data(data)
                    st.rerun()

    with tab3:
        st.subheader("Tournament Order")
        st.markdown("Set the order tournaments appear in standings and the chart.")
        order = data.get("tournament_order", get_ordered_tournaments(data))
        if not order:
            st.info("No tournaments yet.")
        else:
            for i, t_name in enumerate(order):
                c1, c2, c3 = st.columns([6, 1, 1])
                c1.markdown(f"**{i+1}.** {t_name}")
                if i > 0 and c2.button("▲", key=f"up_{i}"):
                    order[i-1], order[i] = order[i], order[i-1]
                    data["tournament_order"] = order
                    save_data(data)
                    st.rerun()
                if i < len(order)-1 and c3.button("▼", key=f"dn_{i}"):
                    order[i], order[i+1] = order[i+1], order[i]
                    data["tournament_order"] = order
                    save_data(data)
                    st.rerun()

    with tab4:
        st.subheader("Manual Entry / Edit")
        st.info("💡 Use Live Leaderboard → Import to auto-populate results. Use this tab for manual corrections.")
        mode_entry = st.radio("Action", ["Create new tournament", "Edit existing tournament"],
                              horizontal=True, label_visibility="collapsed")
        if mode_entry == "Create new tournament":
            new_t_name = st.text_input("Tournament Name", key="new_t_setup")
            if st.button("Create Tournament", key="create_t_setup") and new_t_name:
                if new_t_name not in data["tournaments"]:
                    data["tournaments"][new_t_name] = {"results": {}}
                    if new_t_name not in data.get("tournament_order", []):
                        data.setdefault("tournament_order", []).append(new_t_name)
                    save_data(data)
                    st.success(f"Created '{new_t_name}'")
                    st.rerun()
                else:
                    st.warning("Already exists.")
        if data["tournaments"]:
            edit_t = st.selectbox("Tournament to edit", get_ordered_tournaments(data), key="edit_t_setup")
            if edit_t and data["teams"]:
                all_golfers_me = sorted(set(g for gs in data["teams"].values() for g in gs))
                current_results_me = data["tournaments"][edit_t].get("results", {})
                all_statuses = ["scored", "cut", "wd", "not_entered", "unknown_absent"]
                with st.form("results_form_setup"):
                    st.markdown("Enter prize money and status for each golfer.")
                    new_results_me = {}
                    for i in range(0, len(all_golfers_me), 3):
                        cols = st.columns(3)
                        for j, golfer in enumerate(all_golfers_me[i:i+3]):
                            entry = current_results_me.get(golfer, {"prize": 0, "status": "not_entered"})
                            cur_prize = int(get_prize(entry))
                            cur_status = get_status(entry)
                            safe_idx = all_statuses.index(cur_status) if cur_status in all_statuses else 3
                            with cols[j]:
                                st.markdown(f"**{golfer}**")
                                prize_val = st.number_input(f"Prize ({golfer})", min_value=0, value=cur_prize,
                                                            step=1000, key=f"prize_me_{edit_t}_{golfer}",
                                                            label_visibility="collapsed")
                                status_val = st.selectbox(f"Status ({golfer})", all_statuses, index=safe_idx,
                                                          key=f"status_me_{edit_t}_{golfer}",
                                                          label_visibility="collapsed")
                                new_results_me[golfer] = {"prize": prize_val, "status": status_val}
                    if st.form_submit_button("💾 Save Results", type="primary"):
                        data["tournaments"][edit_t]["results"] = new_results_me
                        save_data(data)
                        st.success(f"Saved results for {edit_t}")
                        st.rerun()
            st.markdown("---")
            del_t = st.selectbox("Delete a tournament", get_ordered_tournaments(data), key="del_t_setup")
            if st.button("🗑️ Delete Tournament", type="secondary", key="del_t_btn"):
                del data["tournaments"][del_t]
                if del_t in data.get("tournament_order", []):
                    data["tournament_order"].remove(del_t)
                save_data(data)
                st.success(f"Deleted {del_t}")
                st.rerun()

    with tab5:
        st.subheader("Backup & Restore")
        c1, c2 = st.columns(2)
        with c1:
            st.download_button("⬇️ Download Backup", data=json.dumps(data, indent=2), file_name="fantasy_golf_backup.json", mime="application/json")
        with c2:
            uploaded = st.file_uploader("Upload backup", type="json")
            if uploaded:
                imported = json.load(uploaded)
                st.session_state.data = imported
                save_data(imported)
                st.success("Imported!")
                st.rerun()
        st.markdown("---")
        st.markdown(f"**Teams:** {len(data['teams'])} | **Golfers:** {sum(len(v) for v in data['teams'].values())} | **Tournaments:** {len(data['tournaments'])}")