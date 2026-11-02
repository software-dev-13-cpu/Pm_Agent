from google.adk.models.lite_llm import LiteLlm



PROVIDER = "groq"   # "groq" or "gemini"

if PROVIDER == "gemini":
    MODEL = "gemini-3-flash-preview"
else:
    MODEL = LiteLlm(
    model="groq/openai/gpt-oss-120b",
    extra_body={"include_reasoning": False},
)


SPREADSHEET_ID = "1wPq5zOe0DWvZZEbSC8P4hMRZTOuiYBd-vLovRW2swvs"
REGISTER_TAB = "Register"
LOG_TAB = "Log"


TIMEZONE = "Asia/Colombo"
STALE_AFTER_WORKING_DAYS = 3

# Public holidays (yyyy-mm-dd) that don't count as working days, e.g. Poya days.
HOLIDAYS = [
]


LOG_TAB = "Log"   # skip this line if you already have it from Stage 1
VALID_STATUSES = ["Not started", "On track", "At risk", "Blocked", "Done"]