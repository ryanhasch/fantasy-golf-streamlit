"""
Fantasy Golf League Tracker
Run with:  uv run streamlit run fantasy_golf.py
Install:   uv add streamlit pandas requests beautifulsoup4
Python:    3.8+

Results storage format per golfer:
  { "prize": 1400000, "status": "scored" }
  { "prize": 0,       "status": "cut"    }
  { "prize": 0,       "status": "wd"     }
  { "prize": 0,       "status": "not_entered" }
"""

import streamlit as st
import json
import os
import re
import requests
import pandas as pd
from bs4 import BeautifulSoup
from collections import defaultdict

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

DATA_FILE = "fantasy_golf_data.json"

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/scoreboard"
ESPN_LEADERBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/pga/leaderboard"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

STATUS_EMOJI = {
    "scored":      "💰",
    "cut":         "✂️ CUT",
    "wd":          "🚫 WD/DQ",
    "not_entered": "—",
}

# ─────────────────────────────────────────────
# Data persistence
# ─────────────────────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
        # Ensure tournament_order exists and is in sync
        if "tournament_order" not in d:
            d["tournament_order"] = list(d.get("tournaments", {}).keys())
        # Add any tournaments not yet in order list
        for t in d.get("tournaments", {}):
            if t not in d["tournament_order"]:
                d["tournament_order"].append(t)
        return d
    return {"teams": {}, "tournaments": {}, "tournament_order": []}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_prize(result_entry):
    """Safely extract prize from either old format (int) or new format (dict)."""
    if isinstance(result_entry, dict):
        return result_entry.get("prize", 0)
    return result_entry  # old format was bare int

def get_status(result_entry):
    """Safely extract status."""
    if isinstance(result_entry, dict):
        return result_entry.get("status", "scored")
    return "scored"  # old format assumed scored if value > 0

# ─────────────────────────────────────────────
# PGA Tour payout article scraper
# ─────────────────────────────────────────────

def scrape_pga_payout_table(url: str):
    """
    Scrape a PGA Tour payout article.
    Returns:
      - payout_map: {position_int: prize_float}
      - tournament_name: str
    """
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find("h1")
    title_tag = soup.find("title")
    raw_title = h1.get_text(strip=True) if h1 else (title_tag.get_text(strip=True) if title_tag else "")
    tourney_name = raw_title.split("|")[0].strip()[:80]

    payout_map = {}
    table = soup.find("table")
    if not table:
        raise ValueError("No payout table found. Make sure you're using a PGA Tour payout article URL.")

    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        pos_text = cells[0].get_text(strip=True)
        amount_text = cells[2].get_text(strip=True)

        if pos_text.lower() in ("pos.", "pos", "position"):
            continue
        try:
            pos = int(pos_text)
        except ValueError:
            continue
        amount_clean = re.sub(r"[^\d.]", "", amount_text)
        try:
            amount = float(amount_clean)
        except ValueError:
            continue
        if amount > 0:
            payout_map[pos] = amount

    if not payout_map:
        raise ValueError("Found a table but couldn't parse payout amounts. Check the URL.")

    return payout_map, tourney_name


# ─────────────────────────────────────────────
# PGA Tour "Points and Payouts" results scraper
# (for completed tournaments)
# ─────────────────────────────────────────────

def scrape_pga_results_article(url: str):
    """
    Scrape a PGA Tour 'Points and Payouts' article.
    These articles have a per-player table with name, position, and prize.
    Returns:
      - players: list of {"name": str, "position": int, "position_display": str,
                          "prize": float, "status": str}
      - tournament_name: str
    """
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    h1 = soup.find("h1")
    title_tag = soup.find("title")
    raw_title = h1.get_text(strip=True) if h1 else (title_tag.get_text(strip=True) if title_tag else "")
    tourney_name = raw_title.split("|")[0].strip()[:80]

    players = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        # Look for a table that has player/name and money/prize columns
        has_name = any(h in ("player", "name", "golfer") for h in headers)
        has_money = any(h in ("money", "prize", "earnings", "amount", "prize money") for h in headers)

        if not (has_name or has_money):
            # Try to detect by column content — look for $ signs in rows
            rows = table.find_all("tr")
            sample_text = " ".join(td.get_text() for row in rows[:5] for td in row.find_all("td"))
            if "$" not in sample_text:
                continue

        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        # Detect column indices from header row
        header_row = rows[0].find_all(["th", "td"])
        header_texts = [c.get_text(strip=True).lower() for c in header_row]

        pos_idx = next((i for i, h in enumerate(header_texts)
                        if h in ("pos", "pos.", "position", "place", "fin", "finish")), None)
        name_idx = next((i for i, h in enumerate(header_texts)
                         if h in ("player", "name", "golfer", "athlete")), None)
        money_idx = next((i for i, h in enumerate(header_texts)
                          if h in ("money", "prize", "earnings", "amount", "prize money",
                                   "winnings", "purse")), None)

        # If headers not found, try positional guessing:
        # PGA tour format is usually: Pos | Player | Score | ... | Money
        if pos_idx is None and name_idx is None:
            # Check if first col looks like positions and last col looks like money
            sample_rows = rows[1:4]
            for sr in sample_rows:
                cells = sr.find_all("td")
                if len(cells) >= 3:
                    first = cells[0].get_text(strip=True)
                    last = cells[-1].get_text(strip=True)
                    if re.match(r"^T?\d+$", first) and "$" in last:
                        pos_idx = 0
                        name_idx = 1
                        money_idx = len(cells) - 1
                        break

        if name_idx is None or money_idx is None:
            continue

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(filter(None, [pos_idx, name_idx, money_idx])):
                continue

            name = cells[name_idx].get_text(strip=True) if name_idx < len(cells) else ""
            if not name or name.lower() in ("player", "name", "golfer"):
                continue

            money_text = cells[money_idx].get_text(strip=True) if money_idx < len(cells) else "0"
            money_clean = re.sub(r"[^\d.]", "", money_text)
            try:
                prize = float(money_clean)
            except ValueError:
                prize = 0.0

            pos_display = ""
            pos_int = 999
            if pos_idx is not None and pos_idx < len(cells):
                pos_display = cells[pos_idx].get_text(strip=True)
                pos_num = re.sub(r"[^\d]", "", pos_display)
                try:
                    pos_int = int(pos_num)
                except ValueError:
                    pass

            # Infer status from position display
            pd_lower = pos_display.lower()
            if "cut" in pd_lower or "mc" in pd_lower:
                status = "cut"
            elif any(x in pd_lower for x in ("wd", "dq", "w/d", "disq")):
                status = "wd"
            elif prize > 0 or (pos_int < 999):
                status = "scored"
            else:
                status = "cut"

            players.append({
                "name": name,
                "position": pos_int,
                "position_display": pos_display,
                "prize": prize,
                "status": status,
            })

        if players:
            break  # Found a valid table, stop

    if not players:
        raise ValueError(
            "Could not find a player results table in this article. "
            "Make sure you're using a 'Points and Payouts' article, not the purse breakdown."
        )

    return players, tourney_name


# ─────────────────────────────────────────────
# ESPN leaderboard fetcher (LIVE - current tournament only)
# ─────────────────────────────────────────────

def fetch_espn_leaderboard():
    """
    Fetch current live leaderboard from ESPN.
    NOTE: Only works for the currently active tournament week.
    For past tournaments, use scrape_pga_results_article() instead.

    Returns:
      - players: list of dicts with name, position, position_display, score, thru, espn_status
      - tournament_name: str
      - status_message: str
    """
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
        raise ConnectionError("Could not reach ESPN API. Check your internet connection.")

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

    # Safely extract status message
    status_obj = comp.get("status", {})
    if not isinstance(status_obj, dict):
        status_obj = {}
    round_num = status_obj.get("period", 0)
    type_obj = status_obj.get("type", {})
    if not isinstance(type_obj, dict):
        type_obj = {}
    status_detail = type_obj.get("detail", "")
    status_message = f"Round {round_num} - {status_detail}" if round_num else status_detail

    players = []
    for c in comp.get("competitors", []):
        if not isinstance(c, dict):
            continue

        # Athlete name
        athlete = c.get("athlete", {})
        if not isinstance(athlete, dict):
            athlete = {}
        full_name = athlete.get("displayName", "Unknown")

        # Position — defensive: position may be dict or absent
        c_status = c.get("status", {})
        if not isinstance(c_status, dict):
            c_status = {}
        position_obj = c_status.get("position", {})
        if isinstance(position_obj, dict):
            pos_display = position_obj.get("displayName", "")
        elif isinstance(position_obj, str):
            pos_display = position_obj
        else:
            pos_display = ""

        pos_int_str = re.sub(r"[^\d]", "", pos_display)
        try:
            pos_int = int(pos_int_str)
        except ValueError:
            pos_int = 999

        # Score
        score_obj = c.get("score", {})
        if isinstance(score_obj, dict):
            score_total = score_obj.get("displayValue", "E")
        elif isinstance(score_obj, str):
            score_total = score_obj
        else:
            score_total = "E"

        # Thru
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

        # ESPN status name
        type_obj2 = c_status.get("type", {})
        if isinstance(type_obj2, dict):
            espn_status = type_obj2.get("name", "").lower()
        elif isinstance(type_obj2, str):
            espn_status = type_obj2.lower()
        else:
            espn_status = ""

        players.append({
            "name": full_name,
            "position": pos_int,
            "position_display": pos_display,
            "score": score_total,
            "thru": thru_val,
            "espn_status": espn_status,
        })

    players.sort(key=lambda x: x["position"])
    return players, tournament_name, status_message


def espn_status_to_league_status(espn_status: str, prize: float) -> str:
    """Map ESPN status string to our four league statuses."""
    s = espn_status.lower()
    if "cut" in s or "mdf" in s:
        return "cut"
    if "wd" in s or "withdraw" in s or "dq" in s or "disqualif" in s:
        return "wd"
    if prize > 0:
        return "scored"
    # Active but $0 — either still playing or very far back but not cut
    return "scored"


def build_results_from_espn(players, payout_map, all_league_golfers=None):
    """
    Build a results dict from scraped player data + payout map.
    Saves ALL players from the source (not just current league members),
    so rosters added later will still find their golfers results.
    Golfers in all_league_golfers who are missing = not_entered.
    Returns {golfer_name: {"prize": float, "status": str}}
    """
    results = {}

    for p in players:
        pos = p["position"]
        prize = p.get("prize") or payout_map.get(pos, 0.0)
        espn_st = p.get("espn_status") or p.get("status", "")
        status = espn_status_to_league_status(espn_st, prize)
        results[p["name"]] = {"prize": prize, "status": status}

    if all_league_golfers:
        for golfer in all_league_golfers:
            if golfer not in results:
                results[golfer] = {"prize": 0, "status": "not_entered"}

    return results


# ─────────────────────────────────────────────
# League logic
# ─────────────────────────────────────────────

def get_team_earnings_for_tournament(team_golfers, tournament_results):
    """
    Returns (top3_total, top3_list) where top3_list = [(golfer, prize), ...]
    Only golfers with prize > 0 are eligible.
    """
    earnings = []
    for golfer in team_golfers:
        entry = tournament_results.get(golfer, {"prize": 0, "status": "not_entered"})
        prize = get_prize(entry)
        if prize > 0:
            earnings.append((golfer, prize))
    earnings.sort(key=lambda x: x[1], reverse=True)
    top3 = earnings[:3]
    return sum(e[1] for e in top3), top3


def get_ordered_tournaments(data):
    """Return tournament names in the correct display order."""
    order = data.get("tournament_order", [])
    # Include any not in order list at the end (safety net)
    all_t = list(data.get("tournaments", {}).keys())
    return order + [t for t in all_t if t not in order]


def compute_standings(data):
    standings = {}
    for team_name, golfers in data["teams"].items():
        standings[team_name] = {"total": 0, "tournaments": {}}

    for t_name in get_ordered_tournaments(data):
        t_info = data["tournaments"].get(t_name, {})
        results = t_info.get("results", {})
        for team_name, golfers in data["teams"].items():
            total, top3 = get_team_earnings_for_tournament(golfers, results)
            standings[team_name]["total"] += total
            standings[team_name]["tournaments"][t_name] = {"total": total, "top3": top3}

    return standings


def compute_rank_history(data):
    """
    Returns a dict: {team_name: [(tournament_label, cumulative_total, rank), ...]}
    Tournaments in order, ranks computed after each event.
    """
    ordered = get_ordered_tournaments(data)
    if not ordered:
        return {}

    teams = list(data["teams"].keys())
    cumulative = {t: 0 for t in teams}
    history = {t: [] for t in teams}

    for t_name in ordered:
        t_info = data["tournaments"].get(t_name, {})
        results = t_info.get("results", {})
        for team_name in teams:
            golfers = data["teams"][team_name]
            total, _ = get_team_earnings_for_tournament(golfers, results)
            cumulative[team_name] += total

        # Compute ranks at this point
        sorted_teams = sorted(teams, key=lambda t: cumulative[t], reverse=True)
        ranks = {t: i+1 for i, t in enumerate(sorted_teams)}

        for team_name in teams:
            history[team_name].append({
                "tournament": t_name,
                "cumulative": cumulative[team_name],
                "rank": ranks[team_name],
            })

    return history


def compute_live_team_standings(data, live_payout, live_players):
    name_to_prize = {}
    for p in live_players:
        pos = p["position"]
        espn_st = p.get("espn_status", "")
        if "cut" in espn_st or "wd" in espn_st or "dq" in espn_st:
            name_to_prize[p["name"]] = 0
        else:
            name_to_prize[p["name"]] = live_payout.get(pos, 0.0)

    results = []
    for team_name, golfers in data["teams"].items():
        earnings = [(g, name_to_prize[g]) for g in golfers
                    if g in name_to_prize and name_to_prize[g] > 0]
        earnings.sort(key=lambda x: x[1], reverse=True)
        top3 = earnings[:3]
        results.append((team_name, sum(e[1] for e in top3), top3))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def fmt_money(val):
    if not val:
        return "$0"
    return f"${float(val):,.0f}"


# ─────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────

st.set_page_config(page_title="Fantasy Golf League", page_icon="🏌️", layout="wide")

st.markdown("""
<style>
    [data-testid="stSidebar"] { background: #1a2e1a; }
    [data-testid="stSidebar"] * { color: #e8f5e0 !important; }
    .big-number { font-size: 2.2rem; font-weight: 700; color: #2d6a2d; }
    .metric-card {
        background: #f0f7ee;
        border-left: 4px solid #3a7d3a;
        border-radius: 6px;
        padding: 1rem 1.2rem;
        margin-bottom: 0.5rem;
    }
    h1, h2, h3 { color: #1a3d1a; }
</style>
""", unsafe_allow_html=True)

if "data" not in st.session_state:
    st.session_state.data = load_data()
if "live_payout" not in st.session_state:
    st.session_state.live_payout = {}
if "live_players" not in st.session_state:
    st.session_state.live_players = []
if "live_tourney_name" not in st.session_state:
    st.session_state.live_tourney_name = ""
if "live_status" not in st.session_state:
    st.session_state.live_status = ""

data = st.session_state.data

# ─────────────────────────────────────────────
# Admin password
# Change ADMIN_PASSWORD to whatever you want.
# ─────────────────────────────────────────────
ADMIN_PASSWORD = st.secrets["ADMIN_PASSWORD"]

if "is_admin" not in st.session_state:
    st.session_state.is_admin = False

st.sidebar.title("🏌️ Fantasy Golf")
st.sidebar.markdown("---")

# Admin login/logout in sidebar
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

# Build page list based on admin status
if st.session_state.is_admin:
    page_options = ["🔴 Live Leaderboard", "🏆 Standings", "🗓️ Tournaments", "👥 Teams", "📊 Player Stats", "⚙️ Setup"]
else:
    page_options = ["🔴 Live Leaderboard", "🏆 Standings", "👥 Teams", "📊 Player Stats"]

page = st.sidebar.radio("Navigate", page_options)

# ─────────────────────────────────────────────
# PAGE: LIVE LEADERBOARD
# ─────────────────────────────────────────────

if page == "🔴 Live Leaderboard":
    st.title("🔴 Live Leaderboard")

    if not data["teams"]:
        st.info("No teams set up yet. Go to ⚙️ Setup first.")
        st.stop()

    # ── Mode selector (admin only sees import option) ──
    if st.session_state.is_admin:
        mode = st.radio(
            "What do you want to do?",
            ["📥 Import a completed tournament", "🔴 Track a live/current tournament"],
            horizontal=True,
            label_visibility="collapsed"
        )
    else:
        mode = "🔴 Track a live/current tournament"

    st.markdown("---")

    # ══════════════════════════════════════════
    # MODE A: Import completed tournament (admin only)
    # ══════════════════════════════════════════
    if mode == "📥 Import a completed tournament":
        st.subheader("📥 Import Completed Tournament Results")
        st.markdown(
            "After a tournament ends, PGA Tour publishes a **'Points and Payouts'** article "
            "with every player's prize. Paste that URL here — it looks like:\n\n"
            "`pgatour.com/article/news/betting-dfs/2026/.../points-payouts-...`\n\n"
            "Search Google for: **`site:pgatour.com points payouts [tournament name] 2026`**"
        )

        col_url, col_btn = st.columns([4, 1])
        with col_url:
            results_url = st.text_input(
                "Points & Payouts URL", label_visibility="collapsed",
                placeholder="https://www.pgatour.com/article/news/betting-dfs/2026/.../points-payouts-..."
            )
        with col_btn:
            fetch_results = st.button("Fetch", type="primary", key="fetch_results")

        if fetch_results and results_url:
            with st.spinner("Scraping results article..."):
                try:
                    result_players, tourney_name = scrape_pga_results_article(results_url)
                    st.session_state.live_players = [
                        {**p, "espn_status": p["status"], "score": "", "thru": "F"}
                        for p in result_players
                    ]
                    st.session_state.live_tourney_name = tourney_name
                    st.session_state.live_status = "Final"
                    # Build a payout map from the results themselves
                    st.session_state.live_payout = {
                        p["position"]: p["prize"]
                        for p in result_players if p["prize"] > 0
                    }
                    st.success(
                        f"Fetched **{tourney_name}** — {len(result_players)} players found. "
                        f"Ready to import below."
                    )
                except Exception as e:
                    st.error(f"Failed: {e}")

        if st.session_state.live_players and st.session_state.live_status == "Final":
            st.markdown("---")
            st.subheader(f"Preview: {st.session_state.live_tourney_name}")

            all_golfers = list(set(g for gs in data["teams"].values() for g in gs))
            results_preview = build_results_from_espn(
                st.session_state.live_players,
                st.session_state.live_payout,
                all_golfers
            )

            # Show preview table for league golfers only
            preview_rows = []
            for g in sorted(all_golfers):
                entry = results_preview[g]
                team = next((t for t, gs in data["teams"].items() if g in gs), "?")
                preview_rows.append({
                    "Golfer": g, "Team": team,
                    "Status": STATUS_EMOJI.get(entry["status"], entry["status"]),
                    "Prize": entry["prize"],
                })
            preview_rows.sort(key=lambda x: x["Prize"], reverse=True)
            st.dataframe(
                pd.DataFrame(preview_rows).style.format({"Prize": fmt_money}),
                width="stretch", hide_index=True
            )

            col1, col2 = st.columns([2, 1])
            with col1:
                import_name = st.text_input(
                    "Save as tournament name",
                    value=st.session_state.live_tourney_name,
                    key="import_name_completed"
                )
            with col2:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("💾 Save to Season", type="primary", key="save_completed"):
                    if import_name:
                        data["tournaments"][import_name] = {"results": results_preview}
                        save_data(data)
                        scored  = sum(1 for v in results_preview.values() if v["status"] == "scored" and v["prize"] > 0)
                        cut     = sum(1 for v in results_preview.values() if v["status"] == "cut")
                        wd      = sum(1 for v in results_preview.values() if v["status"] == "wd")
                        missing = sum(1 for v in results_preview.values() if v["status"] == "not_entered")
                        st.success(f"Saved! {scored} scored · {cut} cut · {wd} WD/DQ · {missing} not in field")
                        st.rerun()

    # ══════════════════════════════════════════
    # MODE B: Live tournament tracking
    # ══════════════════════════════════════════
    else:
        st.subheader("🔴 Live Tournament Tracking")
        st.caption(
            "ESPN's API only provides data for the **current active tournament week**. "
            "For completed tournaments, switch to 'Import a completed tournament' above."
        )

        # Step 1: Payout table
        st.markdown("**Step 1 — Load Payout Table** (paste the purse breakdown article URL)")
        col_url, col_btn = st.columns([4, 1])
        with col_url:
            payout_url = st.text_input(
                "Payout URL", label_visibility="collapsed",
                placeholder="https://www.pgatour.com/article/news/.../purse-breakdown-..."
            )
        with col_btn:
            if st.button("Load", type="primary", key="load_payout"):
                with st.spinner("Scraping payout table..."):
                    try:
                        pm, tn = scrape_pga_payout_table(payout_url)
                        st.session_state.live_payout = pm
                        st.session_state.live_tourney_name = tn
                        st.session_state.live_status = ""
                        st.success(f"Loaded **{tn}** — winner: {fmt_money(pm.get(1,0))}")
                    except Exception as e:
                        st.error(f"Failed: {e}")

        if st.session_state.live_payout and st.session_state.live_status != "Final":
            with st.expander("View payout table"):
                st.dataframe(
                    pd.DataFrame([{"Position": k, "Prize": fmt_money(v)}
                                   for k, v in sorted(st.session_state.live_payout.items())]),
                    width="stretch", hide_index=True
                )

        st.markdown("**Step 2 — Fetch Live Standings from ESPN**")
        if st.button("🔄 Refresh Leaderboard", type="primary", key="refresh_live"):
            with st.spinner("Fetching from ESPN..."):
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
            st.markdown("---")
            tourney_label = st.session_state.live_tourney_name or "Current Tournament"
            st.subheader(f"{tourney_label} — {st.session_state.live_status}")

            # Import final results button (if tournament just finished)
            with st.expander("💾 Import as Final Results (use when tournament is complete)"):
                if not st.session_state.live_payout:
                    st.warning("Load payout table in Step 1 first.")
                else:
                    col1, col2 = st.columns([2, 1])
                    with col1:
                        import_name = st.text_input("Save as", value=tourney_label, key="import_live_name")
                    with col2:
                        st.markdown("<br>", unsafe_allow_html=True)
                        if st.button("💾 Save to Season", type="primary", key="save_live"):
                            all_golfers = list(set(g for gs in data["teams"].values() for g in gs))
                            results = build_results_from_espn(
                                st.session_state.live_players,
                                st.session_state.live_payout,
                                all_golfers
                            )
                            data["tournaments"][import_name] = {"results": results}
                            save_data(data)
                            st.success(f"Saved {import_name}!")
                            st.rerun()

            tab1, tab2 = st.tabs(["🏆 Team Projections", "📋 Full Leaderboard"])

            with tab1:
                if not st.session_state.live_payout:
                    st.warning("Load payout table in Step 1 to see projected earnings.")
                else:
                    proj = compute_live_team_standings(
                        data, st.session_state.live_payout, st.session_state.live_players
                    )
                    rank_labels = ["🥇", "🥈", "🥉"]
                    cols = st.columns(min(len(proj), 4))
                    for i, (team_name, total, _) in enumerate(proj[:4]):
                        with cols[i]:
                            medal = rank_labels[i] if i < 3 else f"#{i+1}"
                            st.markdown(f"""
                            <div class="metric-card">
                                <div style="font-size:1rem;font-weight:600;">{medal} {team_name}</div>
                                <div class="big-number">{fmt_money(total)}</div>
                                <div style="font-size:0.8rem;color:#555;">projected this event</div>
                            </div>""", unsafe_allow_html=True)

                    espn_by_name = {p["name"]: p for p in st.session_state.live_players}
                    st.markdown("#### Team Breakdown")
                    for rank, (team_name, total, top3) in enumerate(proj, 1):
                        medal = rank_labels[rank-1] if rank <= 3 else f"#{rank}"
                        with st.expander(f"{medal} {team_name} — {fmt_money(total)} projected"):
                            rows = []
                            for g in sorted(data["teams"][team_name]):
                                p = espn_by_name.get(g)
                                in_top3 = any(g == t[0] for t in top3)
                                if p:
                                    espn_st = p.get("espn_status", "")
                                    if "cut" in espn_st:
                                        disp = "✂️ CUT"; prize = 0
                                    elif "wd" in espn_st or "dq" in espn_st:
                                        disp = "🚫 WD/DQ"; prize = 0
                                    else:
                                        disp = "🏌️ Playing"
                                        prize = st.session_state.live_payout.get(p["position"], 0)
                                    rows.append({"Golfer": g, "Pos": p["position_display"],
                                                 "Score": p["score"], "Thru": p["thru"],
                                                 "Status": disp, "Proj. Prize": prize,
                                                 "Counts": "✅" if in_top3 else ""})
                                else:
                                    rows.append({"Golfer": g, "Pos": "—", "Score": "—",
                                                 "Thru": "—", "Status": "Not in field",
                                                 "Proj. Prize": 0, "Counts": ""})
                            st.dataframe(
                                pd.DataFrame(rows).style.format({"Proj. Prize": fmt_money}),
                                width="stretch", hide_index=True
                            )

                    st.markdown("---")
                    st.subheader("Season If Tournament Ended Now")
                    base = compute_standings(data)
                    combined = sorted([
                        {"Team": tn, "Season So Far": base.get(tn, {}).get("total", 0),
                         "This Event (proj)": pt,
                         "Total": base.get(tn, {}).get("total", 0) + pt}
                        for tn, pt, _ in proj
                    ], key=lambda x: x["Total"], reverse=True)
                    for i, row in enumerate(combined):
                        row["Rank"] = rank_labels[i] if i < 3 else f"#{i+1}"
                    cdf = pd.DataFrame(combined)[["Rank","Team","Season So Far","This Event (proj)","Total"]]
                    st.dataframe(
                        cdf.style.format({c: fmt_money for c in ["Season So Far","This Event (proj)","Total"]}),
                        width="stretch", hide_index=True
                    )

            with tab2:
                all_team_golfers = set(g for gs in data["teams"].values() for g in gs)
                lb_rows = []
                for p in st.session_state.live_players:
                    espn_st = p.get("espn_status", "")
                    if "cut" in espn_st:
                        status_disp = "✂️ CUT"; prize = 0
                    elif "wd" in espn_st or "dq" in espn_st:
                        status_disp = "🚫 WD/DQ"; prize = 0
                    else:
                        status_disp = "🏌️"
                        prize = st.session_state.live_payout.get(p["position"], 0) if st.session_state.live_payout else 0
                    lb_rows.append({
                        "⭐": "⭐" if p["name"] in all_team_golfers else "",
                        "Pos": p["position_display"], "Player": p["name"],
                        "Score": p["score"], "Thru": p["thru"],
                        "Status": status_disp, "Proj. Prize": prize,
                    })
                st.dataframe(
                    pd.DataFrame(lb_rows).style.format({"Proj. Prize": fmt_money}),
                    width="stretch", hide_index=True
                )
                st.caption("⭐ = on a league team")

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
    for i, (team_name, info) in enumerate(sorted_teams[:4]):
        with cols[i]:
            medal = rank_labels[i] if i < 3 else f"#{i+1}"
            st.markdown(f"""
            <div class="metric-card">
                <div style="font-size:1.1rem; font-weight:600;">{medal} {team_name}</div>
                <div class="big-number">{fmt_money(info['total'])}</div>
            </div>""", unsafe_allow_html=True)

    st.markdown("---")
    st.subheader("Full Standings Table")

    rows = []
    ordered_cols = get_ordered_tournaments(data)
    for rank, (team_name, info) in enumerate(sorted_teams, 1):
        row = {"Rank": rank, "Team": team_name, "Total Earnings": info["total"]}
        for t_name in ordered_cols:
            row[t_name] = info["tournaments"].get(t_name, {}).get("total", 0)
        rows.append(row)

    if rows:
        df = pd.DataFrame(rows)
        money_cols = [c for c in df.columns if c not in ["Rank", "Team"]]
        st.dataframe(
            df.style.format({c: fmt_money for c in money_cols}),
            width="stretch", hide_index=True
        )

    if len(sorted_teams) > 1:
        st.markdown("---")
        st.subheader("Gap to Leader")
        leader_total = sorted_teams[0][1]["total"]
        gap_rows = [
            {"Rank": rank, "Team": team, "Total": fmt_money(info["total"]),
             "Gap to Leader": "LEADER" if info["total"] == leader_total else f"-{fmt_money(leader_total - info['total'])}"}
            for rank, (team, info) in enumerate(sorted_teams, 1)
        ]
        st.dataframe(pd.DataFrame(gap_rows), width="stretch", hide_index=True)

    # ── Rank history chart ──────────────────────
    ordered_t = get_ordered_tournaments(data)
    if len(ordered_t) >= 1 and len(data["teams"]) >= 2:
        st.markdown("---")
        st.subheader("📈 Standings Over Time")
        st.caption("Lower = better. Shows cumulative rank after each tournament.")

        history = compute_rank_history(data)

        # Build a tidy dataframe for plotting
        plot_rows = []
        for team_name, events in history.items():
            for e in events:
                plot_rows.append({
                    "Tournament": e["tournament"],
                    "Team": team_name,
                    "Rank": e["rank"],
                    "Total Earnings": e["cumulative"],
                })
        plot_df = pd.DataFrame(plot_rows)

        # One color per team - use a distinct palette
        teams_ordered = [t for t, _ in sorted_teams]  # in current standing order
        colors = ["#2d6a2d","#e07b2a","#1a6fa8","#a82828","#7b3fa8","#a8963f",
                  "#2a9d8f","#e63946","#457b9d","#f4a261"]
        color_map = {t: colors[i % len(colors)] for i, t in enumerate(teams_ordered)}

        # Build SVG-free chart using streamlit + manual plotly
        try:
            import plotly.graph_objects as go

            fig = go.Figure()
            tournament_labels = [e["tournament"] for e in next(iter(history.values()))]

            for team_name in teams_ordered:
                events = history[team_name]
                ranks = [e["rank"] for e in events]
                totals = [fmt_money(e["cumulative"]) for e in events]
                t_labels = [e["tournament"] for e in events]

                fig.add_trace(go.Scatter(
                    x=t_labels,
                    y=ranks,
                    mode="lines+markers",
                    name=team_name,
                    line=dict(color=color_map[team_name], width=3),
                    marker=dict(size=10, color=color_map[team_name]),
                    hovertemplate=(
                        f"<b>{team_name}</b><br>"
                        "%{x}<br>"
                        "Rank: %{y}<br>"
                        "Total: " + "%{customdata}<extra></extra>"
                    ),
                    customdata=totals,
                ))

            n_teams = len(data["teams"])
            fig.update_layout(
                yaxis=dict(
                    autorange="reversed",
                    tickmode="linear",
                    tick0=1,
                    dtick=1,
                    title="Rank",
                    range=[n_teams + 0.3, 0.7],
                ),
                xaxis=dict(title=""),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                height=420,
                margin=dict(l=40, r=20, t=40, b=40),
                plot_bgcolor="#f8fdf6",
                paper_bgcolor="#ffffff",
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True)

        except ImportError:
            # Fallback: simple table if plotly not installed
            st.info("Install plotly for the interactive chart: `uv add plotly`")
            pivot = plot_df.pivot(index="Tournament", columns="Team", values="Rank")
            st.dataframe(pivot, width="stretch")

# ─────────────────────────────────────────────
# PAGE: TOURNAMENTS
# ─────────────────────────────────────────────

elif page == "🗓️ Tournaments":
    if not st.session_state.is_admin:
        st.error("🔒 Admin access required.")
        st.stop()
    st.title("🗓️ Tournaments")
    tab1, tab2 = st.tabs(["📋 View Results", "✏️ Manual Entry / Edit"])

    with tab1:
        if not data["tournaments"]:
            st.info("No tournament results yet. Import them via the Live Leaderboard page, or use Manual Entry.")
        else:
            standings = compute_standings(data)
            selected_t = st.selectbox("Select Tournament", sorted(data["tournaments"].keys()))
            if selected_t:
                results = data["tournaments"][selected_t].get("results", {})
                st.subheader(f"{selected_t}")

                # Team breakdown
                sorted_teams_t = sorted(
                    data["teams"].items(),
                    key=lambda x: standings[x[0]]["tournaments"].get(selected_t, {}).get("total", 0),
                    reverse=True
                )
                rank_labels = ["🥇", "🥈", "🥉"]
                team_cols = st.columns(min(len(data["teams"]), 3))
                for i, (team_name, _) in enumerate(sorted_teams_t):
                    t_data = standings[team_name]["tournaments"].get(selected_t, {})
                    total = t_data.get("total", 0)
                    top3 = t_data.get("top3", [])
                    medal = rank_labels[i] if i < 3 else f"#{i+1}"
                    with team_cols[i % 3]:
                        st.markdown(f"**{medal} {team_name}** — {fmt_money(total)}")
                        for g, m in top3:
                            st.markdown(f"&nbsp;&nbsp;💰 {g}: {fmt_money(m)}")
                        if not top3:
                            st.caption("No scoring golfers")
                        st.markdown("---")

                # ── Recalculate button ──────────────────────────────────
                all_golfers_in_league = sorted(set(g for gs in data["teams"].values() for g in gs))
                missing = [g for g in all_golfers_in_league if g not in results]
                if missing:
                    st.warning(
                        f"{len(missing)} golfer(s) from current rosters are missing from this tournament's results: "
                        + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")
                    )
                    st.markdown(
                        "This happens when rosters were added after the tournament was imported. "
                        "If these golfers are in the saved results data (just under a different name, or were in the field), "
                        "click **Recalculate** to pull their prizes from the existing data."
                    )
                    if st.button(f"🔄 Recalculate '{selected_t}' with current rosters", type="primary"):
                        # Any golfer already in results keeps their data.
                        # Any new golfer gets not_entered (user can manually correct if needed).
                        for g in missing:
                            results[g] = {"prize": 0, "status": "not_entered"}
                        data["tournaments"][selected_t]["results"] = results
                        save_data(data)
                        st.success(
                            f"Updated! {len(missing)} golfer(s) added as 'not_entered'. "
                            "If any of them actually played, edit their results manually in the Manual Entry tab."
                        )
                        st.rerun()

                # Full results table for this tournament
                if results:
                    st.subheader("All Golfer Results")
                    res_rows = []
                    for g in all_golfers_in_league:
                        entry = results.get(g, {"prize": 0, "status": "not_entered"})
                        prize = get_prize(entry)
                        status = get_status(entry)
                        team = next((t for t, gs in data["teams"].items() if g in gs), "?")
                        res_rows.append({
                            "Golfer": g,
                            "Team": team,
                            "Status": STATUS_EMOJI.get(status, status),
                            "Prize": prize,
                        })
                    res_rows.sort(key=lambda x: x["Prize"], reverse=True)
                    res_df = pd.DataFrame(res_rows).style.format({"Prize": fmt_money})
                    st.dataframe(res_df, width="stretch", hide_index=True)

    with tab2:
        st.subheader("Manual Entry / Edit")
        st.info(
            "💡 Tip: Use the **Live Leaderboard** page to auto-import results after a tournament ends. "
            "Manual entry is here as a fallback or for corrections."
        )

        mode = st.radio("Action", ["Create new tournament", "Edit existing tournament"],
                        horizontal=True, label_visibility="collapsed")

        if mode == "Create new tournament":
            new_t_name = st.text_input("Tournament Name")
            if st.button("Create Tournament") and new_t_name:
                if new_t_name not in data["tournaments"]:
                    data["tournaments"][new_t_name] = {"results": {}}
                    save_data(data)
                    st.success(f"Created '{new_t_name}'")
                    st.rerun()
                else:
                    st.warning("Already exists.")

        if data["tournaments"]:
            edit_t = st.selectbox("Tournament to edit", sorted(data["tournaments"].keys()), key="edit_t")
            if edit_t and data["teams"]:
                all_golfers = sorted(set(g for gs in data["teams"].values() for g in gs))
                current_results = data["tournaments"][edit_t].get("results", {})

                with st.form("results_form"):
                    st.markdown("For each golfer, enter their prize money and status.")
                    new_results = {}
                    n_cols = 3
                    for i in range(0, len(all_golfers), n_cols):
                        cols = st.columns(n_cols)
                        for j, golfer in enumerate(all_golfers[i:i+n_cols]):
                            entry = current_results.get(golfer, {"prize": 0, "status": "not_entered"})
                            cur_prize = int(get_prize(entry))
                            cur_status = get_status(entry)
                            with cols[j]:
                                st.markdown(f"**{golfer}**")
                                prize_val = st.number_input(
                                    f"Prize ({golfer})", min_value=0, value=cur_prize,
                                    step=1000, key=f"prize_{edit_t}_{golfer}",
                                    label_visibility="collapsed"
                                )
                                status_val = st.selectbox(
                                    f"Status ({golfer})",
                                    ["scored", "cut", "wd", "not_entered"],
                                    index=["scored", "cut", "wd", "not_entered"].index(cur_status)
                                    if cur_status in ["scored", "cut", "wd", "not_entered"] else 3,
                                    key=f"status_{edit_t}_{golfer}",
                                    label_visibility="collapsed"
                                )
                                new_results[golfer] = {"prize": prize_val, "status": status_val}

                    if st.form_submit_button("💾 Save Results", type="primary"):
                        data["tournaments"][edit_t]["results"] = new_results
                        save_data(data)
                        st.success(f"Saved results for {edit_t}")
                        st.rerun()

            st.markdown("---")
            del_t = st.selectbox("Delete a tournament", sorted(data["tournaments"].keys()), key="del_t")
            if st.button("🗑️ Delete Tournament", type="secondary"):
                del data["tournaments"][del_t]
                save_data(data)
                st.success(f"Deleted {del_t}")
                st.rerun()

# ─────────────────────────────────────────────
# PAGE: TEAMS
# ─────────────────────────────────────────────

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
            st.markdown(f"""
            <div class="metric-card">
                <div style="font-size:1rem;color:#555;">Season Rank</div>
                <div class="big-number">{rank_labels.get(rank, f'#{rank}')}</div>
            </div>
            <div class="metric-card">
                <div style="font-size:1rem;color:#555;">Total Earnings</div>
                <div class="big-number">{fmt_money(team_info['total'])}</div>
            </div>""", unsafe_allow_html=True)
            st.markdown("**Drafted Golfers:**")
            for g in sorted(golfers):
                st.markdown(f"• {g}")

        with col2:
            st.subheader("Tournament Breakdown")
            t_rows = []
            for t_name, t_data in sorted(team_info["tournaments"].items()):
                top3 = t_data.get("top3", [])
                scorers = ", ".join(f"{g} ({fmt_money(m)})" for g, m in top3) if top3 else "—"
                t_rows.append({"Tournament": t_name, "Top 3 Total": t_data["total"], "Scoring Golfers": scorers})
            if t_rows:
                st.dataframe(
                    pd.DataFrame(t_rows).style.format({"Top 3 Total": fmt_money}),
                    width="stretch", hide_index=True
                )

            st.subheader("Golfer Detail")
            ge = defaultdict(lambda: {"cashes": 0, "cuts": 0, "wds": 0, "not_entered": 0,
                                       "total_prize": 0, "counted": 0})
            for t_name, t_info_item in data["tournaments"].items():
                results = t_info_item.get("results", {})
                team_t = team_info["tournaments"].get(t_name, {})
                top3_names = {g for g, _ in team_t.get("top3", [])}
                for g in golfers:
                    entry = results.get(g, {"prize": 0, "status": "not_entered"})
                    prize = get_prize(entry)
                    status = get_status(entry)
                    if status == "cut":
                        ge[g]["cuts"] += 1
                    elif status == "wd":
                        ge[g]["wds"] += 1
                    elif status == "not_entered":
                        ge[g]["not_entered"] += 1
                    else:
                        if prize > 0:
                            ge[g]["cashes"] += 1
                            ge[g]["total_prize"] += prize
                    if g in top3_names:
                        ge[g]["counted"] += prize

            ge_rows = [{
                "Golfer": g,
                "Cashes": ge[g]["cashes"],
                "Cuts": ge[g]["cuts"],
                "WD/DQ": ge[g]["wds"],
                "Not Entered": ge[g]["not_entered"],
                "Total Prize": ge[g]["total_prize"],
                "Counted for Team": ge[g]["counted"],
            } for g in sorted(golfers)]
            ge_df = pd.DataFrame(ge_rows).sort_values("Counted for Team", ascending=False)
            st.dataframe(
                ge_df.style.format({"Total Prize": fmt_money, "Counted for Team": fmt_money}),
                width="stretch", hide_index=True
            )

# ─────────────────────────────────────────────
# PAGE: PLAYER STATS
# ─────────────────────────────────────────────

elif page == "📊 Player Stats":
    st.title("📊 Player Stats")

    if not data["teams"]:
        st.info("No teams set up yet.")
        st.stop()

    all_golfers = sorted(set(g for gs in data["teams"].values() for g in gs))
    golfer_to_team = {g: t for t, gs in data["teams"].items() for g in gs}

    rows = []
    for golfer in all_golfers:
        team = golfer_to_team.get(golfer, "?")
        stats = {"cashes": 0, "cuts": 0, "wds": 0, "not_entered": 0,
                 "total_prize": 0, "counted": 0}
        for t_name, t_info in data["tournaments"].items():
            results = t_info.get("results", {})
            entry = results.get(golfer, {"prize": 0, "status": "not_entered"})
            prize = get_prize(entry)
            status = get_status(entry)
            if status == "cut":
                stats["cuts"] += 1
            elif status == "wd":
                stats["wds"] += 1
            elif status == "not_entered":
                stats["not_entered"] += 1
            else:
                if prize > 0:
                    stats["cashes"] += 1
                    stats["total_prize"] += prize
            # Check if counted for team
            team_golfers = data["teams"].get(team, [])
            t_results_raw = {g2: get_prize(results.get(g2, 0)) for g2 in team_golfers}
            _, top3 = get_team_earnings_for_tournament(team_golfers, results)
            if golfer in {g2 for g2, _ in top3}:
                stats["counted"] += prize

        rows.append({
            "Golfer": golfer, "Team": team,
            "Cashes": stats["cashes"], "Cuts": stats["cuts"],
            "WD/DQ": stats["wds"], "Not Entered": stats["not_entered"],
            "Total Prize": stats["total_prize"], "Counted for Team": stats["counted"],
        })

    df = pd.DataFrame(rows)

    col1, col2 = st.columns(2)
    with col1:
        team_filter = st.multiselect("Filter by Team", sorted(data["teams"].keys()))
    with col2:
        sort_col = st.selectbox("Sort By", ["Counted for Team", "Total Prize", "Cashes", "Cuts"])

    if team_filter:
        df = df[df["Team"].isin(team_filter)]
    df = df.sort_values(sort_col, ascending=False)

    st.dataframe(
        df.style.format({"Total Prize": fmt_money, "Counted for Team": fmt_money}),
        width="stretch", hide_index=True
    )

    if len(df) > 0:
        st.markdown("---")
        st.subheader("🌟 Top Performers")
        c1, c2, c3 = st.columns(3)
        with c1:
            top = df.sort_values("Counted for Team", ascending=False).iloc[0]
            st.markdown("**Most Counted for Team**")
            st.metric(top["Golfer"], fmt_money(top["Counted for Team"]), f"({top['Team']})")
        with c2:
            top = df.sort_values("Total Prize", ascending=False).iloc[0]
            st.markdown("**Most Prize Money**")
            st.metric(top["Golfer"], fmt_money(top["Total Prize"]), f"({top['Team']})")
        with c3:
            top = df.sort_values("Cuts", ascending=False).iloc[0]
            st.markdown("**Most Cuts Made (bad)**")
            st.metric(top["Golfer"], f"{int(top['Cuts'])} cuts", f"({top['Team']})")

# ─────────────────────────────────────────────
# PAGE: SETUP
# ─────────────────────────────────────────────

elif page == "⚙️ Setup":
    if not st.session_state.is_admin:
        st.error("🔒 Admin access required.")
        st.stop()
    st.title("⚙️ League Setup")
    tab1, tab2, tab3, tab4 = st.tabs(["Add Team", "Edit Team Roster", "Tournament Order", "Import / Export"])

    with tab1:
        st.subheader("Create a New Team")
        new_team_name = st.text_input("Team / Owner Name")
        golfer_input = st.text_area(
            "Golfers (one per line)", height=300,
            placeholder="Scottie Scheffler\nRory McIlroy\nXander Schauffele\n..."
        )
        if st.button("➕ Add Team", type="primary") and new_team_name and golfer_input:
            golfers = [g.strip() for g in golfer_input.strip().split("\n") if g.strip()]
            if new_team_name in data["teams"]:
                st.warning("Already exists — use Edit tab to modify.")
            else:
                data["teams"][new_team_name] = golfers
                save_data(data)
                st.success(f"Added '{new_team_name}' with {len(golfers)} golfers!")
                st.rerun()

        st.info(
            "**Name matching:** Golfer names must match ESPN exactly (e.g. 'Scottie Scheffler'). "
            "After fetching the leaderboard, check the Full Leaderboard tab — "
            "⭐ marks players already matched to a team. Fix mismatches here."
        )

    with tab2:
        if not data["teams"]:
            st.info("No teams yet.")
        else:
            edit_team = st.selectbox("Select Team", sorted(data["teams"].keys()))
            updated = st.text_area("Golfers (one per line)",
                                   value="\n".join(data["teams"].get(edit_team, [])), height=300)
            col1, col2 = st.columns(2)
            with col1:
                if st.button("💾 Save", type="primary"):
                    data["teams"][edit_team] = [g.strip() for g in updated.strip().split("\n") if g.strip()]
                    save_data(data)
                    st.success("Saved!")
                    st.rerun()
            with col2:
                if st.button("🗑️ Delete Team", type="secondary"):
                    del data["teams"][edit_team]
                    save_data(data)
                    st.rerun()

    with tab3:
        st.subheader("Tournament Order")
        st.markdown(
            "Set the order tournaments appear in the standings table and the rank history chart. "
            "Move them up/down using the buttons."
        )
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
        st.subheader("Backup & Restore")
        col1, col2 = st.columns(2)
        with col1:
            st.download_button("⬇️ Download Backup", data=json.dumps(data, indent=2),
                               file_name="fantasy_golf_backup.json", mime="application/json")
        with col2:
            uploaded = st.file_uploader("Upload backup", type="json")
            if uploaded:
                imported = json.load(uploaded)
                st.session_state.data = imported
                save_data(imported)
                st.success("Imported!")
                st.rerun()

        st.markdown("---")
        st.markdown(
            f"**Teams:** {len(data['teams'])} | "
            f"**Golfers:** {sum(len(v) for v in data['teams'].values())} | "
            f"**Tournaments:** {len(data['tournaments'])}"
        )