from datetime import date

from pm_agent.rules import check_project, working_days_between, expected_percent
from pm_agent.sheet_layout import parse_project_tab

TODAY = date(2026, 11, 2)  # a Monday


def make_tab(deadline="2026-12-01", start="2026-10-01", status="Active", rows=()):
    sheet = [
        ["Project ID", "P99"],
        ["Start date", start],
        ["Target deadline", deadline],
        ["Status", status],
        [],
        ["Date", "Developer", "Work done", "Completion %", "Blockers"],
    ] + [list(r) for r in rows]
    return parse_project_tab("P99 Test", 0, sheet)


def rules_for(**kwargs):
    return [f["rule"] for f in check_project(make_tab(**kwargs), TODAY)]


assert working_days_between(date(2026, 10, 28), TODAY) == 3
assert expected_percent(date(2026, 10, 1), date(2026, 12, 1), TODAY) == 52

# healthy: updated today, 60% done (52% expected)
assert rules_for(rows=[["2026-11-02", "Kasun", "API", "60"]]) == []

# stale: last row 3 working days ago
assert rules_for(rows=[["2026-10-28", "Kasun", "API", "60"]]) == ["stale"]

# overdue: deadline passed, not 100%
assert rules_for(deadline="2026-10-30", rows=[["2026-11-02", "Kasun", "API", "90"]]) == ["overdue"]

# blocked: latest row has a blocker ("none" does not count)
assert rules_for(rows=[["2026-11-02", "Kasun", "API", "60", "Waiting for ERP access"]]) == ["blocked"]
assert rules_for(rows=[["2026-11-02", "Kasun", "API", "60", "none"]]) == []

# behind schedule: 20+ points below expected
assert rules_for(rows=[["2026-11-02", "Kasun", "API", "25"]]) == ["behind_schedule"]

# rows out of order: the latest DATE counts, not the last row
assert rules_for(rows=[["2026-11-02", "Kasun", "API", "60"],
                       ["2026-10-20", "Nimali", "Design", "30"]]) == []

# bad rows are ignored and reported
assert rules_for(rows=[["2026-11-02", "Kasun", "API", "60"],
                       ["30/10", "Nimali", "Test", "about 50"]]) == ["data_issue"]

# 100% -> ready to close; non-Active -> no flags
assert rules_for(rows=[["2026-10-01", "Kasun", "Done", "100"]]) == ["ready_to_close"]
assert rules_for(status="On hold", deadline="2026-01-01") == []

print("All rule tests passed.")