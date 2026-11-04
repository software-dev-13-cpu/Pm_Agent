from datetime import date, timedelta
import holidays

from .config import STALE_AFTER_WORKING_DAYS, BEHIND_SCHEDULE_POINTS

HOLIDAY_DATES = set()
NO_BLOCKER_WORDS = {"", "none", "no", "nil", "n/a", "na", "-"}


def set_holidays(extra_dates, today, official_dates=None):
    """Fills HOLIDAY_DATES with official Sri Lankan holidays plus extra dates.
    If official_dates is not given, the holidays library is used.
    Returns how many official holidays are known for this year."""
    if official_dates is None:
        official_dates = set(holidays.country_holidays(
            "LK", years=[today.year - 1, today.year, today.year + 1]).keys())
    HOLIDAY_DATES.clear()
    HOLIDAY_DATES.update(official_dates)
    HOLIDAY_DATES.update(extra_dates)
    return sum(1 for d in official_dates if d.year == today.year)

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


def has_blocker(text):
    return text.strip().lower() not in NO_BLOCKER_WORDS


def expected_percent(start, deadline, today):
    """How complete the project should be today if work were spread evenly."""
    if start is None or deadline is None or deadline <= start:
        return None
    fraction = (today - start).days / (deadline - start).days
    return round(max(0.0, min(1.0, fraction)) * 100)


def check_project(project, today):
    """Returns a list of flags for one project tab. Empty list = no problems."""
    info = project["info"]
    entries = project["entries"]
    flags = []

    if project.get("access_error"):
        return [{"rule": "access_error",
                 "detail": f"Could not read the project sheet: {project['access_error']}"}]

    if info.get("Status", "Active").strip() != "Active":
        return flags

    latest = entries[-1] if entries else None
    pct = latest["pct"] if latest else 0

    if pct >= 100:
        return [{"rule": "ready_to_close",
                 "detail": "Reported 100% complete. The GM can set Status to Completed."}]

    start = parse_date(info.get("Start date", ""))
    deadline = parse_date(info.get("Target deadline", ""))

    if deadline is None:
        flags.append({"rule": "missing_deadline",
                      "detail": "Target deadline is blank or not yyyy-mm-dd."})
    elif deadline < today:
        flags.append({"rule": "overdue",
                      "detail": f"Deadline was {deadline}, {(today - deadline).days} days ago, at {pct:.0f}% complete."})

    if latest is None:
        flags.append({"rule": "no_updates", "detail": "No progress rows yet."})
    else:
        idle = working_days_between(latest["date"], today)
        if idle >= STALE_AFTER_WORKING_DAYS:
            flags.append({"rule": "stale",
                          "detail": f"No update for {idle} working days (last on {latest['date']})."})
        if has_blocker(latest["blocker"]):
            flags.append({"rule": "blocked",
                          "detail": f"Latest update reports a blocker: {latest['blocker']}"})

    expected = expected_percent(start, deadline, today)
    if expected is not None and expected - pct >= BEHIND_SCHEDULE_POINTS:
        flags.append({"rule": "behind_schedule",
                      "detail": f"{pct:.0f}% complete, about {expected}% expected by now."})

    if "id_mismatch" in project:
        flags.append({"rule": "id_mismatch",
                      "detail": f"The project sheet says Project ID '{project['id_mismatch']}', "
                                f"but the index says '{info.get('Project ID')}'."})

    if project["bad_rows"]:
        rows = ", ".join(str(r) for r in project["bad_rows"])
        flags.append({"rule": "data_issue",
                      "detail": f"Row(s) {rows} have an invalid date or completion % and were ignored."})

    return flags


def snapshot(project, today):
    """All the figures the summary needs for one project, computed in code."""
    info = project["info"]
    entries = project["entries"]
    latest = entries[-1] if entries else None
    flags = check_project(project, today)
    rules = {f["rule"] for f in flags}

    status = info.get("Status", "Active").strip()
    if status != "Active":
        health = status
    elif "overdue" in rules:
        health = "Overdue"
    elif rules & {"blocked", "behind_schedule", "stale", "no_updates","missing_deadline", "access_error"}:
        health = "Needs attention"
    elif "ready_to_close" in rules:
        health = "Ready to close"
    else:
        health = "On track"

    start = parse_date(info.get("Start date", ""))
    deadline = parse_date(info.get("Target deadline", ""))
    return {
        "project_id": info.get("Project ID", ""),
        "project": info.get("Project name", ""),
        "responsible": info.get("Responsible developer", ""),
        "target_deadline": deadline.isoformat() if deadline else "",
        "days_left": (deadline - today).days if deadline else None,
        "completion_pct": round(latest["pct"]) if latest else 0,
        "expected_pct": expected_percent(start, deadline, today),
        "last_update": latest["date"].isoformat() if latest else "",
        "health": health,
        "flags": flags,
        "latest_work": latest["work"] if latest else "",
    }