import base64
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from .config import SPREADSHEET_ID, REGISTER_TAB, LOG_TAB, TIMEZONE, VALID_STATUSES
from .google_auth import sheets_service, gmail_service
from .rules import check_project


# ---------- helpers (not tools; the model never sees these) ----------

def _now():
    return datetime.now(ZoneInfo(TIMEZONE))


def _load_projects():
    """Reads the register and returns a list of dicts. Raises on API errors."""
    result = (
        sheets_service()
        .spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=f"{REGISTER_TAB}!A1:K")
        .execute()
    )
    rows = result.get("values", [])
    if len(rows) < 2:
        return []
    headers = rows[0]
    projects = []
    for row in rows[1:]:
        padded = row + [""] * (len(headers) - len(row))
        projects.append(dict(zip(headers, padded)))
    return projects



REGISTER_COLUMNS_AGENT_MAY_EDIT = {"Status", "Last update"}


def _find_project(projects, project_id):
    return next((p for p in projects if p.get("Project ID") == project_id), None)


def _write_log_row(project_id, source, summary, status="", new_due_date="", link=""):
    row = [_now().strftime("%Y-%m-%d %H:%M"), project_id, source,
           summary, status, new_due_date, link]
    (
        sheets_service()
        .spreadsheets()
        .values()
        .append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{LOG_TAB}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        )
        .execute()
    )


def _update_register(project_id, changes):
    """Updates allowed cells in one project's register row. Returns True if found."""
    for column in changes:
        if column not in REGISTER_COLUMNS_AGENT_MAY_EDIT:
            raise ValueError(f"The agent is not allowed to edit '{column}'.")

    values = sheets_service().spreadsheets().values()
    rows = values.get(spreadsheetId=SPREADSHEET_ID,
                      range=f"{REGISTER_TAB}!A1:K").execute().get("values", [])
    headers = rows[0]
    id_col = headers.index("Project ID")

    for sheet_row, row in enumerate(rows[1:], start=2):
        if len(row) > id_col and row[id_col] == project_id:
            data = []
            for column, value in changes.items():
                letter = chr(ord("A") + headers.index(column))
                data.append({"range": f"{REGISTER_TAB}!{letter}{sheet_row}",
                             "values": [[value]]})
            values.batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ).execute()
            return True
    return False

# ---------- tools (the model can call these) ----------

def read_projects() -> dict:
    """Reads every project from the project register.

    Use this whenever you need to know which projects exist, who owns them,
    their due dates, status, percent complete, or last update date.

    Returns:
        dict: 'status' is 'success' or 'error'. On success, 'projects' is a
        list of projects, each a dict keyed by the register's column names.
    """
    try:
        projects = _load_projects()
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {"status": "success", "count": len(projects), "projects": projects}


def check_delays() -> dict:
    """Checks every project against the fixed delay rules: overdue, stale
    (no update for several working days), at risk/blocked, and missing dates.

    Use this for any question like "what is late?", "what needs attention?",
    or "is anything overdue?". The rules are computed in code. Report the
    flags exactly as returned; never add, remove or reinterpret them.

    Returns:
        dict: 'today', 'flagged_count', and 'flagged' (a list of projects,
        each with its 'flags').
    """
    try:
        projects = _load_projects()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    today = _now().date()
    flagged = []
    for p in projects:
        flags = check_project(p, today)
        if flags:
            flagged.append({
                "project_id": p.get("Project ID"),
                "name": p.get("Name"),
                "owner": p.get("Owner"),
                "due": p.get("Due"),
                "status": p.get("Status"),
                "flags": flags,
            })

    return {"status": "success", "today": today.isoformat(),
            "flagged_count": len(flagged), "flagged": flagged}


def append_log(project_id: str, source: str, summary: str, status: str = "",
               new_due_date: str = "", raw_message_link: str = "") -> dict:
    """Records a project update: adds an entry to the append-only log and
    sets the register's 'Last update' to today (and 'Status', if given).

    Args:
        project_id: An existing Project ID from the register, e.g. 'P001'.
        source: Where the update came from, e.g. 'GM chat' or 'Developer email'.
        summary: One or two sentences describing the update.
        status: Optional. One of: Not started, On track, At risk, Blocked, Done.
        new_due_date: Optional. A proposed new due date as yyyy-mm-dd. This is
            only logged as a proposal for the GM; the register's due date is
            never changed.
        raw_message_link: Optional. Link to the original email or message.

    Returns:
        dict: 'status' is 'success' or 'error'.
    """
    if status and status not in VALID_STATUSES:
        return {"status": "error",
                "message": f"Status must be one of {VALID_STATUSES}."}
    try:
        if _find_project(_load_projects(), project_id) is None:
            return {"status": "error",
                    "message": f"No project with ID '{project_id}' in the register."}

        _write_log_row(project_id, source, summary, status, new_due_date, raw_message_link)

        changes = {"Last update": _now().date().isoformat()}
        if status:
            changes["Status"] = status
        _update_register(project_id, changes)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    result = {"status": "success", "logged_for": project_id,
              "register_updated": list(changes)}
    if new_due_date:
        result["note"] = "New due date logged as a proposal only. The GM must change the register."
    return result


def email_developer(project_id: str, note: str = "") -> dict:
    """Emails the developers of one project asking for a status update
    (status, blockers, new date). Recipients come from the register's
    'Developer emails' column; you cannot choose or add recipients.

    Args:
        project_id: An existing Project ID from the register, e.g. 'P001'.
        note: Optional. One short sentence to add, e.g. 'The due date is tomorrow.'

    Returns:
        dict: 'status', the recipients, and the Gmail 'thread_id'.
    """
    try:
        project = _find_project(_load_projects(), project_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    if project is None:
        return {"status": "error",
                "message": f"No project with ID '{project_id}' in the register."}

    recipients = [e.strip() for e in project.get("Developer emails", "").split(",") if e.strip()]
    if not recipients:
        return {"status": "error",
                "message": f"Project {project_id} has no developer emails in the register."}

    name = project.get("Name", "")
    lines = [
        "Hi,",
        "",
        f"Please reply to this email with a short update on {project_id} - {name} "
        f"(due {project.get('Due') or 'not set'}):",
        "",
        "1. Status: Not started / On track / At risk / Blocked / Done",
        "2. Blockers: anything stopping you, or 'none'",
        "3. New date: only if the due date will change",
    ]
    if note:
        lines += ["", f"Note: {note}"]
    lines += ["", "A short plain-English reply is fine.", "",
              "Thanks,", "PM Agent (on behalf of the IT project manager)"]

    msg = EmailMessage()
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"[{project_id}] Status update request: {name}"
    msg.set_content("\n".join(lines))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        sent = gmail_service().users().messages().send(
            userId="me", body={"raw": raw}).execute()
        thread_link = f"https://mail.google.com/mail/u/0/#all/{sent['threadId']}"
        _write_log_row(project_id, "Agent: update request",
                       f"Asked {', '.join(recipients)} for a status update.",
                       link=thread_link)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {"status": "success", "sent_to": recipients, "thread_id": sent["threadId"]}