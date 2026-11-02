from datetime import date, timedelta

from .config import STALE_AFTER_WORKING_DAYS, HOLIDAYS

HOLIDAY_DATES = {date.fromisoformat(d) for d in HOLIDAYS}


def parse_date(value):
    """Turns 'yyyy-mm-dd' into a date. Returns None if blank or invalid."""
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def working_days_between(start, end):
    """Counts working days after start, up to and including end."""
    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in HOLIDAY_DATES:
            days += 1
    return days


def check_project(project, today):
    """Returns a list of delay flags for one project. Empty list = no problems."""
    flags = []
    status = project.get("Status", "").strip()

    if status == "Done":
        return flags

    due = parse_date(project.get("Due", ""))
    if due is None:
        flags.append({"rule": "missing_due_date",
                      "detail": "Due date is blank or not in yyyy-mm-dd format."})
    elif due < today:
        flags.append({"rule": "overdue",
                      "detail": f"Was due {due}, {(today - due).days} days ago."})

    last_update = parse_date(project.get("Last update", ""))
    if last_update is None:
        flags.append({"rule": "no_update_date",
                      "detail": "Last update is blank or not in yyyy-mm-dd format."})
    else:
        idle = working_days_between(last_update, today)
        if idle >= STALE_AFTER_WORKING_DAYS:
            flags.append({"rule": "stale",
                          "detail": f"No update for {idle} working days (last on {last_update})."})

    if status in ("At risk", "Blocked"):
        flags.append({"rule": "at_risk", "detail": f"Status is '{status}'."})

    return flags