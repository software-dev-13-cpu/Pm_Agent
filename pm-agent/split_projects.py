import re

from pm_agent.config import SPREADSHEET_ID, INDEX_TAB, PROJECT_TAB_PATTERN
from pm_agent.google_auth import sheets_service


def main():
    sheets = sheets_service().spreadsheets()

    meta = sheets.get(spreadsheetId=SPREADSHEET_ID,
                      fields="sheets.properties(title,sheetId)").execute()
    tabs = [s["properties"] for s in meta["sheets"]]
    titles = [t["title"] for t in tabs]
    project_tabs = [t for t in tabs if re.match(PROJECT_TAB_PATTERN, t["title"])]

    if INDEX_TAB in titles:
        existing = sheets.values().get(spreadsheetId=SPREADSHEET_ID,
                                       range=f"{INDEX_TAB}!A2:C").execute().get("values", [])
        if existing:
            print(f"The '{INDEX_TAB}' tab already has rows. Stopping so nothing is duplicated.")
            return
    else:
        sheets.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [
            {"addSheet": {"properties": {"title": INDEX_TAB, "index": 0}}}
        ]}).execute()

    index_rows = [["Project ID", "Project name", "Sheet link"]]
    for tab in project_tabs:
        title = tab["title"]
        project_id, _, name = title.partition(" ")

        new_file = sheets.create(body={"properties": {"title": f"PM - {title}"}},
                                 fields="spreadsheetId,sheets.properties.sheetId").execute()
        new_id = new_file["spreadsheetId"]
        blank_sheet_id = new_file["sheets"][0]["properties"]["sheetId"]

        copied = sheets.sheets().copyTo(spreadsheetId=SPREADSHEET_ID, sheetId=tab["sheetId"],
                                        body={"destinationSpreadsheetId": new_id}).execute()

        sheets.batchUpdate(spreadsheetId=new_id, body={"requests": [
            {"deleteSheet": {"sheetId": blank_sheet_id}},
            {"updateSheetProperties": {"properties": {"sheetId": copied["sheetId"],
                                                      "title": "Progress"},
                                       "fields": "title"}},
        ]}).execute()

        link = f"https://docs.google.com/spreadsheets/d/{new_id}/edit"
        index_rows.append([project_id, name, link])
        print(f"{project_id}: created {link}")

    sheets.values().update(spreadsheetId=SPREADSHEET_ID, range=f"{INDEX_TAB}!A1",
                           valueInputOption="RAW", body={"values": index_rows}).execute()
    print(f"Done. {len(project_tabs)} project files created and listed in '{INDEX_TAB}'.")
    print("Check them, then delete the old project tabs from this file by hand.")


if __name__ == "__main__":
    main()