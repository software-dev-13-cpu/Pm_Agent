import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import (SPREADSHEET_ID, TIMEZONE, TEAM_TAB, MEETINGS_TAB,
                     WORK_START_HOUR, WORK_END_HOUR, MEETING_SEARCH_DAYS)
from .google_auth import sheets_service, calendar_service
from .rules import is_working_day, parse_date
from .sheet_layout import leave_days_for
from .tools import _load_projects, _find_project, _now, _quote, _write_log_row

TZ = ZoneInfo(TIMEZONE)
COLUMNS = ["Meeting ID", "Created", "Project ID", "Purpose", "Start", "End",
           "Attendees", "Status", "Decided", "Event link", "Meet link", "Notes"]


# ---------- helpers ----------

def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)


def _team_directory():
    """Lower-case name -> email, from the master's Team tab."""
    rows = (sheets_service().spreadsheets().values()
            .get(spreadsheetId=SPREADSHEET_ID, range=f"{_quote(TEAM_TAB)}!A2:B")
            .execute().get("values", []))
    return {r[0].strip().lower(): r[1].strip().lower()
            for r in rows if len(r) > 1 and r[0].strip() and "@" in r[1]}


def _attendees_for(project, directory):
    """The project's team (responsible developer first) with emails, plus names with no email."""
    info = project["info"]
    names = [n.strip() for n in info.get("Team", "").split(",") if n.strip()]
    responsible = info.get("Responsible developer", "").strip()
    if responsible and responsible.lower() not in [n.lower() for n in names]:
        names.insert(0, responsible)
    attendees, missing = [], []
    for name in names:
        email = directory.get(name.lower())
        if not email and name.lower() == responsible.lower():
            email = info.get("Responsible email", "").strip().lower() or None
        if email:
            attendees.append({"name": name, "email": email})
        else:
            missing.append(name)
    return attendees, missing


def _busy_times(emails, start, end):
    """Email -> list of (busy_start, busy_end), plus emails whose calendar could not be read."""
    result = calendar_service().freebusy().query(body={
        "timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": TIMEZONE,
        "items": [{"id": email} for email in emails],
    }).execute()
    busy, unreadable = {}, []
    for email in emails:
        calendar = result.get("calendars", {}).get(email, {})
        if calendar.get("errors"):
            unreadable.append(email)
        busy[email] = [(_parse_time(b["start"]), _parse_time(b["end"]))
                       for b in calendar.get("busy", [])]
    return busy, unreadable


def _find_slots(busy, leave_days, duration, first_day, now, count=3):
    """First free slot on each of the next working days, up to `count` slots."""
    slots = []
    for offset in range(MEETING_SEARCH_DAYS):
        day = first_day + timedelta(days=offset)
        if not is_working_day(day) or day in leave_days:
            continue
        start = datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=TZ)
        last_start = datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=TZ) - duration
        while start <= last_start:
            end = start + duration
            clash = any(b_start < end and start < b_end
                        for times in busy.values() for b_start, b_end in times)
            if start > now and not clash:
                slots.append((start, end))
                break
            start += timedelta(minutes=30)
        if len(slots) == count:
            break
    return slots


def _read_meetings():
    rows = (sheets_service().spreadsheets().values()
            .get(spreadsheetId=SPREADSHEET_ID, range=f"{_quote(MEETINGS_TAB)}!A2:L")
            .execute().get("values", []))
    meetings = []
    for number, row in enumerate(rows, start=2):
        row = row + [""] * (len(COLUMNS) - len(row))
        if row[0].strip():
            meetings.append({"row": number, **dict(zip(COLUMNS, row))})
    return meetings


def _set_decision(meeting, status, decided_by, event_link="", meet_link=""):
    decided = f"{decided_by} {_now().strftime('%Y-%m-%d %H:%M')}"
    (sheets_service().spreadsheets().values()
     .update(spreadsheetId=SPREADSHEET_ID,
             range=f"{_quote(MEETINGS_TAB)}!H{meeting['row']}:K{meeting['row']}",
             valueInputOption="RAW",
             body={"values": [[status, decided, event_link, meet_link]]})
     .execute())


# ---------- agent tools ----------

def propose_meeting(project_id: str, purpose: str, duration_minutes: int = 30,
                    earliest_date: str = "") -> dict:
    """Proposes a meeting for a project's team at the first time everyone is free
    (Google Calendar free/busy, working hours, skipping weekends, holidays and
    leave). It does NOT create the meeting or send invitations: the proposal is
    saved as Pending until the GM approves it in the Approvals panel.

    Args:
        project_id: e.g. 'P03'.
        purpose: One short sentence, e.g. 'Discuss the overdue POS rollout'.
        duration_minutes: 15 to 120. Default 30.
        earliest_date: Optional yyyy-mm-dd. Nothing is proposed before this day.
    """
    duration_minutes = max(15, min(120, duration_minutes))
    purpose = purpose.strip()[:200] or "Project check-in"
    try:
        projects = _load_projects()
        project = _find_project(projects, project_id)
        if project is None:
            return {"status": "error", "message": f"No project with ID '{project_id}'."}
        attendees, missing = _attendees_for(project, _team_directory())
        if not attendees:
            return {"status": "error",
                    "message": "No attendee emails found. Add the team to the Team tab."}

        all_leave = [entry for p in projects for entry in p["leave"]]
        leave_days = set().union(*(leave_days_for(all_leave, a["name"]) for a in attendees))

        now = _now()
        first_day = max(now.date(), parse_date(earliest_date) or now.date())
        window_end = first_day + timedelta(days=MEETING_SEARCH_DAYS)
        emails = [a["email"] for a in attendees]
        busy, unreadable = _busy_times(
            emails,
            datetime(first_day.year, first_day.month, first_day.day, tzinfo=TZ),
            datetime(window_end.year, window_end.month, window_end.day, tzinfo=TZ))
        slots = _find_slots(busy, leave_days, timedelta(minutes=duration_minutes), first_day, now)
        if not slots:
            return {"status": "not_found",
                    "message": f"No time in the next {MEETING_SEARCH_DAYS} days when everyone is free."}

        start, end = slots[0]
        pid = project["info"]["Project ID"]
        meeting_id = f"M{now:%y%m%d%H%M}-{uuid.uuid4().hex[:4]}"
        warnings = []
        if unreadable:
            warnings.append("Calendar not readable (treated as free): " + ", ".join(unreadable))
        if missing:
            warnings.append("No email in the Team tab for: " + ", ".join(missing))

        row = [meeting_id, now.strftime("%Y-%m-%d %H:%M"), pid, purpose,
               start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"),
               ", ".join(emails), "Pending", "", "", "", "; ".join(warnings)]
        (sheets_service().spreadsheets().values()
         .append(spreadsheetId=SPREADSHEET_ID, range=f"{_quote(MEETINGS_TAB)}!A1",
                 valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                 body={"values": [row]})
         .execute())
        _write_log_row(pid, "Agent: meeting proposed",
                       f"{meeting_id}: {start:%Y-%m-%d %H:%M}, {purpose}")
    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {"status": "pending_gm_approval", "meeting_id": meeting_id, "project_id": pid,
            "start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M"),
            "attendees": [a["name"] for a in attendees],
            "other_free_times": [s.strftime("%Y-%m-%d %H:%M") for s, _ in slots[1:]],
            "warnings": warnings,
            "next_step": "Nothing has been sent. The GM must approve it in the Approvals panel."}


def list_meetings(status: str = "Pending") -> dict:
    """Lists meetings from the Meetings tab.

    Args:
        status: 'Pending', 'Approved', 'Rejected' or 'All'. Default 'Pending'.
    """
    try:
        meetings = _read_meetings()
    except Exception as e:
        return {"status": "error", "message": str(e)}
    wanted = status.strip().lower()
    chosen = [m for m in meetings if wanted == "all" or m["Status"].lower() == wanted]
    return {"status": "success", "count": len(chosen),
            "meetings": [{k: m[k] for k in ("Meeting ID", "Project ID", "Purpose", "Start",
                                            "End", "Status", "Notes")} for m in chosen]}


# ---------- server only (the agent cannot call these) ----------

def pending_meetings():
    return [{"id": m["Meeting ID"], "project": m["Project ID"], "purpose": m["Purpose"],
             "start": m["Start"], "end": m["End"],
             "attendees": [e.strip() for e in m["Attendees"].split(",") if e.strip()],
             "notes": m["Notes"]}
            for m in _read_meetings() if m["Status"].strip().lower() == "pending"]

def approve_meeting(meeting_id, approved_by):
    meeting = next((m for m in _read_meetings() if m["Meeting ID"] == meeting_id), None)
    if meeting is None:
        return {"ok": False, "message": "Meeting not found."}
    if meeting["Status"].strip().lower() != "pending":
        return {"ok": False, "message": f"This meeting is already {meeting['Status'].lower()}."}

    start = datetime.strptime(meeting["Start"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    end = datetime.strptime(meeting["End"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    if start <= _now():
        return {"ok": False, "message": "This time has passed. Ask the agent to propose a new time."}

    emails = [e.strip() for e in meeting["Attendees"].split(",") if e.strip()]
    busy, _ = _busy_times(emails, start, end)
    taken = [email for email, times in busy.items()
             if any(b_start < end and start < b_end for b_start, b_end in times)]
    if taken:
        return {"ok": False, "message": "No longer free for: " + ", ".join(taken)
                + ". Ask the agent to propose a new time."}

    event = calendar_service().events().insert(
        calendarId="primary", conferenceDataVersion=1, sendUpdates="all",
        body={
            "summary": f"[{meeting['Project ID']}] {meeting['Purpose']}",
            "description": f"Proposed by PM Agent. Approved by {approved_by}.",
            "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
            "attendees": [{"email": e} for e in emails],
            "conferenceData": {"createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
        }).execute()

    event_link = event.get("htmlLink", "")
    meet_link = event.get("hangoutLink", "")
    _set_decision(meeting, "Approved", approved_by, event_link, meet_link)
    _write_log_row(meeting["Project ID"], "GM: meeting approved",
                   f"{meeting_id} approved by {approved_by}. Invitations sent.", event_link)
    return {"ok": True, "message": "Meeting created and invitations sent.",
            "event_link": event_link, "meet_link": meet_link}



def reject_meeting(meeting_id, rejected_by):
    meeting = next((m for m in _read_meetings() if m["Meeting ID"] == meeting_id), None)
    if meeting is None:
        return {"ok": False, "message": "Meeting not found."}
    if meeting["Status"] != "Pending":
        return {"ok": False, "message": f"This meeting is already {meeting['Status'].lower()}."}
    _set_decision(meeting, "Rejected", rejected_by)
    _write_log_row(meeting["Project ID"], "GM: meeting rejected",
                   f"{meeting_id} rejected by {rejected_by}.")
    return {"ok": True, "message": "Meeting rejected. Nothing was sent."}