import base64
import json
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from .config import SPREADSHEET_ID, SUMMARY_TAB, LOG_TAB, TIMEZONE, PROJECT_TAB_PATTERN
from .google_auth import sheets_service, gmail_service
from .rules import snapshot, parse_date
from .sheet_layout import parse_project_tab

HEALTH_ORDER = {"Overdue": 0, "Needs attention": 1, "Ready to close": 2, "On track": 3}


# ---------- helpers (not tools; the model never sees these) ----------

def _now():
    return datetime.now(ZoneInfo(TIMEZONE))


def _quote(title):
    """Tab names with spaces must be wrapped in single quotes in a range."""
    return "'" + title.replace("'", "''") + "'"


def _load_projects():
    """Reads every project tab (named like 'P01 ...') in one request."""
    sheets = sheets_service().spreadsheets()
    meta = sheets.get(spreadsheetId=SPREADSHEET_ID,
                      fields="sheets.properties(title,sheetId)").execute()
    tabs = [s["properties"] for s in meta["sheets"]
            if re.match(PROJECT_TAB_PATTERN, s["properties"]["title"])]
    if not tabs:
        return []

    result = sheets.values().batchGet(
        spreadsheetId=SPREADSHEET_ID,
        ranges=[f"{_quote(t['title'])}!A1:E" for t in tabs],
    ).execute()

    projects = []
    for tab, value_range in zip(tabs, result.get("valueRanges", [])):
        projects.append(parse_project_tab(tab["title"], tab["sheetId"],
                                          value_range.get("values", [])))
    return projects


def _find_project(projects, project_id):
    wanted = project_id.strip().upper()
    return next((p for p in projects
                 if p["info"].get("Project ID", "").upper() == wanted), None)


def _entry_out(entry):
    """Makes a progress row JSON-friendly for the model."""
    return {"date": entry["date"].isoformat(), "developer": entry["developer"],
            "work": entry["work"], "completion_pct": round(entry["pct"]),
            "blocker": entry["blocker"]}


def _tab_link(project):
    return f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={project['gid']}"


def _read_log():
    return (sheets_service().spreadsheets().values()
            .get(spreadsheetId=SPREADSHEET_ID, range=f"{LOG_TAB}!A2:E")
            .execute().get("values", []))


def _write_log_row(project_id, source, details, link=""):
    row = [_now().strftime("%Y-%m-%d %H:%M"), project_id, source, details, link]
    (
        sheets_service().spreadsheets().values()
        .append(spreadsheetId=SPREADSHEET_ID, range=f"{LOG_TAB}!A1",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                body={"values": [row]})
        .execute()
    )


# ---------- tools (the model can call these) ----------

def read_projects() -> dict:
    """Overview of every project: computed figures (completion %, expected %,
    days left, last update, health, flags) plus the 3 most recent progress rows.

    Use this for general questions about the projects and before writing
    the summary. All figures are computed in code; report them as given.
    """
    try:
        projects = _load_projects()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    today = _now().date()
    overview = []
    for p in projects:
        item = snapshot(p, today)
        item["recent_updates"] = [_entry_out(e) for e in p["entries"][-3:]]
        overview.append(item)
    return {"status": "success", "today": today.isoformat(),
            "count": len(overview), "projects": overview}


def check_delays() -> dict:
    """Lists only the projects that need attention, with the reason for each:
    overdue, stale (no update for several working days), blocked, behind
    schedule, missing deadline, or data-entry problems.

    Use this for questions like "what is late?" or "what needs attention?".
    Report the flags exactly as returned; never add, remove or reinterpret them.
    """
    try:
        projects = _load_projects()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    today = _now().date()
    flagged = []
    for p in projects:
        s = snapshot(p, today)
        if s["flags"]:
            flagged.append({k: s[k] for k in
                            ("project_id", "project", "responsible", "health", "flags")})
    return {"status": "success", "today": today.isoformat(),
            "flagged_count": len(flagged), "flagged": flagged}


def read_project_log(project_id: str, last_n: int = 10) -> dict:
    """Shows one project's details and its most recent progress rows.

    Args:
        project_id: e.g. 'P03'.
        last_n: How many recent rows to return (1 to 50). Default 10.
    """
    try:
        project = _find_project(_load_projects(), project_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    if project is None:
        return {"status": "error", "message": f"No project tab with ID '{project_id}'."}

    last_n = max(1, min(50, last_n))
    return {"status": "success", "details": project["info"],
            "total_rows": len(project["entries"]),
            "recent_updates": [_entry_out(e) for e in project["entries"][-last_n:]],
            "tab_link": _tab_link(project)}


def developer_activity(developer_name: str, days: int = 7) -> dict:
    """Shows what one developer logged across ALL projects in the last few days.

    Args:
        developer_name: Full or partial name, e.g. 'Kasun'.
        days: How many calendar days back to look (1 to 60). Default 7.
    """
    try:
        projects = _load_projects()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    days = max(1, min(60, days))
    since = _now().date() - timedelta(days=days)
    needle = developer_name.strip().lower()
    activity = []
    for p in projects:
        for e in p["entries"]:
            if needle in e["developer"].lower() and e["date"] >= since:
                row = _entry_out(e)
                row["project_id"] = p["info"].get("Project ID", "")
                row["project"] = p["info"].get("Project name", "")
                activity.append(row)
    activity.sort(key=lambda r: r["date"])
    return {"status": "success", "since": since.isoformat(),
            "count": len(activity), "activity": activity}


def write_summary(assessments_json: str) -> dict:
    """Rewrites the Summary tab for the GM. All figures are recomputed in code;
    you only supply the one-sentence assessment for each project.

    Args:
        assessments_json: A JSON object mapping project ID to one short
            sentence, e.g. '{"P01": "On track; approval flow done.", "P02": "..."}'.
    """
    try:
        assessments = json.loads(assessments_json) if assessments_json.strip() else {}
        if not isinstance(assessments, dict):
            raise ValueError
    except ValueError:
        return {"status": "error",
                "message": 'assessments_json must be a JSON object like {"P01": "one sentence"}.'}
    assessments = {str(k).strip().upper(): str(v) for k, v in assessments.items()}

    try:
        projects = _load_projects()
        today = _now().date()
        snaps = [snapshot(p, today) for p in projects]
        snaps.sort(key=lambda s: (HEALTH_ORDER.get(s["health"], 9), s["project_id"]))

        rows = [
            [f"Project summary - written by PM Agent - last refreshed {_now().strftime('%Y-%m-%d %H:%M')}"],
            [],
            ["Project ID", "Project", "Responsible", "Target deadline", "Days left",
             "Completion %", "Expected %", "Last update", "Health", "Flags",
             "Latest work", "AI assessment"],
        ]
        for s in snaps:
            rows.append([
                s["project_id"], s["project"], s["responsible"], s["target_deadline"],
                "" if s["days_left"] is None else s["days_left"],
                s["completion_pct"],
                "" if s["expected_pct"] is None else s["expected_pct"],
                s["last_update"], s["health"],
                "; ".join(f["detail"] for f in s["flags"]),
                s["latest_work"],
                assessments.get(s["project_id"].upper(), "")[:300],
            ])

        values = sheets_service().spreadsheets().values()
        values.clear(spreadsheetId=SPREADSHEET_ID, range=f"{SUMMARY_TAB}!A1:Z1000",
                     body={}).execute()
        values.update(spreadsheetId=SPREADSHEET_ID, range=f"{SUMMARY_TAB}!A1",
                      valueInputOption="RAW", body={"values": rows}).execute()
        _write_log_row("ALL", "Agent: summary refreshed", f"{len(snaps)} projects written.")
    except Exception as e:
        return {"status": "error", "message": str(e)}

    known = {s["project_id"].upper() for s in snaps}
    return {"status": "success", "projects_written": len(snaps),
            "missing_assessments": sorted(known - set(assessments)),
            "unknown_ids_ignored": sorted(set(assessments) - known)}


def append_log(project_id: str, note: str) -> dict:
    """Records a note from the GM about a project in the Log tab.

    Args:
        project_id: e.g. 'P04'.
        note: One or two sentences, e.g. 'Client delayed sign-off by a week.'
    """
    try:
        if _find_project(_load_projects(), project_id) is None:
            return {"status": "error", "message": f"No project tab with ID '{project_id}'."}
        _write_log_row(project_id.strip().upper(), "GM chat", note)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {"status": "success", "logged_for": project_id.strip().upper()}


def email_developer(project_id: str, note: str = "", force: bool = False) -> dict:
    """Emails the project's responsible developer asking them to add a progress
    row to their project tab. The recipient comes from the tab; you cannot
    choose it. Only one reminder is sent until the developer adds a new row.

    Args:
        project_id: e.g. 'P02'.
        note: Optional. One short sentence to add to the email.
        force: Only True if the GM explicitly asks to send another reminder.
    """
    try:
        project = _find_project(_load_projects(), project_id)
        if project is None:
            return {"status": "error", "message": f"No project tab with ID '{project_id}'."}

        info = project["info"]
        pid = info.get("Project ID", project_id).upper()
        to = info.get("Responsible email", "").strip()
        if not to:
            return {"status": "error", "message": f"{pid} has no 'Responsible email'."}

        last = project["entries"][-1]["date"] if project["entries"] else None
        if not force:
            for row in _read_log():
                if len(row) >= 3 and row[1] == pid and row[2] == "Agent: reminder":
                    sent_on = parse_date(row[0][:10])
                    if sent_on and (last is None or sent_on >= last):
                        return {"status": "not_sent",
                                "reason": f"A reminder was already sent on {sent_on} and no new "
                                          f"row has been added since. Tell the GM instead."}

        name = info.get("Responsible developer", "")
        lines = [
            f"Hi {name.split()[0] if name else ''},",
            "",
            f"The {pid} - {info.get('Project name', '')} tab has had no update since "
            f"{last.isoformat() if last else 'the project started'}.",
            "Please add a row for your recent work: Date, Developer, Work done, "
            "Completion % (whole project), Blockers.",
            "",
            f"Your project tab: {_tab_link(project)}",
        ]
        if note:
            lines += ["", f"Note: {note}"]
        lines += ["", "Thanks,", "PM Agent (on behalf of the IT project manager)"]

        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = f"[{pid}] Progress update reminder"
        msg.set_content("\n".join(lines))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        sent = gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()
        _write_log_row(pid, "Agent: reminder", f"Reminder sent to {to}.",
                       f"https://mail.google.com/mail/u/0/#all/{sent['threadId']}")
    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {"status": "success", "sent_to": to}