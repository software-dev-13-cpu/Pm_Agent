import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "pm_agent" / ".env")

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH", "")
SESSION_HOURS = 12
COOKIE = "pm_session"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
logins = {}  # token -> {"user": ..., "expires": ..., "session_id": ...}


# ---------- login ----------

def check_password(password):
    try:
        salt, expected = APP_PASSWORD_HASH.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(digest, expected)


def current_login(request: Request):
    token = request.cookies.get(COOKIE, "")
    login = logins.get(token)
    if not login or login["expires"] < time.time():
        logins.pop(token, None)
        raise HTTPException(status_code=401, detail="Please log in.")
    return login


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    ok_user = hmac.compare_digest(body.username.strip(), APP_USERNAME)
    if not (APP_USERNAME and ok_user and check_password(body.password)):
        time.sleep(1)
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    token = secrets.token_urlsafe(32)
    logins[token] = {"user": APP_USERNAME, "expires": time.time() + SESSION_HOURS * 3600,
                     "session_id": None}
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                        max_age=SESSION_HOURS * 3600)
    return {"user": APP_USERNAME}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    logins.pop(request.cookies.get(COOKIE, ""), None)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"user": current_login(request)["user"]}


# ---------- agent ----------

_runner = None


def get_runner():
    global _runner
    if _runner is None:
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from pm_agent.agent import root_agent
        _runner = Runner(agent=root_agent, app_name="pm_agent",
                         session_service=InMemorySessionService())
    return _runner


async def ask_agent(login, message):
    """Sends one message to the agent. Returns (reply text, tool names used)."""
    from google.genai.types import Content, Part

    runner = get_runner()
    if login["session_id"] is None:
        session = await runner.session_service.create_session(
            app_name="pm_agent", user_id=login["user"])
        login["session_id"] = session.id

    tools_used = []
    reply = ""
    async for event in runner.run_async(
            user_id=login["user"], session_id=login["session_id"],
            new_message=Content(role="user", parts=[Part(text=message)])):
        parts = event.content.parts if event.content and event.content.parts else []
        for part in parts:
            if part.function_call:
                tools_used.append(part.function_call.name)
        if event.is_final_response() and parts:
            reply = "".join(p.text for p in parts
                            if getattr(p, "text", None) and not getattr(p, "thought", False))
    return reply, tools_used


class ChatBody(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(body: ChatBody, request: Request):
    login = current_login(request)
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty.")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="Message is too long (max 2000 characters).")
    try:
        reply, tools_used = await ask_agent(login, message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"The agent could not answer: {e}")
    return {"reply": reply or "(The agent returned no text.)", "tools": tools_used}


@app.post("/api/new-chat")
def new_chat(request: Request):
    current_login(request)["session_id"] = None
    return {"ok": True}


# ---------- page ----------
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")