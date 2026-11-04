import base64
import re
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import holidays

from .config import (SPREADSHEET_ID, SUMMARY_TAB, LOG_TAB, TIMEZONE, INDEX_TAB, HOLIDAYS_TAB,LK_HOLIDAY_CALENDAR)
from .google_auth import sheets_service, gmail_service, calendar_service
from .rules import snapshot, parse_date, set_holidays
from .sheet_layout import parse_project_tab

HEALTH_ORDER = {"Overdue": 0, "Needs attention": 1, "Ready to close": 2, "On track": 3}


# ---------- helpers (not tools; the model never sees these) ----------

def _now():
    return datetime.now(ZoneInfo(TIMEZONE))


def _quote(title):
    """Tab names with spaces must be wrapped in single quotes in a range."""
    return "'" + title.replace("'", "''") + "'"


def _file_id(link):
    """Gets the spreadsheet ID from a Google Sheets link (or accepts a bare ID)."""
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", link)
    if match:
        return match.group(1)
    link = link.strip()
    return link if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", link) else None



def _company_holidays():
    """Date -> name from the master's Holidays tab. Empty if the tab is missing."""
    try:
        rows = (sheets_service().spreadsheets().values()
                .get(spreadsheetId=SPREADSHEET_ID, range=f"{_quote(HOLIDAYS_TAB)}!A2:B")
                .execute().get("values", []))
    except Exception as e:
        print(f"Could not read the {HOLIDAYS_TAB} tab: {e}")
        return {}
    result = {}
    for row in rows:
        day = parse_date(row[0]) if row else None
        if day:
            name = row[1].strip() if len(row) > 1 else ""
            result[day] = name or "Company day off"
    return result


def _google_holidays(start, end):
    """Date -> name from Google's Sri Lanka holiday calendar. Raises on any error."""
    tz = ZoneInfo(TIMEZONE)
    day_after_end = end + timedelta(days=1)
    events = (
        calendar_service().events()
        .list(calendarId=LK_HOLIDAY_CALENDAR,
              timeMin=datetime(start.year, start.month, start.day, tzinfo=tz).isoformat(),
              timeMax=datetime(day_after_end.year, day_after_end.month,
                               day_after_end.day, tzinfo=tz).isoformat(),
              singleEvents=True, orderBy="startTime", maxResults=250)
        .execute().get("items", [])
    )
    result = {}
    for event in events:
        if "observance" in event.get("description", "").lower():
            continue
        day = parse_date(event.get("start", {}).get("date", ""))
        if day:
            result[day] = event.get("summary", "Public holiday")
    return result


def _official_holidays(start, end):
    """Official holidays from Google Calendar, or from the holidays library if that fails.
    Returns (date -> name, source name)."""
    try:
        return _google_holidays(start, end), "Google Calendar"
    except Exception as e:
        print(f"Google holiday calendar unavailable ({e}). Using the holidays library.")
        library = holidays.country_holidays("LK", years=list(range(start.year, end.year + 1)),
                                            language="en_US")
        return {d: n for d, n in library.items() if start <= d <= end}, "holidays library"


def _load_holidays():
    """Loads official holidays plus the Holidays tab dates into the rules.
    Returns how many official holidays are known for this year."""
    today = _now().date()
    official, _ = _official_holidays(date(today.year - 1, 1, 1), date(today.year + 1, 12, 31))
    return set_holidays(set(_company_holidays()), today, official_dates=set(official))
    


def _load_projects():
    """Reads the index tab, then each project's own spreadsheet."""
    _load_holidays()
    values = sheets_service().spreadsheets().values()
    index = values.get(spreadsheetId=SPREADSHEET_ID,
                       range=f"{_quote(INDEX_TAB)}!A2:C").execute().get("values", [])

    projects = []
    for row in index:
        project_id, name, link = (cell.strip() for cell in (row + ["", "", ""])[:3])
        if not project_id:
            continue
        project_id = project_id.upper()
        file_id = _file_id(link)
        try:
            if file_id is None:
                raise ValueError("The Sheet link is missing or is not a Google Sheets link.")
            rows = values.get(spreadsheetId=file_id, range="A1:E").execute().get("values", [])
            project = parse_project_tab(name, None, rows)
            file_says = project["info"].get("Project ID", "").upper()
            if file_says != project_id:
                project["id_mismatch"] = file_says
            project["info"]["Project ID"] = project_id
        except Exception as e:
            project = {"tab": name, "gid": None, "entries": [], "bad_rows": [],
                       "has_table": False, "access_error": str(e)[:200],
                       "info": {"Project ID": project_id, "Project name": name}}
        project["file_id"] = file_id
        projects.append(project)
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


def _project_link(project):
    if not project.get("file_id"):
        return ""
    return f"https://docs.google.com/spreadsheets/d/{project['file_id']}/edit"

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
            "tab_link": _project_link(project)}


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


def write_summary(assessments: list[str]) -> dict:
    """Rewrites the Summary tab for the GM. All figures are recomputed in code;
    you only supply a one-sentence assessment for each project.

    Args:
        assessments: One item per project, each written as
            'P01: one short sentence'.
    """
    parsed = {}
    unreadable = []
    for item in assessments:
        project_id, separator, text = str(item).partition(":")
        if separator and project_id.strip() and text.strip():
            parsed[project_id.strip().upper()] = text.strip()
        else:
            unreadable.append(str(item))
    assessments = parsed

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
            "unknown_ids_ignored": sorted(set(assessments) - known),
            "unreadable_items": unreadable}


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
                        f"The {pid} - {info.get('Project name', '')} sheet has had no update since "
            f"{last.isoformat() if last else 'the project started'}.",
            "Please add a row for your recent work: Date, Developer, Work done, "
            "Completion % (whole project), Blockers.",
            "",
            f"Your project sheet: {_project_link(project)}",
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


def list_holidays(project_id: str = "", start_date: str = "", end_date: str = "") -> dict:
    """Lists Sri Lankan public holidays and company days off in a period, and
    which projects each one falls within.

    Args:
        project_id: A project ID (e.g. 'P05') to use that project's start date
            and target deadline, or 'ALL' to cover every project. Leave empty
            to use start_date and end_date instead.
        start_date: yyyy-mm-dd. Used only when project_id is empty.
        end_date: yyyy-mm-dd. Used only when project_id is empty.
    """
    try:
        projects = _load_projects()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    spans = {}
    for p in projects:
        start = parse_date(p["info"].get("Start date", ""))
        deadline = parse_date(p["info"].get("Target deadline", ""))
        if start and deadline:
            spans[p["info"]["Project ID"]] = (start, deadline)

    wanted = project_id.strip().upper()
    if wanted.isdigit():
        wanted = f"P{int(wanted):02d}"
    if wanted == "ALL":
        if not spans:
            return {"status": "error", "message": "No project has both a start date and a deadline."}
        start = min(s for s, _ in spans.values())
        end = max(d for _, d in spans.values())
    elif wanted:
        if wanted not in spans:
            return {"status": "error",
                    "message": f"No project '{wanted}' with both a start date and a target deadline."}
        start, end = spans[wanted]
    else:
        start = parse_date(start_date) or _now().date()
        end = parse_date(end_date) or start + timedelta(days=60)

    if end < start:
        return {"status": "error", "message": "The end date is before the start date."}
    if (end - start).days > 730:
        return {"status": "error", "message": "Please ask for a period of two years or less."}

    years = list(range(start.year, end.year + 1))
    official, official_source = _official_holidays(start, end)
    company = _company_holidays()

    found = [(d, n, "Official") for d, n in official.items() if start <= d <= end]
    found += [(d, n, "Company") for d, n in company.items() if start <= d <= end]
    found.sort()

    result = []
    for day, name, source in found:
        result.append({
            "date": day.isoformat(),
            "day": day.strftime("%A"),
            "name": name,
            "source": source,
            "on_a_weekday": day.weekday() < 5,
            "projects": sorted(pid for pid, (s, d) in spans.items() if s <= day <= d),
        })

    missing_years = [y for y in years if not any(d.year == y for d in official)]
    reply = {"status": "success", "from": start.isoformat(), "to": end.isoformat(),
             "official_source": official_source,
             "count": len(result), "weekday_count": sum(h["on_a_weekday"] for h in result),
             "holidays": result}
    if missing_years:
        reply["warning"] = (f"No official holiday list is available yet for {missing_years}. "
                            "Only company days off are shown for those years.")
    return reply