from pm_agent.config import SPREADSHEET_ID, INDEX_TAB, LEAVE_TAB
from pm_agent.google_auth import sheets_service
from pm_agent.tools import _file_id, _quote

HEADERS = ["Developer", "From", "To", "Type", "Notes"]
LEAVE_TYPES = ["Annual", "Casual", "Sick", "Half day", "Other"]


def validation(sheet_id, column, condition):
    return {"setDataValidation": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 1000,
                  "startColumnIndex": column, "endColumnIndex": column + 1},
        "rule": {"condition": condition, "strict": True, "showCustomUi": True},
    }}


def one_of(items):
    return {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": item} for item in items]}


def main():
    sheets = sheets_service().spreadsheets()
    index = (sheets.values().get(spreadsheetId=SPREADSHEET_ID, range=f"{_quote(INDEX_TAB)}!A2:C")
             .execute().get("values", []))

    for row in index:
        project_id, _, link = (cell.strip() for cell in (row + ["", "", ""])[:3])
        file_id = _file_id(link)
        if not project_id or not file_id:
            print(f"Skipping row {row}: no Project ID or link.")
            continue

        meta = sheets.get(spreadsheetId=file_id, fields="sheets.properties(title,sheetId)").execute()
        if LEAVE_TAB in [s["properties"]["title"] for s in meta["sheets"]]:
            print(f"{project_id}: already has a {LEAVE_TAB} tab.")
            continue

        details = sheets.values().get(spreadsheetId=file_id, range="A1:B10").execute().get("values", [])
        team = next((r[1] for r in details if len(r) > 1 and r[0].strip() == "Team"), "")
        names = [n.strip() for n in team.split(",") if n.strip()]

        reply = sheets.batchUpdate(spreadsheetId=file_id, body={"requests": [
            {"addSheet": {"properties": {"title": LEAVE_TAB, "index": 1,
                                         "gridProperties": {"frozenRowCount": 1}}}}
        ]}).execute()
        sheet_id = reply["replies"][0]["addSheet"]["properties"]["sheetId"]

        sheets.values().update(spreadsheetId=file_id, range=f"{_quote(LEAVE_TAB)}!A1",
                               valueInputOption="RAW", body={"values": [HEADERS]}).execute()

        requests = [
            {"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold"}},
            {"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 1000,
                          "startColumnIndex": 1, "endColumnIndex": 3},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            validation(sheet_id, 1, {"type": "DATE_IS_VALID"}),
            validation(sheet_id, 2, {"type": "DATE_IS_VALID"}),
            validation(sheet_id, 3, one_of(LEAVE_TYPES)),
        ]
        if names:
            requests.append(validation(sheet_id, 0, one_of(names)))
        sheets.batchUpdate(spreadsheetId=file_id, body={"requests": requests}).execute()
        print(f"{project_id}: {LEAVE_TAB} tab added (team: {', '.join(names) or 'none listed'}).")


if __name__ == "__main__":
    main()