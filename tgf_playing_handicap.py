"""
TGF Playing Handicap Calculator
================================
Connects to the Turkish Golf Federation (tgf.org.tr) website to:
1. Look up a player's handicap index by name
2. Find course information (slope, rating, par) for all tees
3. Calculate the playing handicap for each tee using the WHS formula

Usage:
    Single player:
        python tgf_playing_handicap.py "Player Name" "Course Name"

    Multiple players (comma-separated):
        python tgf_playing_handicap.py "Player1, Player2, Player3" "Course Name"

Examples:
    python tgf_playing_handicap.py "Ali Akar" "Kemer"
    python tgf_playing_handicap.py "Ali Akar, Mehmet Yılmaz" "Gloria"

Requirements:
    pip install selenium requests beautifulsoup4
"""

import sys
import os
import hmac
import hashlib
import time
import json
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# Fix Windows console encoding for Turkish characters
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "https://scoring.tgf.org.tr/lists/"


# ---------------------------------------------------------------------------
# Session helper – the scoring sub-site requires an HMAC-authenticated hit
# to 1Page.aspx before it will serve any other page.  The hash uses a shared
# secret ("123") and the current day+month+minute, so it is time-sensitive.
# ---------------------------------------------------------------------------

def _create_authenticated_session(page: str, extra_params: str = "") -> requests.Session:
    """Create a requests.Session with a valid ASP.NET session cookie.

    Retries up to 5 times because the hash is minute-based and the server
    can be flaky around the minute boundary.
    """
    for _ in range(5):
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://scoring.tgf.org.tr/lists/1ClubCall.html",
        })

        now = datetime.now()
        dt_str = f"{now.day}{now.month}{now.minute}"
        msg = "admin" + dt_str
        h = hmac.new(b"123", msg.encode(), hashlib.sha1).hexdigest()

        url = (
            f"{BASE_URL}1Page.aspx?user=admin&dt={dt_str}"
            f"&page={page}{extra_params}"
            f"&pagelang=tr&callcontext=clubarea&hash={h}"
        )

        try:
            resp = session.get(url, timeout=15, allow_redirects=False)
            if "ASP.NET_SessionId" in session.cookies:
                return session
        except requests.RequestException:
            pass

        time.sleep(2)

    return None


# ---------------------------------------------------------------------------
# Step 1 – look up player handicap
# ---------------------------------------------------------------------------

def search_player(name: str) -> list[dict]:
    """Search for a player by name on the TGF handicap list.

    Returns a list of matching player dicts.
    """
    session = _create_authenticated_session(
        "handicaps", "&ccode=All"
    )

    if session is None:
        raise RuntimeError(
            "Could not establish a session with the TGF scoring server. "
            "The server may be temporarily unavailable — please try again."
        )

    api_url = BASE_URL + "FederatedsList_V2.aspx/HandicapsLST"
    payload = {
        "name": name,
        "fedno": "",
        "ClubCode": "All",
        "FedStat": "9",
        "Gender": "All",
        "Agelev": "All",
        "HcpStat": "All",
        "FHcp": "",
        "THcp": "",
        "ProAm": "All",
        "IniFlag": "0",
        "FAge": "",
        "TAge": "",
        "Permit": "",
        "MaxResults": "0",
        "MessMax": "",
        "jtStartIndex": 0,
        "jtPageSize": 100,
        "jtSorting": "name ASC",
    }

    resp = session.post(
        api_url,
        json=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE_URL + "FederatedsList_V2.aspx",
        },
        timeout=15,
    )
    resp.raise_for_status()

    data = resp.json()
    records = data.get("d", {}).get("Records", [])

    players = []
    for r in records:
        hcp_raw = r.get("hcp_exact")
        hcp_value = hcp_raw / 10.0 if hcp_raw is not None else None

        players.append({
            "fed_no": r.get("federation_code"),
            "name": r.get("name"),
            "club": r.get("acronym"),
            "club_code": r.get("club_code"),
            "hcp_index": hcp_value,
            "hcp_status": r.get("hcp_status"),
            "gender": r.get("gender"),
            "age_group": r.get("age_level"),
        })

    return players


# ---------------------------------------------------------------------------
# Step 1 (fallback) – scrape the handicap list HTML table with Selenium
# ---------------------------------------------------------------------------

def search_player_selenium(name: str) -> list[dict]:
    """Fallback: use Selenium to search the handicap list when the API fails."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=options)
    players = []

    try:
        driver.get("https://www.tgf.org.tr/tr/handikap-listesi")
        time.sleep(3)

        # Switch to the iframe
        iframe = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        driver.switch_to.frame(iframe)

        # Wait for the search form
        name_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "insrhname"))
        )
        name_input.clear()
        name_input.send_keys(name)

        # Click the search button
        search_btn = driver.find_element(By.ID, "btnSearch")
        search_btn.click()

        # Wait for results
        time.sleep(3)

        # Parse the results table
        rows = driver.find_elements(By.CSS_SELECTOR, ".jtable tbody tr.jtable-data-row")

        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) >= 9:
                fed_no = cells[0].text.strip()
                pname = cells[1].text.strip()
                club_text = cells[2].text.strip()
                hcp_text = cells[3].text.strip()
                hcp_status = cells[4].text.strip()
                am_pro = cells[5].text.strip()
                gender = cells[6].text.strip()
                age_group = cells[7].text.strip()

                # Parse club code from "Club Name (code)" format
                club = club_text
                club_code = ""
                if "(" in club_text and ")" in club_text:
                    parts = club_text.rsplit("(", 1)
                    club = parts[0].strip()
                    club_code = parts[1].rstrip(")")

                try:
                    hcp_value = float(hcp_text) if hcp_text and hcp_text != "-" else None
                except ValueError:
                    hcp_value = None

                players.append({
                    "fed_no": fed_no,
                    "name": pname,
                    "club": club,
                    "club_code": club_code,
                    "hcp_index": hcp_value,
                    "hcp_status": hcp_status,
                    "gender": gender,
                    "age_group": age_group,
                })
    finally:
        driver.quit()

    return players


# ---------------------------------------------------------------------------
# Step 2 – get course data from the playing HCP calculator page
# ---------------------------------------------------------------------------

def get_courses() -> list[dict]:
    """Fetch all course/tee data from the TGF CalcPlayHcp page.

    Tries the requests-based approach first, falls back to Selenium.
    """
    courses = _get_courses_requests()
    if courses:
        return courses
    try:
        return _get_courses_selenium()
    except Exception:
        return []


def _get_courses_requests() -> list[dict]:
    """Try to fetch courses via authenticated requests session."""
    session = _create_authenticated_session(
        "calchcp", "&fedno=&tcode=&param="
    )
    if session is None:
        return []

    try:
        resp = session.get(
            BASE_URL + "CalcPlayHcp.aspx?fedno=&tcode=&gender=&hcp=&param=",
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return _parse_courses_html(resp.text)
    except requests.RequestException:
        return []


def _get_courses_selenium() -> list[dict]:
    """Fallback: use Selenium to load the CalcPlayHcp page."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get("https://www.tgf.org.tr/tr/oyun-hcp-hesaplama")
        time.sleep(3)

        iframe = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        driver.switch_to.frame(iframe)

        # Wait for the courses dropdown
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "DpCourses"))
        )

        return _parse_courses_html(driver.page_source)
    finally:
        driver.quit()


def _parse_courses_html(html: str) -> list[dict]:
    """Parse course data from the CalcPlayHcp page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id="DpCourses")
    if not select:
        return []

    courses = []
    for option in select.find_all("option"):
        text = option.get_text(strip=True)
        value = option.get("value", "")

        if text == "Manuel" or len(value) < 27:
            continue

        # Packed value: PAR18(3) CR18(3) SL18(3) PAR9F(3) CR9F(3) SL9F(3) PAR9B(3) CR9B(3) SL9B(3)
        # Course Rating stored as int*10 (677 -> 67.7)
        try:
            par_18   = int(value[0:3])
            cr_18    = int(value[3:6]) / 10.0
            slope_18 = int(value[6:9])
            par_f9   = int(value[9:12])
            cr_f9    = int(value[12:15]) / 10.0
            slope_f9 = int(value[15:18])
            par_b9   = int(value[18:21])
            cr_b9    = int(value[21:24]) / 10.0
            slope_b9 = int(value[24:27])
        except (ValueError, IndexError):
            continue

        courses.append({
            "name": text,
            "par_18": par_18,   "cr_18": cr_18,   "slope_18": slope_18,
            "par_f9": par_f9,   "cr_f9": cr_f9,   "slope_f9": slope_f9,
            "par_b9": par_b9,   "cr_b9": cr_b9,   "slope_b9": slope_b9,
        })

    return courses


def find_courses_by_name(courses: list[dict], query: str) -> list[dict]:
    """Find courses whose name contains the query string (case-insensitive)."""
    q = query.lower()
    return [c for c in courses if q in c["name"].lower()]


# ---------------------------------------------------------------------------
# Step 3 – WHS Playing Handicap calculation
# ---------------------------------------------------------------------------

def calc_playing_handicap(
    hcp_index: float, slope: int, course_rating: float, par: int,
    allowance: int = 100,
) -> int | None:
    """Calculate the WHS Playing Handicap.

    Formula: Playing HCP = round(HCP_Index * (Slope / 113) + (CR - Par)) * allowance%
    """
    if slope == 0 or course_rating == 0:
        return None
    course_hcp = hcp_index * (slope / 113) + (course_rating - par)
    return round(course_hcp * allowance / 100)


# ---------------------------------------------------------------------------
# Helper – resolve a single player name (or fed number) to one player dict
# ---------------------------------------------------------------------------

def _search_by_fedno(fedno: str) -> list[dict]:
    """Search for a player by federation number."""
    try:
        session = _create_authenticated_session("handicaps", "&ccode=All")
        if session is None:
            raise RuntimeError("no session")

        api_url = BASE_URL + "FederatedsList_V2.aspx/HandicapsLST"
        payload = {
            "name": "", "fedno": fedno,
            "ClubCode": "All", "FedStat": "9", "Gender": "All",
            "Agelev": "All", "HcpStat": "All", "FHcp": "", "THcp": "",
            "ProAm": "All", "IniFlag": "0", "FAge": "", "TAge": "",
            "Permit": "", "MaxResults": "0", "MessMax": "",
            "jtStartIndex": 0, "jtPageSize": 10, "jtSorting": "name ASC",
        }
        resp = session.post(
            api_url, json=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": BASE_URL + "FederatedsList_V2.aspx",
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
    except Exception:
        return _search_by_fedno_selenium(fedno)


def _search_by_fedno_selenium(fedno: str) -> list[dict]:
    """Fallback: search by federation number via Selenium."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=options)
    players = []
    try:
        driver.get("https://www.tgf.org.tr/tr/handikap-listesi")
        time.sleep(3)
        iframe = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
        )
        driver.switch_to.frame(iframe)
        fedno_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "insrhfedno"))
        )
        fedno_input.clear()
        fedno_input.send_keys(fedno)
        driver.find_element(By.ID, "btnSearch").click()
        time.sleep(3)

        rows = driver.find_elements(
            By.CSS_SELECTOR, ".jtable tbody tr.jtable-data-row"
        )
        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) >= 9:
                club_text = cells[2].text.strip()
                club, club_code = club_text, ""
                if "(" in club_text and ")" in club_text:
                    parts = club_text.rsplit("(", 1)
                    club = parts[0].strip()
                    club_code = parts[1].rstrip(")")
                hcp_text = cells[3].text.strip()
                try:
                    hcp_val = float(hcp_text) if hcp_text and hcp_text != "-" else None
                except ValueError:
                    hcp_val = None
                players.append({
                    "fed_no": cells[0].text.strip(),
                    "name": cells[1].text.strip(),
                    "club": club, "club_code": club_code,
                    "hcp_index": hcp_val,
                    "hcp_status": cells[4].text.strip(),
                    "gender": cells[6].text.strip(),
                    "age_group": cells[7].text.strip(),
                })
    finally:
        driver.quit()
    return players


def resolve_player(name_or_id: str) -> dict | None:
    """Resolve a player name OR federation number to one confirmed player.

    - If the input is purely numeric, search by Federation Number directly.
    - Otherwise search by name, prefer exact matches, and ask the user to
      pick when there is ambiguity.
    """
    is_fedno = name_or_id.strip().isdigit()

    if is_fedno:
        # ---- Search by Federation Number ----
        print(f"Looking up Federation No: {name_or_id}")
        players = _search_by_fedno(name_or_id.strip())
    else:
        # ---- Search by name ----
        print(f"Searching for player: {name_or_id}")
        try:
            players = search_player(name_or_id)
        except Exception:
            print("  API method failed, trying browser fallback...")
            players = search_player_selenium(name_or_id)

    if not players:
        print(f"  No players found matching '{name_or_id}'.\n")
        return None

    # Keep only active handicaps
    active = [p for p in players
              if p["hcp_index"] is not None and p["hcp_status"] == "Aktif"]

    if not active:
        print(f"  Found {len(players)} player(s) but none have an active handicap:")
        for p in players:
            print(f"    - {p['name']} (Fed.No: {p['fed_no']}, "
                  f"HCP: {p['hcp_index']}, Status: {p['hcp_status']})")
        print()
        return None

    # --- Try exact name match first (case-insensitive) ---
    if not is_fedno:
        query_lower = name_or_id.strip().lower()
        exact = [p for p in active if p["name"].lower() == query_lower]
        if len(exact) == 1:
            # Unique exact match – use it directly
            active = exact
        elif len(exact) > 1:
            # Multiple people with the same full name – narrow to these
            active = exact

    if len(active) == 1:
        player = active[0]
    else:
        # List all matches with full details so the user can distinguish
        print(f"  Found {len(active)} matching players:")
        print()
        print(f"    {'#':<4} {'Name':<25} {'Fed.No':<8} {'Club':<22} "
              f"{'HCP':>5} {'Gender':<7} {'Age Group'}")
        print(f"    {'─'*4} {'─'*25} {'─'*8} {'─'*22} {'─'*5} {'─'*7} {'─'*15}")
        for i, p in enumerate(active, 1):
            print(f"    {i:<4} {p['name']:<25} {p['fed_no']:<8} "
                  f"{p['club']:<22} {p['hcp_index']:>5} "
                  f"{p['gender']:<7} {p['age_group']}")
        print()
        print("  Tip: You can also use Federation Number directly to avoid "
              "ambiguity, e.g.:")
        print(f"       python tgf_playing_handicap.py \"{active[0]['fed_no']}\" \"CourseName\"")
        print()
        while True:
            choice_str = input(
                f"  Enter row number (1-{len(active)}) or Federation Number: "
            ).strip()
            # Allow the user to type a federation number directly
            fedno_match = [p for p in active if str(p["fed_no"]) == choice_str]
            if fedno_match:
                player = fedno_match[0]
                break
            try:
                choice = int(choice_str)
                if 1 <= choice <= len(active):
                    player = active[choice - 1]
                    break
            except ValueError:
                pass
            print(f"  Please enter a number between 1 and {len(active)}, "
                  "or a valid Federation Number.")

    print(f"  ✓ {player['name']}  |  Fed.No: {player['fed_no']}  |  "
          f"Club: {player['club']} ({player['club_code']})  |  "
          f"Gender: {player['gender']}  |  HCP Index: {player['hcp_index']}")
    print()
    return player


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print('Usage: python tgf_playing_handicap.py "Player(s)" "Course Name"')
        print()
        print("Single player (by name):")
        print('  python tgf_playing_handicap.py "Ali Akar" "Kemer"')
        print()
        print("Single player (by Federation Number):")
        print('  python tgf_playing_handicap.py "2769" "Kemer"')
        print()
        print("Multiple players (comma-separated names and/or Fed numbers):")
        print('  python tgf_playing_handicap.py "Ali Akar, 6099, Mehmet Yılmaz" "Gloria"')
        sys.exit(1)

    player_names_raw = sys.argv[1]
    course_query = sys.argv[2]

    # Split comma-separated player names
    player_names = [n.strip() for n in player_names_raw.split(",") if n.strip()]

    # ---- Resolve each player ----
    print("=" * 60)
    print("PLAYER LOOKUP")
    print("=" * 60)

    confirmed_players: list[dict] = []
    for name in player_names:
        player = resolve_player(name)
        if player:
            confirmed_players.append(player)

    if not confirmed_players:
        print("No valid players found. Exiting.")
        sys.exit(1)

    # ---- Fetch course data ----
    print("=" * 60)
    print("COURSE LOOKUP")
    print("=" * 60)
    print(f"Searching for course: {course_query}")
    print()

    all_courses = get_courses()
    if not all_courses:
        print("ERROR: Could not retrieve course data from TGF.")
        sys.exit(1)

    matching = find_courses_by_name(all_courses, course_query)

    if not matching:
        print(f"No courses found matching '{course_query}'.")
        print()
        print("Available courses:")
        seen = set()
        for c in sorted(all_courses, key=lambda x: x["name"]):
            base = c["name"].rsplit(" - ", 1)[0]
            if base not in seen:
                seen.add(base)
                print(f"  - {base}")
        sys.exit(1)

    # Group by base course name
    base_names: dict[str, list[dict]] = {}
    for c in matching:
        base = c["name"].rsplit(" - ", 1)[0]
        base_names.setdefault(base, []).append(c)

    if len(base_names) > 1:
        print(f"Multiple courses match '{course_query}':")
        bases = sorted(base_names)
        for i, b in enumerate(bases, 1):
            tees = [c["name"].rsplit(" - ", 1)[-1] for c in base_names[b]]
            print(f"  {i}. {b} (Tees: {', '.join(tees)})")
        print()
        while True:
            try:
                choice = int(input("Select course number: "))
                if 1 <= choice <= len(bases):
                    selected_base = bases[choice - 1]
                    matching = base_names[selected_base]
                    break
            except ValueError:
                pass
            print(f"Please enter a number between 1 and {len(bases)}.")
    else:
        selected_base = next(iter(base_names))
        matching = base_names[selected_base]

    tees_sorted = sorted(matching, key=lambda c: c["slope_18"], reverse=True)

    # ---- Calculate and display ----
    print()
    print("=" * 60)
    print("PLAYING HANDICAPS")
    print("=" * 60)
    print(f"Course: {selected_base}")
    print()

    # Build a dynamic table with one column per player
    tee_names = []
    for course in tees_sorted:
        tee = course["name"].rsplit(" - ", 1)[-1] if " - " in course["name"] else course["name"]
        tee_names.append(tee)

    # Determine column widths
    player_col_width = max(12, max(len(p["name"]) for p in confirmed_players) + 2)
    tee_col_width = max(8, max(len(t) for t in tee_names) + 2)

    # --- Header row ---
    header = f"{'Tee':<{tee_col_width}} {'PAR':>4} {'Rating':>7} {'Slope':>6}"
    for p in confirmed_players:
        display = f"{p['name']} ({p['hcp_index']})"
        header += f"  {display:>{player_col_width}}"
    print(header)
    print("=" * len(header))

    # --- Data rows ---
    for course, tee in zip(tees_sorted, tee_names):
        row = (f"{tee:<{tee_col_width}} {course['par_18']:>4} "
               f"{course['cr_18']:>7.1f} {course['slope_18']:>6}")

        for p in confirmed_players:
            phcp = calc_playing_handicap(
                p["hcp_index"], course["slope_18"],
                course["cr_18"], course["par_18"],
            )
            val = str(phcp) if phcp is not None else "N/A"
            display_w = len(f"{p['name']} ({p['hcp_index']})")
            col_w = max(player_col_width, display_w)
            row += f"  {val:>{col_w}}"

        print(row)

    print()
    print("Note: Playing Handicap = round(HCP Index x (Slope / 113) + (Course Rating - PAR))")
    print("      Calculated using 100% handicap allowance (stroke play).")


if __name__ == "__main__":
    main()
