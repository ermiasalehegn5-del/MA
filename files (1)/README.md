# EA Studio backend (AI assistant + contact form)

Serves your `home.html` and the two endpoints it already calls:

| Endpoint | Purpose |
|---|---|
| `POST /api/chat` `{message}` -> `{reply}` | AI assistant (Groq -> Gemini fallback, per-visitor memory) |
| `POST /api/send` `{name,email,subject,phone,message}` | Contact form (SMTP email, or saved to `messages.jsonl`) |
| `GET /api/health` | Shows which providers have keys |

## Run
```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # add GROQ_API_KEY and/or GEMINI_API_KEY
python app.py               # http://localhost:5000
```
Free keys: https://console.groq.com/keys and https://aistudio.google.com/apikey

## Deploy
`gunicorn -w 2 -b 0.0.0.0:$PORT app:app` (Render, Railway, Fly.io, a VPS...).
Set the env vars from `.env.example` in the host's dashboard. Serve over HTTPS.
If you scale past one process, move sessions/rate-limit to Redis.

## Notes
- Edit `system_prompt.txt` to change what the assistant knows about your studio.
- Keep API keys in `.env` only. The original Jarvis repo has keys/passwords committed in files; never copy that pattern.
- Jarvis' desktop features (pyautogui, ADB, face auth) are deliberately left out of a public backend.
