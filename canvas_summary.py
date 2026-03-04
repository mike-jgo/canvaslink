"""
canvas_summary.py
-----------------
Fetches upcoming assignments and recent announcements from Canvas LMS
and prints a clean, readable summary.

Usage:
    python canvas_summary.py

Configuration:
    Set CANVAS_URL and CANVAS_TOKEN in a .env file (or export as env vars).
    See .env.example for reference.
"""

import os
import sys
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

CANVAS_URL   = os.getenv("CANVAS_URL", "https://myschool.instructure.com")
CANVAS_TOKEN = os.getenv("CANVAS_TOKEN", "")

# How many days ahead to consider an assignment "upcoming"
UPCOMING_DAYS = int(os.getenv("UPCOMING_DAYS", "14"))

# How many days back to fetch announcements
ANNOUNCEMENT_DAYS = int(os.getenv("ANNOUNCEMENT_DAYS", "7"))

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_session(token: str | None = None) -> requests.Session:
    resolved_token = token or CANVAS_TOKEN
    if not resolved_token:
        sys.exit(
            "ERROR: CANVAS_TOKEN is not set.\n"
            "Add it to a .env file or export it as an environment variable."
        )
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {resolved_token}"})
    return session


def paginate(session: requests.Session, url: str, params: dict | None = None) -> list:
    """Follow Canvas's Link-header pagination and return all results."""
    results = []
    params = params or {}
    params.setdefault("per_page", 100)
    while url:
        resp = session.get(url, params=params)
        resp.raise_for_status()
        results.extend(resp.json())
        # Canvas puts the next page URL in the Link header
        url = _next_link(resp.headers.get("Link", ""))
        params = {}          # params are already baked into the next URL
    return results


def _next_link(link_header: str) -> str | None:
    """Parse the 'next' rel from a Canvas Link header."""
    for part in link_header.split(","):
        url_part, *rel_parts = part.strip().split(";")
        if any('rel="next"' in r for r in rel_parts):
            return url_part.strip().strip("<>")
    return None


def parse_dt(iso: str | None) -> datetime | None:
    """Parse an ISO-8601 string (with or without Z) into an aware datetime."""
    if not iso:
        return None
    iso = iso.replace("Z", "+00:00")
    return datetime.fromisoformat(iso)


def fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "No due date"
    local = dt.astimezone()          # convert to local timezone
    return local.strftime("%a %b %d, %Y  %I:%M %p")


def strip_html(text: str) -> str:
    """Very light HTML tag stripper — avoids a BeautifulSoup dependency."""
    import re
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&nbsp;",  " ", text)
    text = re.sub(r"&amp;",   "&", text)
    text = re.sub(r"&lt;",    "<", text)
    text = re.sub(r"&gt;",    ">", text)
    text = re.sub(r"&quot;",  '"', text)
    text = re.sub(r"\s{2,}",  " ", text)
    return text.strip()

# ── Canvas API calls ──────────────────────────────────────────────────────────

def get_courses(session: requests.Session, canvas_url: str | None = None) -> list[dict]:
    """Return active enrolled courses for the current user."""
    base = canvas_url or CANVAS_URL
    courses = paginate(
        session,
        f"{base}/api/v1/courses",
        {"enrollment_state": "active", "state[]": "available"},
    )
    # Filter out any courses without a name (shells / concluded courses)
    return [c for c in courses if c.get("name")]


def get_assignments(session: requests.Session, course_id: int, cutoff: datetime, canvas_url: str | None = None) -> list[dict]:
    """Return assignments due between now and cutoff for a single course."""
    base = canvas_url or CANVAS_URL
    now = datetime.now(timezone.utc)
    assignments = paginate(
        session,
        f"{base}/api/v1/courses/{course_id}/assignments",
        {"order_by": "due_at", "bucket": "upcoming"},
    )
    upcoming = []
    for a in assignments:
        due = parse_dt(a.get("due_at"))
        if due is None:
            continue
        if now <= due <= cutoff:
            upcoming.append(a)
    return sorted(upcoming, key=lambda a: a["due_at"])


def get_announcements(session: requests.Session, course_ids: list[int], since: datetime, canvas_url: str | None = None) -> list[dict]:
    """Return announcements posted since `since` across the given courses."""
    base = canvas_url or CANVAS_URL
    all_ann = paginate(
        session,
        f"{base}/api/v1/announcements",
        {
            "context_codes[]": [f"course_{cid}" for cid in course_ids],
            "start_date": since.date().isoformat(),
            "end_date": datetime.now(timezone.utc).date().isoformat(),
        },
    )
    return sorted(all_ann, key=lambda a: a.get("posted_at") or "", reverse=True)

# ── Display ───────────────────────────────────────────────────────────────────

SEP  = "─" * 72
SEP2 = "═" * 72

def header(title: str) -> None:
    print(f"\n{SEP2}")
    print(f"  {title}")
    print(SEP2)


def print_assignments(courses_map: dict[int, str], all_assignments: list[tuple[dict, dict]]) -> None:
    header(f"UPCOMING ASSIGNMENTS  (next {UPCOMING_DAYS} days)")
    if not all_assignments:
        print("  No upcoming assignments — enjoy the break!")
        return

    # Group by course
    by_course: dict[int, list] = {}
    for course, assignment in all_assignments:
        by_course.setdefault(course["id"], []).append(assignment)

    for course_id, assignments in by_course.items():
        course_name = courses_map.get(course_id, f"Course {course_id}")
        print(f"\n  {course_name}")
        print(f"  {SEP[:len(course_name) + 2]}")
        for a in assignments:
            due   = parse_dt(a.get("due_at"))
            score = a.get("points_possible")
            pts   = f"  [{score} pts]" if score is not None else ""
            print(f"    • {a['name']}")
            print(f"        Due: {fmt_dt(due)}{pts}")


def print_announcements(courses_map: dict[int, str], announcements: list[dict]) -> None:
    header(f"RECENT ANNOUNCEMENTS  (last {ANNOUNCEMENT_DAYS} days)")
    if not announcements:
        print("  No recent announcements.")
        return

    for ann in announcements:
        course_code = ann.get("context_code", "")
        course_id   = int(course_code.replace("course_", "")) if course_code.startswith("course_") else None
        course_name = courses_map.get(course_id, course_code)
        posted      = parse_dt(ann.get("posted_at"))
        body        = strip_html(ann.get("message", ""))
        # Truncate long bodies
        preview = body[:280] + ("…" if len(body) > 280 else "")

        print(f"\n  [{course_name}]  {ann.get('title', '(no title)')}")
        print(f"  Posted: {fmt_dt(posted)}")
        if preview:
            print(f"  {preview}")
        print(f"  {SEP}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    session  = make_session()
    now      = datetime.now(timezone.utc)
    cutoff   = now + timedelta(days=UPCOMING_DAYS)
    since    = now - timedelta(days=ANNOUNCEMENT_DAYS)

    print(f"\nConnecting to {CANVAS_URL} …")

    # 1. Courses
    print("Fetching active courses …")
    courses     = get_courses(session)
    courses_map = {c["id"]: c.get("course_code") or c["name"] for c in courses}

    if not courses:
        print("No active courses found.")
        return

    print(f"Found {len(courses)} active course(s).")

    # 2. Assignments (in parallel — one request per course)
    print("Fetching upcoming assignments …")
    all_pairs: list[tuple[dict, dict]] = []
    for course in courses:
        for assignment in get_assignments(session, course["id"], cutoff):
            all_pairs.append((course, assignment))

    # 3. Announcements
    print("Fetching recent announcements …")
    course_ids    = [c["id"] for c in courses]
    announcements = get_announcements(session, course_ids, since)

    # 4. Print summary
    print_assignments(courses_map, all_pairs)
    print_announcements(courses_map, announcements)

    print(f"\n{SEP2}")
    print(f"  Summary generated on {fmt_dt(now)}")
    print(f"{SEP2}\n")


if __name__ == "__main__":
    main()
