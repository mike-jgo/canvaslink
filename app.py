"""
app.py
------
Flask web dashboard for Canvas LMS.
Imports all API logic from canvas_summary.py — no rewrites.

Run:
    python app.py
    # open http://localhost:5001
"""

import os
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify, render_template, session, redirect, url_for, request

from canvas_summary import (
    make_session,
    get_courses,
    get_assignments,
    get_announcements,
    fmt_dt,
    strip_html,
    parse_dt,
    paginate,
    UPCOMING_DAYS,
    ANNOUNCEMENT_DAYS,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


# ── Credential helpers ─────────────────────────────────────────────────────────

def _get_credentials():
    """Return (canvas_url, token) from the Flask session, or (None, None)."""
    return session.get("canvas_url"), session.get("canvas_token")


@app.before_request
def require_login():
    if request.endpoint in ("login", "logout", "static"):
        return
    canvas_url, token = _get_credentials()
    if not canvas_url or not token:
        return redirect(url_for("login"))


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None, prefill_url="")

    canvas_url = request.form.get("canvas_url", "").rstrip("/")
    token = request.form.get("token", "").strip()

    if not canvas_url or not token:
        return render_template("login.html", error="Both fields are required.", prefill_url=canvas_url)

    # Verify credentials against Canvas
    try:
        sess = make_session(token=token)
        resp = sess.get(f"{canvas_url}/api/v1/users/self", timeout=10)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        if status == 401:
            error = "Invalid API token. Please double-check and try again."
        else:
            error = f"Canvas returned HTTP {status}. Check your Canvas URL."
        return render_template("login.html", error=error, prefill_url=canvas_url)
    except requests.exceptions.ConnectionError:
        return render_template("login.html", error="Could not connect to that Canvas URL. Check the address.", prefill_url=canvas_url)
    except Exception as exc:
        return render_template("login.html", error=str(exc), prefill_url=canvas_url)

    session["canvas_url"] = canvas_url
    session["canvas_token"] = token
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Local helpers ──────────────────────────────────────────────────────────────

def _hours_until(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 3600


def _urgency(hours: float | None) -> str:
    if hours is None:
        return "green"
    if hours < 0:
        return "red"    # overdue
    if hours < 72:
        return "yellow" # due within 3 days
    return "green"


def _get_overdue_assignments(sess, course_id: int, canvas_url: str) -> list:
    """Fetch overdue assignments for a course; returns [] on any error."""
    try:
        return paginate(
            sess,
            f"{canvas_url}/api/v1/courses/{course_id}/assignments",
            {"order_by": "due_at", "bucket": "overdue"},
        )
    except Exception:
        return []


def _build_assignment_dict(a: dict, course_id: int, course_code: str) -> dict:
    """Build the standard assignment dict for API response."""
    due_dt = parse_dt(a.get("due_at"))
    hours = _hours_until(due_dt)
    return {
        "id": a["id"],
        "name": a["name"],
        "course_id": course_id,
        "course_code": course_code,
        "due_at": a.get("due_at"),
        "due_fmt": fmt_dt(due_dt),
        "points_possible": a.get("points_possible"),
        "html_url": a.get("html_url", ""),
        "urgency": _urgency(hours),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        upcoming_days=UPCOMING_DAYS,
        announcement_days=ANNOUNCEMENT_DAYS,
    )


@app.route("/api/data")
def api_data():
    canvas_url, token = _get_credentials()

    # 1. Build session
    try:
        sess = make_session(token=token)
    except SystemExit as e:
        return jsonify({"error": str(e), "courses": [], "assignments": [], "announcements": [], "generated_at": None})

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=UPCOMING_DAYS)
    since = now - timedelta(days=ANNOUNCEMENT_DAYS)

    try:
        # 2. Courses
        courses = get_courses(sess, canvas_url=canvas_url)
        courses_map = {c["id"]: c.get("course_code") or c["name"] for c in courses}
        course_ids = [c["id"] for c in courses]

        # 3. Upcoming assignments
        all_assignments = []
        seen_ids: set[int] = set()
        for course in courses:
            code = courses_map.get(course["id"], f"Course {course['id']}")
            for a in get_assignments(sess, course["id"], cutoff, canvas_url=canvas_url):
                all_assignments.append(_build_assignment_dict(a, course["id"], code))
                seen_ids.add(a["id"])

        # 4. Overdue assignments (deduplicated)
        for course in courses:
            code = courses_map.get(course["id"], f"Course {course['id']}")
            for a in _get_overdue_assignments(sess, course["id"], canvas_url):
                if a["id"] not in seen_ids:
                    all_assignments.append(_build_assignment_dict(a, course["id"], code))
                    seen_ids.add(a["id"])

        # Sort ascending by due_at (overdue past dates appear first; None last)
        all_assignments.sort(key=lambda x: x["due_at"] or "9999")

        # 5. Announcements
        raw_ann = get_announcements(sess, course_ids, since, canvas_url=canvas_url)
        announcements = []
        for ann in raw_ann:
            context_code = ann.get("context_code", "")
            cid = None
            if context_code.startswith("course_"):
                try:
                    cid = int(context_code.replace("course_", ""))
                except ValueError:
                    pass
            posted_dt = parse_dt(ann.get("posted_at"))
            body = strip_html(ann.get("message", ""))
            preview = body[:280] + ("…" if len(body) > 280 else "")
            announcements.append({
                "id": ann.get("id"),
                "title": ann.get("title", "(no title)"),
                "course_id": cid,
                "course_code": courses_map.get(cid, context_code),
                "posted_at": ann.get("posted_at"),
                "posted_fmt": fmt_dt(posted_dt),
                "preview": preview,
                "html_url": ann.get("html_url", ""),
            })

        # 6. Course list for display
        courses_out = [
            {
                "id": c["id"],
                "course_code": c.get("course_code") or c["name"],
                "name": c["name"],
            }
            for c in courses
        ]

        return jsonify({
            "courses": courses_out,
            "assignments": all_assignments,
            "announcements": announcements,
            "generated_at": fmt_dt(now),
            "error": None,
        })

    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "error": str(exc),
            "courses": [],
            "assignments": [],
            "announcements": [],
            "generated_at": None,
        })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5001)
