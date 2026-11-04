from google.adk.models.lite_llm import LiteLlm

PROVIDER = "groq"   # "groq" or "gemini"

if PROVIDER == "gemini":
    MODEL = "gemini-3-flash-preview"
else:
    MODEL = LiteLlm(
        model="groq/openai/gpt-oss-120b",
        extra_body={"include_reasoning": False},
    )

SPREADSHEET_ID = "120DLTcB6M-fTSj6gqv1bSSyxuSR2o8D6hRBUsAY4vLQ"
SUMMARY_TAB = "Summary"
INDEX_TAB = "Projects"
LOG_TAB = "Log"
PROJECT_TAB_PATTERN = r"^P\d+"

TIMEZONE = "Asia/Colombo"
STALE_AFTER_WORKING_DAYS = 3
BEHIND_SCHEDULE_POINTS = 20

# Public holidays (yyyy-mm-dd) that don't count as working days, e.g. Poya days.
HOLIDAYS_TAB = "Holidays"
LK_HOLIDAY_CALENDAR = "en.lk#holiday@group.v.calendar.google.com"


