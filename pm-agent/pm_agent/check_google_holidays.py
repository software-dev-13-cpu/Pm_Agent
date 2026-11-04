from datetime import date, datetime
from zoneinfo import ZoneInfo

import holidays

from pm_agent.config import LK_HOLIDAY_CALENDAR
from pm_agent.google_auth import calendar_service

tz = ZoneInfo("Asia/Colombo")
events = (
    calendar_service().events()
    .list(calendarId=LK_HOLIDAY_CALENDAR,
          timeMin=datetime(2026, 1, 1, tzinfo=tz).isoformat(),
          timeMax=datetime(2028, 1, 1, tzinfo=tz).isoformat(),
          singleEvents=True, orderBy="startTime", maxResults=250)
    .execute().get("items", [])
)
library = holidays.country_holidays("LK", years=[2026, 2027])

for event in events:
    day = event.get("start", {}).get("date", "")
    kind = event.get("description", "").split("\n")[0] or "(no description)"
    in_library = "yes" if day and date.fromisoformat(day) in library else "NO"
    print(f"{day}  {event.get('summary', '')}  |  {kind}  |  in library: {in_library}")