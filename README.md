# OutboundAI

Production-grade AI outbound voice calling SaaS:

- **Telephony:** Vobiz SIP trunk (dial-out)
- **Voice AI:** Google **Gemini Live** real-time
- **Orchestration:** LiveKit Agents 1.x
- **Backend:** FastAPI + APScheduler
- **DB:** Supabase (PostgreSQL)
- **Dashboard:** Single-file HTML + Chart.js
- **Deployment:** Docker, Coolify-ready

---

## 🔒 Configuration policy — single source of truth

**Real environment variables on the host (VPS / Coolify) are the ONLY source of truth for credentials and infra config.** No `.env` file or DB row can override them at runtime.

| Where it lives | What lives there |
| --- | --- |
| **VPS env vars** | All credentials, `LIVEKIT_*`, `GOOGLE_API_KEY`, `GEMINI_*`, `VOBIZ_*`, `OUTBOUND_TRUNK_ID`, `DEFAULT_TRANSFER_NUMBER`, `SUPABASE_*`, `TWILIO_*`, `S3_*`, `CALCOM_*` |
| **Supabase `settings` table** | Only `system_prompt` and `ENABLED_TOOLS` — UI-editable text. Nothing else. |
| **Supabase tables** (`appointments`, `call_logs`, `agent_profiles`, `campaigns`, …) | Application data |
| **`.env` file** | Optional, **local dev only**. Loaded only when `OUTBOUNDAI_LOAD_DOTENV=true` is set, and even then with `override=False`. |

Implications:

- The Settings tab in the dashboard is **read-only diagnostics** for credentials. To change an API key, update it in Coolify and redeploy.
- After clicking ⚡ Create SIP Trunk, the dashboard shows the new trunk ID; you must paste it into `OUTBOUND_TRUNK_ID` in Coolify and redeploy.
- The dashboard's `Save` buttons for credential groups have been removed; the only credentials path is the host environment.

---

## File map

```
agent.py                  LiveKit worker — Gemini Live entrypoint
server.py                 FastAPI backend
db.py                     Supabase async DB + env-only policy
tools.py                  9 LLM function tools
prompts.py                System prompt template
start.sh                  Production startup (bash, traps, fail-fast)
Dockerfile                Python 3.11-slim + HEALTHCHECK
.dockerignore             Keeps .env, .git, node_modules out of the image
requirements.txt          Python deps
supabase_schema.sql       Run once in Supabase SQL Editor
.env.example              Template for local dev only
ui/index.html             Single-file dashboard
```

---

## Required VPS environment variables

Set every one of these in Coolify (or your VPS env). Lower group is optional.

```bash
# LiveKit Cloud (cloud.livekit.io)
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=

# Google Gemini (aistudio.google.com/app/apikey)
GOOGLE_API_KEY=
GEMINI_MODEL=gemini-3.1-flash-live-preview
GEMINI_TTS_VOICE=Aoede
USE_GEMINI_REALTIME=true
GREETING_READY_DELAY_SECONDS=0.1    # optional; lower first-greeting pause, set 0 to disable

# Vobiz SIP
VOBIZ_SIP_DOMAIN=
VOBIZ_USERNAME=your_username
VOBIZ_PASSWORD=your_password
VOBIZ_OUTBOUND_NUMBER=
OUTBOUND_TRUNK_ID=       # filled after Create SIP Trunk
DEFAULT_TRANSFER_NUMBER=

# Supabase (Project Settings → API)
SUPABASE_URL=
SUPABASE_SERVICE_KEY=

# ── Optional ──
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_ENDPOINT_URL=
S3_REGION=ap-northeast-1
S3_BUCKET=call-recordings
CALCOM_API_KEY=
CALCOM_EVENT_TYPE_ID=
CALCOM_TIMEZONE=Asia/Kolkata
DEEPGRAM_API_KEY=        # only for the pipeline fallback
```

If any of `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `GOOGLE_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` are missing, `start.sh` aborts with a clear message before launching anything.

---

## VPS deployment via Coolify

1. **Supabase** → SQL Editor → run `supabase_schema.sql`.
2. Push this repo to GitHub.
3. **Coolify** → New Resource → Application → connect the repo. Coolify auto-detects the `Dockerfile`.
4. **Build pack:** Dockerfile.
5. **Port:** `8000`. Coolify will reverse-proxy it as HTTPS automatically.
6. **Environment Variables:** paste every variable from the section above.
7. **Health check:** Coolify uses the Dockerfile `HEALTHCHECK` automatically (`GET /healthz`).
8. Deploy.
9. After the first deploy, open the dashboard:
   - `Settings` → verify every credential shows `✓ set in VPS env`.
   - `Settings → ⚡ Create SIP Trunk` → copy the returned trunk ID into Coolify as `OUTBOUND_TRUNK_ID` → redeploy.
   - `Single Call` → call your own number to verify end-to-end audio.

### How processes run inside the container

- `start.sh` (PID 1, bash) traps `SIGTERM`, validates required env vars, then starts:
  - **uvicorn** on `0.0.0.0:8000` (FastAPI dashboard + REST API)
  - **`python agent.py start`** — LiveKit worker (outbound websocket to LiveKit Cloud, no inbound port needed)
- `wait -n` ensures **either** child crashing kills the container so Coolify restarts it.
- The `HEALTHCHECK` curls `http://127.0.0.1:8000/healthz` every 30 s.

### Outbound-only network requirements

The container needs egress to:

- LiveKit Cloud (`*.livekit.cloud`, port 443/wss)
- Google Generative AI (`generativelanguage.googleapis.com`, 443)
- Supabase (`*.supabase.co`, 443)
- Vobiz SIP (RTP/SIP via LiveKit's network — handled by LiveKit Cloud, not directly by this container)

**No inbound port** required other than 8000 for the dashboard.

---

## Local development

```bash
cp .env.example .env
# fill in the keys

export OUTBOUNDAI_LOAD_DOTENV=true
pip install -r requirements.txt
bash start.sh
```

`OUTBOUNDAI_LOAD_DOTENV=true` is the **only** way to make the app read the `.env` file. Without it, `.env` is ignored — exactly as it will be in production.

---

## Smoke test after deploy

```bash
# Replace with your Coolify URL
APP=https://outbound.example.com

# 1. Health
curl -fsS $APP/healthz                               # → {"status":"ok"}

# 2. Settings show env vars are configured
curl -fsS $APP/api/settings | head -c 500

# 3. Place a real call to your own phone
curl -fsS -X POST $APP/api/call \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+919876543210","lead_name":"Test"}'
```

If any of those fail, open `Logs` in the dashboard for the agent / server stack traces.

---

## Critical runtime rules (do not deviate)

| Rule | Why |
| --- | --- |
| Dial-first; `wait_until_answered=True` BEFORE `session.start()` | Otherwise session times out during ring |
| Never `close_on_disconnect=True` with SIP | SIP audio dropouts kill the session |
| All 3 silence-prevention configs (session resumption + window compression + LOW end-sensitivity) | Without them, calls go silent in 30–90 s |
| Gemini 3.1 opener uses `session.say()` — never fall back to `generate_reply()` for the first turn | Gemini 3.1 rejects `send_client_content`, causing the timeout from livekit/agents#5260 |
| Server on port 8000, agent worker outbound only | Hardcoded in `start.sh` |
| Never bake credentials into the image, `.env`, or Supabase | Single source of truth = host env vars |

---

## Cost (per minute, India)

| Service | ₹/min |
| --- | --- |
| Vobiz SIP | 1.00 |
| LiveKit Cloud | 0.17 |
| Gemini Live | 0.03 |
| **Total** | **≈ 1.20** |
