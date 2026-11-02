from datetime import date

from pm_agent.rules import check_project, working_days_between

TODAY = date(2026, 11, 2)  # a Monday


def rules_for(project):
    return [f["rule"] for f in check_project(project, TODAY)]


# Wed 28 Oct -> Mon 2 Nov = Thu, Fri, Mon = 3 working days
assert working_days_between(date(2026, 10, 28), TODAY) == 3
# Fri 30 Oct -> Mon 2 Nov = only Mon = 1 (weekend skipped)
assert working_days_between(date(2026, 10, 30), TODAY) == 1

# overdue: due last Friday, updated today
assert rules_for({"Status": "On track", "Due": "2026-10-30",
                  "Last update": "2026-11-02"}) == ["overdue"]

# stale: exactly 3 working days
assert rules_for({"Status": "On track", "Due": "2026-12-31",
                  "Last update": "2026-10-28"}) == ["stale"]

# not stale yet: 2 working days
assert rules_for({"Status": "On track", "Due": "2026-12-31",
                  "Last update": "2026-10-29"}) == []

# blocked
assert rules_for({"Status": "Blocked", "Due": "2026-12-31",
                  "Last update": "2026-11-02"}) == ["at_risk"]

# done projects never flag, even if overdue and stale
assert rules_for({"Status": "Done", "Due": "2026-01-01",
                  "Last update": "2026-01-01"}) == []

# bad data is flagged, not crashed on
assert rules_for({"Status": "On track", "Due": "",
                  "Last update": "next week"}) == ["missing_due_date", "no_update_date"]

print("All rule tests passed.")