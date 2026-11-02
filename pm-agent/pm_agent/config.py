from google.adk.models.lite_llm import LiteLlm

PROVIDER = "groq"   # "groq" or "gemini"

if PROVIDER == "gemini":
    MODEL = "gemini-3-flash-preview"
else:
    MODEL = LiteLlm(
        model="groq/openai/gpt-oss-120b",
        extra_body={"include_reasoning": False},
    )

SPREADSHEET_ID = "1tDrfRWrKGalTaRcJCw_uUZ3wzNO-zaeHsMmRsllQVEs"
SUMMARY_TAB = "Summary"
LOG_TAB = "Log"
PROJECT_TAB_PATTERN = r"^P\d+"

TIMEZONE = "Asia/Colombo"
STALE_AFTER_WORKING_DAYS = 3
BEHIND_SCHEDULE_POINTS = 20

# Public holidays (yyyy-mm-dd) that don't count as working days, e.g. Poya days.
HOLIDAYS = [
]