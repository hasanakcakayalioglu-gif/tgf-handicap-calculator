"""
TGF Playing Handicap Calculator – Web Interface
=================================================
A browser-based UI for looking up player handicaps from the
Turkish Golf Federation and calculating playing handicaps.

Usage:
    python tgf_playing_handicap_web.py

Then open http://localhost:5000 in your browser.
"""

import sys, os

# Fix Windows encoding before any output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Make sure we can import the backend module sitting next to this file
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, render_template_string
import tgf_playing_handicap as tgf  # reuse all backend logic
import requests
import threading
import time
from datetime import date

app = Flask(__name__)

# ── Cache courses so we don't re-fetch on every request ──────────────
_course_cache: list[dict] = []


def _get_courses_cached() -> list[dict]:
    global _course_cache
    if not _course_cache:
        _course_cache = tgf.get_courses()
    return _course_cache


# ── Cache TGF session & player lookups ───────────────────────────────

_tgf_session_lock = threading.Lock()
_tgf_session: requests.Session | None = None
_tgf_session_time: float = 0

_player_cache_lock = threading.Lock()
_player_cache: dict[str, dict] = {}   # {query_lower: {"players": [...], "date": date}}


def _get_or_create_tgf_session() -> requests.Session | None:
    """Return a cached TGF session, creating a new one if stale (>5 min)."""
    global _tgf_session, _tgf_session_time
    with _tgf_session_lock:
        now = time.time()
        if _tgf_session and (now - _tgf_session_time) < 300:
            return _tgf_session

        session = tgf._create_authenticated_session("handicaps", "&ccode=All")
        if session is None:
            return None

        # Visit the list page so ASP.NET sets up server-side state
        try:
            session.get(tgf.BASE_URL + "FederatedsList_V2.aspx?ccode=All", timeout=15)
        except Exception:
            pass

        _tgf_session = session
        _tgf_session_time = now
        return session


def _invalidate_tgf_session():
    """Force the next call to create a fresh session."""
    global _tgf_session, _tgf_session_time
    with _tgf_session_lock:
        _tgf_session = None
        _tgf_session_time = 0


def _search_with_session(session: requests.Session, query: str, is_fedno: bool) -> list[dict]:
    """Search for a player using an already-authenticated session."""
    api_url = tgf.BASE_URL + "FederatedsList_V2.aspx/HandicapsLST"
    payload = {
        "name": "" if is_fedno else query,
        "fedno": query if is_fedno else "",
        "ClubCode": "All", "FedStat": "9", "Gender": "All",
        "Agelev": "All", "HcpStat": "All", "FHcp": "", "THcp": "",
        "ProAm": "All", "IniFlag": "0", "FAge": "", "TAge": "",
        "Permit": "", "MaxResults": "0", "MessMax": "",
        "jtStartIndex": 0, "jtPageSize": 100, "jtSorting": "name ASC",
    }
    resp = session.post(
        api_url, json=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": tgf.BASE_URL + "FederatedsList_V2.aspx",
        },
        timeout=15,
    )
    resp.raise_for_status()
    records = resp.json().get("d", {}).get("Records", [])

    players = []
    for r in records:
        hcp_raw = r.get("hcp_exact")
        players.append({
            "fed_no": r.get("federation_code"),
            "name": r.get("name"),
            "club": r.get("acronym"),
            "club_code": r.get("club_code"),
            "hcp_index": hcp_raw / 10.0 if hcp_raw is not None else None,
            "hcp_status": r.get("hcp_status"),
            "gender": r.get("gender"),
            "age_group": r.get("age_level"),
        })
    return players


# ── API endpoints ────────────────────────────────────────────────────

@app.route("/api/search_player", methods=["POST"])
def api_search_player():
    """Search for a player by name or federation number (with caching)."""
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Empty query"}), 400

    is_fedno = query.isdigit()
    cache_key = query.lower()
    today = date.today()

    # ── Check server-side player cache (same day) ──
    with _player_cache_lock:
        cached = _player_cache.get(cache_key)
        if cached and cached["date"] == today:
            players = cached["players"]
            active = [p for p in players
                      if p["hcp_index"] is not None and p["hcp_status"] == "Aktif"]
            return jsonify({"players": active, "total_raw": len(players), "cached": True})

    # ── Not cached – search using shared TGF session ──
    players = []
    try:
        session = _get_or_create_tgf_session()
        if session:
            players = _search_with_session(session, query, is_fedno)
    except Exception as e:
        print(f"[search] Session-based search failed: {e}")
        _invalidate_tgf_session()

    # ── Fallback to Selenium if session approach yielded nothing ──
    if not players:
        try:
            if is_fedno:
                players = tgf._search_by_fedno_selenium(query)
            else:
                print(f"[search] API returned no results for '{query}', trying Selenium...")
                players = tgf.search_player_selenium(query)
        except Exception as e2:
            print(f"[search] Selenium fallback also failed: {e2}")
            players = []

    # ── Cache successful results ──
    if players:
        with _player_cache_lock:
            _player_cache[cache_key] = {"players": list(players), "date": today}

    active = [p for p in players
              if p["hcp_index"] is not None and p["hcp_status"] == "Aktif"]

    if not players:
        return jsonify({"players": [], "total_raw": 0,
                        "error": "TGF server did not respond. Please try again."})

    return jsonify({"players": active, "total_raw": len(players), "cached": False})


@app.route("/api/courses", methods=["GET"])
def api_courses():
    """Return all courses grouped by base name."""
    courses = _get_courses_cached()

    # Group by base name
    grouped: dict[str, list[dict]] = {}
    for c in courses:
        base = c["name"].rsplit(" - ", 1)[0]
        tee = c["name"].rsplit(" - ", 1)[-1] if " - " in c["name"] else ""
        entry = {**c, "base_name": base, "tee": tee}
        grouped.setdefault(base, []).append(entry)

    return jsonify({"courses": grouped})


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    """Calculate playing handicaps for given players on a course."""
    data = request.get_json(force=True)
    players = data.get("players", [])      # [{name, hcp_index}, ...]
    course_base = data.get("course", "")

    courses = _get_courses_cached()
    matching = [c for c in courses
                if c["name"].rsplit(" - ", 1)[0] == course_base]

    if not matching:
        return jsonify({"error": f"Course '{course_base}' not found"}), 404

    tees_sorted = sorted(matching, key=lambda c: c["slope_18"], reverse=True)

    results = []
    for c in tees_sorted:
        tee = c["name"].rsplit(" - ", 1)[-1] if " - " in c["name"] else c["name"]
        row = {
            "tee": tee,
            "par": c["par_18"],
            "rating": c["cr_18"],
            "slope": c["slope_18"],
            "handicaps": {},
        }
        for p in players:
            hcp = p.get("hcp_index")
            if hcp is not None:
                phcp = tgf.calc_playing_handicap(
                    hcp, c["slope_18"], c["cr_18"], c["par_18"]
                )
                row["handicaps"][p["name"]] = phcp
            else:
                row["handicaps"][p["name"]] = None
        results.append(row)

    return jsonify({"course": course_base, "tees": results, "players": players})


# ── Main HTML page ───────────────────────────────────────────────────

HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TGF Playing Handicap Calculator</title>
<style>
  :root {
    --green: #2e7d32;
    --green-light: #4caf50;
    --green-bg: #e8f5e9;
    --dark: #1b5e20;
    --gray: #f5f5f5;
    --border: #c8e6c9;
    --white: #ffffff;
    --shadow: 0 2px 8px rgba(0,0,0,.1);
    --radius: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--gray);
    color: #333;
    min-height: 100vh;
  }
  header {
    background: linear-gradient(135deg, var(--dark), var(--green));
    color: white;
    padding: 1.2rem 2rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    box-shadow: var(--shadow);
  }
  header h1 { font-size: 1.4rem; font-weight: 600; }
  header .subtitle { font-size: .85rem; opacity: .85; }
  .container { max-width: 1100px; margin: 1.5rem auto; padding: 0 1rem; }

  /* ── Cards ── */
  .card {
    background: var(--white);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 1.2rem;
    overflow: hidden;
  }
  .card-header {
    background: var(--green-bg);
    border-bottom: 2px solid var(--border);
    padding: .8rem 1.2rem;
    font-weight: 600;
    color: var(--dark);
    display: flex;
    align-items: center;
    gap: .5rem;
  }
  .card-header .step {
    background: var(--green);
    color: white;
    width: 26px; height: 26px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: .8rem;
  }
  .card-body { padding: 1.2rem; }

  /* ── Inputs ── */
  .player-row {
    display: flex;
    gap: .5rem;
    margin-bottom: .5rem;
    align-items: center;
  }
  .player-row input {
    flex: 1;
    padding: .55rem .75rem;
    border: 1px solid #ccc;
    border-radius: 6px;
    font-size: .95rem;
    transition: border-color .2s;
  }
  .player-row input:focus { outline: none; border-color: var(--green-light); box-shadow: 0 0 0 3px rgba(76,175,80,.15); }
  .player-row .remove-btn {
    background: none; border: none; color: #c62828; cursor: pointer;
    font-size: 1.3rem; padding: 0 .3rem; line-height: 1;
  }
  .player-row .remove-btn:hover { color: #b71c1c; }
  .player-row .status {
    font-size: .8rem;
    min-width: 180px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .status.found { color: var(--green); }
  .status.error { color: #c62828; }
  .status.searching { color: #f57c00; }

  .btn {
    padding: .55rem 1.2rem;
    border: none;
    border-radius: 6px;
    font-size: .9rem;
    cursor: pointer;
    font-weight: 500;
    transition: background .2s, transform .1s;
  }
  .btn:active { transform: scale(.97); }
  .btn-green { background: var(--green); color: white; }
  .btn-green:hover { background: var(--green-light); }
  .btn-outline { background: white; color: var(--green); border: 1px solid var(--green); }
  .btn-outline:hover { background: var(--green-bg); }
  .btn-sm { padding: .4rem .8rem; font-size: .82rem; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }

  .btn-row { display: flex; gap: .5rem; margin-top: .8rem; flex-wrap: wrap; }

  /* ── Course select ── */
  .course-search {
    position: relative;
  }
  .course-search input {
    width: 100%;
    padding: .55rem .75rem;
    border: 1px solid #ccc;
    border-radius: 6px;
    font-size: .95rem;
  }
  .course-search input:focus { outline: none; border-color: var(--green-light); box-shadow: 0 0 0 3px rgba(76,175,80,.15); }
  .course-dropdown {
    position: absolute;
    top: 100%;
    left: 0; right: 0;
    background: white;
    border: 1px solid #ccc;
    border-top: none;
    border-radius: 0 0 6px 6px;
    max-height: 250px;
    overflow-y: auto;
    z-index: 100;
    display: none;
    box-shadow: 0 4px 12px rgba(0,0,0,.15);
  }
  .course-dropdown.open { display: block; }
  .course-item {
    padding: .5rem .75rem;
    cursor: pointer;
    font-size: .9rem;
    border-bottom: 1px solid #f0f0f0;
  }
  .course-item:hover { background: var(--green-bg); }
  .course-item .tees { font-size: .75rem; color: #666; margin-top: 2px; }
  .course-selected {
    margin-top: .6rem;
    padding: .6rem;
    background: var(--green-bg);
    border-radius: 6px;
    font-size: .9rem;
  }
  .course-selected strong { color: var(--dark); }

  /* ── Tee badges ── */
  .tee-badge {
    display: inline-block;
    padding: .15rem .4rem;
    border-radius: 4px;
    font-size: .75rem;
    font-weight: 600;
    min-width: 45px;
    text-align: center;
  }
  .tee-WHITE  { background: #f5f5f5; color: #333; border: 1px solid #bbb; }
  .tee-BLACK  { background: #212121; color: #fff; }
  .tee-YELLOW { background: #fdd835; color: #333; }
  .tee-BLUE   { background: #1565c0; color: #fff; }
  .tee-RED    { background: #c62828; color: #fff; }
  .tee-GREEN  { background: #2e7d32; color: #fff; }
  .tee-GOLD   { background: #f9a825; color: #333; }

  /* ── Results table ── */
  .table-scroll {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  .results-table {
    width: 100%;
    border-collapse: collapse;
    font-size: .85rem;
  }
  .results-table th {
    background: var(--dark);
    color: white;
    padding: .4rem .4rem;
    text-align: center;
    font-weight: 500;
    white-space: nowrap;
  }
  .results-table th:first-child { text-align: left; }
  .results-table td {
    padding: .4rem .4rem;
    text-align: center;
    border-bottom: 1px solid #e0e0e0;
    white-space: nowrap;
  }
  .results-table td:first-child { text-align: left; }
  .results-table tr:hover td { background: #f1f8e9; }
  .results-table .hcp-cell {
    font-weight: 700;
    font-size: 1.05rem;
    color: var(--dark);
  }
  .player-hdr { font-size: .7rem; color: rgba(255,255,255,.7); font-weight: 400; }
  .player-name { font-size: .8rem; line-height: 1.2; }

  /* ── Disambiguation modal ── */
  .modal-overlay {
    position: fixed; top:0;left:0;right:0;bottom:0;
    background: rgba(0,0,0,.45);
    z-index: 500;
    display: flex; align-items: center; justify-content: center;
  }
  .modal {
    background: white;
    border-radius: var(--radius);
    box-shadow: 0 8px 32px rgba(0,0,0,.25);
    max-width: 700px; width: 95%;
    max-height: 80vh;
    overflow-y: auto;
    padding: 1.5rem;
  }
  .modal h3 { margin-bottom: 1rem; color: var(--dark); }
  .modal table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  .modal th { background: var(--green-bg); padding: .5rem; text-align: left; }
  .modal td { padding: .5rem; border-bottom: 1px solid #eee; }
  .modal tr.selectable { cursor: pointer; }
  .modal tr.selectable:hover td { background: #e8f5e9; }

  /* ── Spinner ── */
  .spinner {
    display: inline-block;
    width: 16px; height: 16px;
    border: 2px solid var(--green-bg);
    border-top-color: var(--green);
    border-radius: 50%;
    animation: spin .6s linear infinite;
    vertical-align: middle;
    margin-right: 4px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .hidden { display: none !important; }

  /* ── Footer ── */
  footer {
    text-align: center;
    padding: 1.5rem;
    color: #888;
    font-size: .8rem;
  }
  footer a { color: var(--green); text-decoration: none; }

  /* responsive */
  @media (max-width: 600px) {
    header { padding: .8rem 1rem; }
    header h1 { font-size: 1.1rem; }
    .card-body { padding: .8rem; }
    .player-row { flex-wrap: wrap; }
    .player-row .status { min-width: 100%; }
  }
</style>
</head>
<body>

<header>
  <div>
    <h1>TGF Playing Handicap Calculator</h1>
    <div class="subtitle">Turkish Golf Federation &middot; WHS Playing Handicap</div>
  </div>
</header>

<div class="container">

  <!-- ════ STEP 1: Players ════ -->
  <div class="card">
    <div class="card-header">
      <span class="step">1</span> Players
    </div>
    <div class="card-body">
      <p style="font-size:.85rem; color:#666; margin-bottom:.8rem;">
        Enter player names or TGF Federation Numbers. One per row.
      </p>
      <div id="playerRows"></div>
      <div class="btn-row">
        <button class="btn btn-outline btn-sm" onclick="addPlayerRow()">+ Add Player</button>
        <button class="btn btn-green" id="btnSearchPlayers" onclick="searchAllPlayers()">
          Search Players
        </button>
      </div>
    </div>
  </div>

  <!-- ════ STEP 2: Course ════ -->
  <div class="card">
    <div class="card-header">
      <span class="step">2</span> Course
    </div>
    <div class="card-body">
      <div class="course-search">
        <input type="text" id="courseInput" placeholder="Type a course name (e.g. Kemer, Gloria, Carya...)"
               oninput="filterCourses()" onfocus="filterCourses()" onkeydown="courseKeydown(event)" autocomplete="off">
        <div class="course-dropdown" id="courseDropdown"></div>
      </div>
      <div id="courseInfo" class="hidden"></div>
    </div>
  </div>

  <!-- ════ STEP 3: Results ════ -->
  <div class="card" id="resultsCard" style="display:none">
    <div class="card-header">
      <span class="step">3</span> Playing Handicaps
    </div>
    <div class="card-body" id="resultsBody"></div>
  </div>

</div>

<footer>
  Data from <a href="https://www.tgf.org.tr" target="_blank">tgf.org.tr</a> &middot;
  Formula: Playing HCP = round(HCP Index &times; Slope / 113 + (Rating &minus; PAR))
</footer>

<!-- ════ Disambiguation modal (hidden) ════ -->
<div id="disambigModal" class="modal-overlay hidden">
  <div class="modal">
    <h3 id="disambigTitle">Select Player</h3>
    <table>
      <thead>
        <tr><th>#</th><th>Name</th><th>Fed.No</th><th>Club</th><th>HCP</th><th>Gender</th><th>Age</th></tr>
      </thead>
      <tbody id="disambigBody"></tbody>
    </table>
  </div>
</div>

<script>
// ── State ──
let confirmedPlayers = [];   // [{name, fed_no, club, hcp_index, gender, ...}]
let allCourses = {};         // {baseName: [{name, tee, par_18, cr_18, slope_18, ...}]}
let selectedCourse = null;   // base name string
let playerCache = {};        // {query_lower: confirmedPlayer} – avoids redundant lookups

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
  addPlayerRow();
  addPlayerRow();
  loadCourses();
  document.addEventListener('click', e => {
    if (!e.target.closest('.course-search')) {
      document.getElementById('courseDropdown').classList.remove('open');
    }
  });
});

// ── Player rows ──
function addPlayerRow(value) {
  const div = document.getElementById('playerRows');
  const row = document.createElement('div');
  row.className = 'player-row';
  row.innerHTML = `
    <input type="text" placeholder="Player name or Federation Number" value="${value || ''}"
           onkeydown="if(event.key==='Enter') searchAllPlayers()">
    <span class="status"></span>
    <button class="remove-btn" title="Remove" onclick="removePlayerRow(this)">&times;</button>
  `;
  div.appendChild(row);
  row.querySelector('input').focus();
}

function removePlayerRow(btn) {
  const rows = document.querySelectorAll('.player-row');
  if (rows.length <= 1) return;  // keep at least one
  btn.closest('.player-row').remove();
}

// ── Search players ──
async function searchAllPlayers() {
  const rows = document.querySelectorAll('.player-row');
  const btn = document.getElementById('btnSearchPlayers');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Searching...';

  confirmedPlayers = [];   // rebuild from all rows (cached + fresh)

  for (const row of rows) {
    const input = row.querySelector('input');
    const status = row.querySelector('.status');
    const query = input.value.trim();
    if (!query) {
      status.textContent = '';
      status.className = 'status';
      continue;
    }

    const cacheKey = query.toLowerCase();

    // ── Use cached result if available (avoids redundant lookups) ──
    if (playerCache[cacheKey]) {
      pickPlayer(playerCache[cacheKey], status);
      continue;
    }

    status.innerHTML = '<span class="spinner"></span> Searching...';
    status.className = 'status searching';

    try {
      const resp = await fetch('/api/search_player', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({query})
      });
      const data = await resp.json();

      if (data.error && (!data.players || data.players.length === 0)) {
        status.textContent = data.error;
        status.className = 'status error';
        continue;
      }

      const players = data.players || [];
      if (players.length === 0) {
        status.textContent = 'No active player found — try again if TGF server was slow';
        status.className = 'status error';
      } else if (players.length === 1) {
        playerCache[cacheKey] = players[0];
        pickPlayer(players[0], status);
      } else {
        // Try exact match first
        const queryLower = query.toLowerCase();
        const exact = players.filter(p => p.name.toLowerCase() === queryLower);
        if (exact.length === 1) {
          playerCache[cacheKey] = exact[0];
          pickPlayer(exact[0], status);
        } else {
          // Disambiguate
          const chosen = await showDisambig(query, exact.length > 1 ? exact : players);
          if (chosen) {
            playerCache[cacheKey] = chosen;
            pickPlayer(chosen, status);
          } else {
            status.textContent = 'No player selected';
            status.className = 'status error';
          }
        }
      }
    } catch (err) {
      status.textContent = 'Network error';
      status.className = 'status error';
    }
  }

  btn.disabled = false;
  btn.innerHTML = 'Search Players';
  tryCalculate();
}

function pickPlayer(p, statusEl) {
  confirmedPlayers.push(p);
  statusEl.innerHTML = `&#10003; <b>${p.name}</b> &middot; HCP ${p.hcp_index} &middot; ${p.club}`;
  statusEl.className = 'status found';
}

// ── Disambiguation modal ──
function showDisambig(query, players) {
  return new Promise(resolve => {
    const modal = document.getElementById('disambigModal');
    const title = document.getElementById('disambigTitle');
    const body = document.getElementById('disambigBody');

    title.textContent = `Multiple players found for "${query}" — click to select:`;
    body.innerHTML = '';

    players.forEach((p, i) => {
      const tr = document.createElement('tr');
      tr.className = 'selectable';
      tr.innerHTML = `
        <td>${i+1}</td>
        <td><b>${p.name}</b></td>
        <td>${p.fed_no}</td>
        <td>${p.club} (${p.club_code})</td>
        <td>${p.hcp_index}</td>
        <td>${p.gender}</td>
        <td>${p.age_group}</td>
      `;
      tr.addEventListener('click', () => {
        modal.classList.add('hidden');
        resolve(p);
      });
      body.appendChild(tr);
    });

    modal.classList.remove('hidden');
    // close on overlay click
    modal.addEventListener('click', function handler(e) {
      if (e.target === modal) {
        modal.classList.add('hidden');
        modal.removeEventListener('click', handler);
        resolve(null);
      }
    });
  });
}

// ── Course loading & filtering ──
async function loadCourses() {
  try {
    const resp = await fetch('/api/courses');
    const data = await resp.json();
    allCourses = data.courses || {};
  } catch (e) {
    console.error('Failed to load courses', e);
  }
}

function filterCourses() {
  const q = document.getElementById('courseInput').value.trim().toLowerCase();
  const dd = document.getElementById('courseDropdown');
  dd.innerHTML = '';

  if (q.length < 1) { dd.classList.remove('open'); return; }

  const names = Object.keys(allCourses).filter(n => n.toLowerCase().includes(q)).sort();

  if (names.length === 0) {
    dd.innerHTML = '<div class="course-item" style="color:#999">No courses found</div>';
    dd.classList.add('open');
    return;
  }

  names.slice(0, 20).forEach(name => {
    const tees = allCourses[name].map(c => c.tee).join(', ');
    const div = document.createElement('div');
    div.className = 'course-item';
    div.innerHTML = `<div><b>${name}</b></div><div class="tees">Tees: ${tees}</div>`;
    div.addEventListener('click', () => selectCourse(name));
    dd.appendChild(div);
  });
  dd.classList.add('open');
}

function courseKeydown(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();

  const q = document.getElementById('courseInput').value.trim().toLowerCase();
  if (!q) return;

  const names = Object.keys(allCourses).filter(n => n.toLowerCase().includes(q)).sort();
  if (names.length === 0) return;

  // Prefer an exact match (case-insensitive), otherwise pick the first result
  const exact = names.find(n => n.toLowerCase() === q);
  selectCourse(exact || names[0]);
}

function selectCourse(baseName) {
  selectedCourse = baseName;
  document.getElementById('courseInput').value = baseName;
  document.getElementById('courseDropdown').classList.remove('open');

  // Show course info
  const tees = allCourses[baseName] || [];
  const teesSorted = [...tees].sort((a, b) => b.slope_18 - a.slope_18);

  let html = `<div class="course-selected"><strong>${baseName}</strong>
    <table style="width:100%; margin-top:.5rem; font-size:.82rem; border-collapse:collapse;">
    <tr style="background:var(--green-bg)">
      <th style="padding:4px 6px; text-align:left">Tee</th>
      <th style="padding:4px 6px">PAR</th>
      <th style="padding:4px 6px">Rating</th>
      <th style="padding:4px 6px">Slope</th>
    </tr>`;
  teesSorted.forEach(t => {
    const cls = 'tee-' + t.tee;
    html += `<tr>
      <td style="padding:4px 6px"><span class="tee-badge ${cls}">${t.tee}</span></td>
      <td style="padding:4px 6px; text-align:center">${t.par_18}</td>
      <td style="padding:4px 6px; text-align:center">${t.cr_18.toFixed(1)}</td>
      <td style="padding:4px 6px; text-align:center">${t.slope_18}</td>
    </tr>`;
  });
  html += '</table></div>';

  const info = document.getElementById('courseInfo');
  info.innerHTML = html;
  info.classList.remove('hidden');

  tryCalculate();
}

// ── Calculate ──
async function tryCalculate() {
  if (confirmedPlayers.length === 0 || !selectedCourse) return;

  const resp = await fetch('/api/calculate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      players: confirmedPlayers.map(p => ({name: p.name, hcp_index: p.hcp_index})),
      course: selectedCourse
    })
  });
  const data = await resp.json();
  if (data.error) return;

  renderResults(data);
}

function renderResults(data) {
  const card = document.getElementById('resultsCard');
  const body = document.getElementById('resultsBody');

  let html = `<p style="margin-bottom:.8rem; font-size:.95rem;">
    <strong>${data.course}</strong></p>`;

  html += '<div class="table-scroll"><table class="results-table"><thead><tr>';
  html += '<th>Tee</th><th>PAR</th><th>CR</th><th>SL</th>';
  data.players.forEach(p => {
    // Split name: first name on top, surname below
    const parts = p.name.split(' ');
    let nameHtml;
    if (parts.length >= 2) {
      const firstName = parts.slice(0, -1).join(' ');
      const surname = parts[parts.length - 1];
      nameHtml = `<span class="player-name">${firstName}<br>${surname}</span>`;
    } else {
      nameHtml = `<span class="player-name">${p.name}</span>`;
    }
    html += `<th>${nameHtml}<br><span class="player-hdr">HCP ${p.hcp_index}</span></th>`;
  });
  html += '</tr></thead><tbody>';

  data.tees.forEach(t => {
    const cls = 'tee-' + t.tee;
    html += '<tr>';
    html += `<td><span class="tee-badge ${cls}">${t.tee}</span></td>`;
    html += `<td>${t.par}</td>`;
    html += `<td>${t.rating.toFixed(1)}</td>`;
    html += `<td>${t.slope}</td>`;
    data.players.forEach(p => {
      const val = t.handicaps[p.name];
      html += `<td class="hcp-cell">${val !== null ? val : 'N/A'}</td>`;
    });
    html += '</tr>';
  });

  html += '</tbody></table></div>';

  body.innerHTML = html;
  card.style.display = '';
  card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


# ── Run ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import webbrowser, threading

    print()
    print("  TGF Playing Handicap Calculator")
    print("  ================================")
    print("  Loading course data (first time may take a moment)...")
    _get_courses_cached()
    print(f"  Loaded {len(_course_cache)} course/tee combinations.")
    print()
    print("  Open your browser at:  http://localhost:5000")
    print("  Press Ctrl+C to stop.")
    print()

    # Auto-open browser after a short delay
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000")).start()

    app.run(host="127.0.0.1", port=5000, debug=False)
