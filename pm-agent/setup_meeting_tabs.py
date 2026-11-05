from pm_agent.config import SPREADSHEET_ID, TEAM_TAB, MEETINGS_TAB
from pm_agent.google_auth import sheets_service
from pm_agent.meetings import COLUMNS
from pm_agent.tools import _load_projects, _quote


def tab_ids(sheets):
    meta = sheets.get(spreadsheetId=SPREADSHEET_ID, fields="sheets.properties(title,sheetId)").execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def add_tab(sheets, title, headers, protect):
    reply = sheets.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [
        {"addSheet": {"properties": {"title": title, "gridProperties": {"frozenRowCount": 1}}}}
    ]}).execute()
    sheet_id = reply["replies"][0]["addSheet"]["properties"]["sheetId"]

    sheets.values().update(spreadsheetId=SPREADSHEET_ID, range=f"{_quote(title)}!A1",
                           valueInputOption="RAW", body={"values": [headers]}).execute()

    requests = [{"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold"}}]
    if protect:
        requests.append({"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": sheet_id},
            "description": f"{title} is written by the PM Agent only",
            "editors": {"users": []}}}})
    sheets.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": requests}).execute()
    print(f"Created the '{title}' tab.")


def fill_team(sheets):
    existing = (sheets.values().get(spreadsheetId=SPREADSHEET_ID, range=f"{_quote(TEAM_TAB)}!A2:B")
                .execute().get("values", []))
    known = {row[0].strip().lower() for row in existing if row and row[0].strip()}

    people = {}
    for project in _load_projects():
        info = project["info"]
        responsible = info.get("Responsible developer", "").strip()
        email = info.get("Responsible email", "").strip()
        names = [n.strip() for n in info.get("Team", "").split(",") if n.strip()]
        for name in names + ([responsible] if responsible else []):
            people.setdefault(name.lower(), [name, ""])
        if responsible and email:
            people[responsible.lower()][1] = email

    new_rows = [people[key] for key in sorted(people) if key not in known]
    if new_rows:
        sheets.values().append(spreadsheetId=SPREADSHEET_ID, range=f"{_quote(TEAM_TAB)}!A1",
                               valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                               body={"values": new_rows}).execute()
    missing = [name for name, email in new_rows if not email]
    print(f"Team tab: {len(new_rows)} new name(s) added, {len(known)} already there.")
    if missing:
        print("Fill in these emails by hand: " + ", ".join(missing))


def main():
    sheets = sheets_service().spreadsheets()
    tabs = tab_ids(sheets)

    if TEAM_TAB not in tabs:
        add_tab(sheets, TEAM_TAB, ["Name", "Email"], protect=False)
    else:
        print(f"The '{TEAM_TAB}' tab already exists.")
    fill_team(sheets)

    if MEETINGS_TAB not in tabs:
        add_tab(sheets, MEETINGS_TAB, COLUMNS, protect=True)
    else:
        print(f"The '{MEETINGS_TAB}' tab already exists.")


if __name__ == "__main__":
    main()