"""
EA Studio backend: AI chat assistant + contact form endpoint.

AI layer adapted from Avinashb722/jarvis-ai-assistant (engine/dual_ai.py,
engine/ai_fallback_system.py): Groq (llama-3.1-8b -> llama-3.3-70b on rate
limit) then Gemini as fallback. The desktop-control features of that repo
(pyautogui, ADB, face auth, voice) are intentionally NOT included: they must
never be reachable from a public website.
"""
import html
import json
import os
import secrets
import smtplib
import threading
import time
from collections import defaultdict, deque
from email.message import EmailMessage
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, request, send_from_directory

load_dotenv()
BASE = Path(__file__).parent
SYSTEM_PROMPT = (BASE / "system_prompt.txt").read_text(encoding="utf-8")

GROQ_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
PROVIDERS = [p.strip() for p in os.getenv("AI_PROVIDERS", "groq,gemini").split(",") if p.strip()]
GROQ_PRIMARY = os.getenv("GROQ_MODEL_PRIMARY", "llama-3.1-8b-instant")
GROQ_FALLBACK = os.getenv("GROQ_MODEL_FALLBACK", "llama-3.3-70b-versatile")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "15"))

MAX_MESSAGE_CHARS = 1000
MAX_HISTORY_TURNS = 8          # user+assistant pairs kept per session
SESSION_TTL = 60 * 60          # seconds

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024  # 32 KB request bodies

# ---------- tiny in-memory stores (use Redis if you scale past 1 process) ----------
_sessions: dict[str, dict] = {}
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")


def rate_limited(ip: str) -> bool:
    now = time.time()
    with _lock:
        q = _hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
    return False


def get_history(sid: str) -> list:
    now = time.time()
    with _lock:
        for k in [k for k, v in _sessions.items() if now - v["t"] > SESSION_TTL]:
            _sessions.pop(k, None)
        s = _sessions.setdefault(sid, {"t": now, "msgs": []})
        s["t"] = now
        return s["msgs"]


# ---------- AI providers ----------
class RateLimitError(Exception):
    pass


def call_groq(messages: list, model: str) -> str:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 400},
        timeout=20,
    )
    if r.status_code == 429:
        raise RateLimitError("groq rate limit")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def call_gemini(messages: list) -> str:
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages if m["role"] != "system"
    ]
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY},
        json={
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 400},
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def ask_ai(history: list, user_msg: str) -> str | None:
    """Try providers in order, like Jarvis' try_all_ai_providers()."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_msg}
    ]
    for provider in PROVIDERS:
        try:
            if provider == "groq" and GROQ_KEY:
                try:
                    return call_groq(messages, GROQ_PRIMARY)
                except RateLimitError:
                    return call_groq(messages, GROQ_FALLBACK)  # 8B -> 70B, as in Jarvis
            if provider == "gemini" and GEMINI_KEY:
                return call_gemini(messages)
        except Exception as e:  # log server-side only; never leak details to visitors
            app.logger.warning("%s failed: %s", provider, e)
    return None


# ---------- routes ----------
@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True, groq=bool(GROQ_KEY), gemini=bool(GEMINI_KEY))


@app.post("/api/chat")
def chat():
    if rate_limited(client_ip()):
        return jsonify(error="Too many requests, please slow down."), 429
    data = request.get_json(silent=True) or {}
    text = str(data.get("message", "")).strip()
    if not text:
        return jsonify(error="Empty message."), 400
    text = text[:MAX_MESSAGE_CHARS]

    sid = request.cookies.get("sid") or secrets.token_urlsafe(16)
    history = get_history(sid)
    reply = ask_ai(history, text)
    if reply is None:
        reply = ("Sorry, I can't reach my AI service right now. "
                 "You can email ermiasalehegn5@gmail.com or use the contact form below.")
    else:
        with _lock:
            history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
            del history[:-MAX_HISTORY_TURNS * 2]

    # The page inserts `reply` with innerHTML, so escape it to prevent XSS.
    resp = make_response(jsonify(reply=html.escape(reply)))
    resp.set_cookie("sid", sid, max_age=SESSION_TTL, httponly=True, samesite="Lax",
                    secure=request.is_secure)
    return resp


@app.post("/api/send")
def send():
    if rate_limited(client_ip()):
        return jsonify(error="Too many requests."), 429
    d = request.get_json(silent=True) or {}
    name = str(d.get("name", "")).strip()[:100]
    email = str(d.get("email", "")).strip()[:200]
    if not name or "@" not in email or any(c in email for c in "\r\n"):
        return jsonify(error="Name and a valid email are required."), 400
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "name": name,
        "email": email,
        "subject": str(d.get("subject", "")).strip()[:150].replace("\n", " "),
        "phone": str(d.get("phone", "")).strip()[:40],
        "message": str(d.get("message", "")).strip()[:5000],
    }

    smtp_user, smtp_pass = os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    if smtp_user and smtp_pass:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[Website] {record['subject'] or 'New contact'}"
            msg["From"] = smtp_user
            msg["To"] = os.getenv("CONTACT_TO", smtp_user)
            msg["Reply-To"] = email
            msg.set_content("\n".join(f"{k}: {v}" for k, v in record.items()))
            with smtplib.SMTP(os.getenv("SMTP_HOST", "smtp.gmail.com"),
                              int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
                s.starttls()
                s.login(smtp_user, smtp_pass)
                s.send_message(msg)
            return jsonify(ok=True)
        except Exception as e:
            app.logger.warning("SMTP failed, saving to file: %s", e)

    with _lock, open(BASE / "messages.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
