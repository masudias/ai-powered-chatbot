# Chatwoot ↔ Botpress Bridge

A Python bridge that connects a **Chatwoot** live-chat inbox to a **Botpress** AI agent. Customer messages flow from Chatwoot → Botpress (via the Chat API), the bot's reply is polled back, and delivered into the same Chatwoot thread. Supports AI→Human escalation and Human→AI handoff on conversation resolve.

---

## Architecture

```
Customer (web widget)
        │
        ▼
  Chatwoot (port 3000)
        │  outgoing webhook
        ▼
  Bridge server — bot_press.py  (port 8000)
        │  Botpress Chat API
        ▼
  Botpress Cloud (chat.botpress.cloud)
        │  bot reply (polled)
        ▼
  Bridge server injects reply
        │  Chatwoot REST API
        ▼
  Chatwoot conversation thread
```

---

## 1. Chatwoot — Local Installation (Docker)

### Prerequisites
- Docker Desktop installed and running
- `git`, `psql` available (or Postgres running locally)

### Clone & configure

```bash
git clone https://github.com/chatwoot/chatwoot.git
cd chatwoot
cp .env.example .env
```

Edit `.env` — minimum required changes:

```env
SECRET_KEY_BASE=<generate with: openssl rand -hex 64>
FRONTEND_URL=http://0.0.0.0:3000

# Postgres (if using host Postgres instead of Docker)
POSTGRES_HOST=host.docker.internal
POSTGRES_USERNAME=postgres
POSTGRES_PASSWORD=your_password
RAILS_ENV=development

# Redis
REDIS_URL=redis://redis:6379
```

### Start with Docker Compose

```bash
docker compose up -d
```

Services started: `rails`, `sidekiq`, `redis`, `postgres` (if included).

### First-time database setup

```bash
docker compose run --rm rails bundle exec rails db:chatwoot_prepare
```

### Access

Open `http://localhost:3000` → complete the onboarding wizard (create admin account, workspace name).

---

## 2. Chatwoot — Required Configuration

### 2a. Create a Website Inbox

`Settings → Inboxes → Add Inbox → Website`

- **Channel Name:** anything (e.g. "AI Support")
- **Website Domain:** your domain or `localhost`
- Complete the wizard. Copy the **Inbox ID** shown at the end (you'll see it in the URL: `/app/accounts/1/settings/inboxes/<ID>/`).

### 2b. Create a Bot Agent (for the API token)

`Settings → Agents → Invite Agent`

- Role: **Agent**
- This agent is used by the bridge to post bot replies. After creation, go to `Profile → Access Token` to copy the **API Access Token**.

This becomes `CHATWOOT_BOT_TOKEN` in your `.env`.

### 2c. Create a Team (optional, for escalation routing)

`Settings → Teams → Create new team`

Add the human agents who should receive escalated conversations. Note the **Team ID** (visible in the URL after creation).

This becomes `CHATWOOT_TEAM_ID` in your `.env`.

### 2d. Register the Outgoing Webhook

`Settings → Integrations → Webhooks → Add new webhook`

| Field | Value |
|-------|-------|
| **Webhook Name** | Botpress Bridge AI Bot |
| **URL** | `http://host.docker.internal:8000/webhook` |
| **Subscribed Events** | ✅ Message Created, ✅ Conversation Status Changed, ✅ Conversation Resolved, ✅ Conversation Updated |
| **Secret** | any string — copy it, it becomes `CHATWOOT_WEBHOOK_SECRET` |

> **Note:** `host.docker.internal` resolves to your Mac from inside Docker. If running the bridge outside Docker, use your machine's LAN IP instead.

> **Important:** Register **only one webhook**. Chatwoot may already fire duplicate events; a second webhook doubles every call and causes double bot replies.

---

## 3. Botpress — Setup

### 3a. Publish your bot

In [Botpress Studio](https://app.botpress.cloud), build and **publish** your bot.

### 3b. Find your `clientId`

1. Go to your bot's **Webchat** integration or **Share** tab.
2. Click the config URL (e.g. `https://files.bpcontent.cloud/.../20260426041718-XXXXXXXX.json`).
3. Open it in a browser — find the `"clientId"` field:
   ```json
   {
     "botId": "0515aa63-...",
     "clientId": "9161c8f4-5692-4783-a505-918316c3106d"
   }
   ```
4. Copy `clientId` → this becomes `BOTPRESS_BOT_ID` in your `.env`.

> The `clientId` (not `botId`) is what the Chat API uses. The Chat API base URL is:
> `https://chat.botpress.cloud/{clientId}`

### 3c. No additional Botpress configuration needed

The bridge uses the **anonymous Chat API** — it creates a Botpress user and conversation automatically per Chatwoot conversation. No API key or webhook URL needs to be configured in Botpress Studio.

---

## 4. Bridge Server — Setup

### 4a. Python environment

```bash
cd /path/to/Priyo   # the folder containing bot_press.py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4b. Create `.env`

```env
# Chatwoot
CHATWOOT_BASE_URL=http://localhost:3000
CHATWOOT_BOT_TOKEN=<bot agent API access token from step 2b>
CHATWOOT_WEBHOOK_SECRET=<webhook secret from step 2d>

# Escalation routing — set one (agent takes priority over team)
CHATWOOT_AGENT_ID=       # numeric agent ID (leave blank to use team)
CHATWOOT_TEAM_ID=1       # numeric team ID from step 2c

# Botpress
BOTPRESS_BOT_ID=9161c8f4-5692-4783-a505-918316c3106d   # clientId from step 3b

# Tuning (optional)
REPLY_TIMEOUT_S=20       # seconds to wait for bot reply before timing out
REPLY_POLL_S=1.5         # polling interval in seconds

# Signature enforcement (optional)
# Set to true once CHATWOOT_WEBHOOK_SECRET is confirmed correct
WEBHOOK_STRICT_SIGNATURE=false
```

### 4c. Run the server

```bash
uvicorn bot_press:app --host 0.0.0.0 --port 8000 --reload
```

The `--reload` flag auto-restarts on code changes (remove in production).

---

### 4e. Run the chat widget in localhost browser

```bash
python3 -m http.server 8080
```

Then open `http://localhost:8080/chatwoot.html` for accessing the widget. 

### Make sure the Chatwoot server is running inside the Docker environment

## 5. How It Works

### Normal flow
1. Customer sends a message → Chatwoot fires `message_created` webhook to `POST /webhook`.
2. Bridge creates a Botpress anonymous user + conversation (first message only; reused after that).
3. Bridge posts the message to Botpress Chat API and polls for the bot's reply.
4. Reply is injected into the Chatwoot thread as an outgoing message.

### AI → Human escalation
- If the customer's message contains words like *"human"*, *"agent"*, *"real person"*, etc., the bridge sends a holding message and calls `_handoff_to_human`.
- If the bot's reply contains `[ESCALATE]`, same handoff happens.
- Handoff: sets conversation status to `open`, assigns to the configured agent or team, and stops the bot from responding to that conversation.

### Human → AI handoff
- When an agent **resolves** the conversation in Chatwoot, Chatwoot fires `conversation_resolved`.
- The bridge clears the escalation flag and the Botpress session — the bot re-engages fresh when the customer next writes.

### Duplicate event protection
- **Dedup cache:** the same `(conv_id, message_content)` pair is dropped within a 10-second window.
- **Per-conversation lock:** even if two events slip through, only one processes at a time per conversation.

---

## 6. Webhook Signature Notes

Chatwoot sends `X-Chatwoot-Signature: sha256=<hex>` with each webhook.

The bridge verifies it using `CHATWOOT_WEBHOOK_SECRET`. By default, mismatches only produce a warning — the request is still processed. To enforce strict rejection:

```env
WEBHOOK_STRICT_SIGNATURE=true
```

> If you see "Signature mismatch" in logs, Chatwoot's internal `hmac_token` (stored in its database) may differ from the secret shown in the UI. Leave `WEBHOOK_STRICT_SIGNATURE=false` unless you can confirm they match.

---

## 7. Useful Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhook` | POST | Receives Chatwoot events |
| `/health` | GET | Server status, active sessions, escalated convs |
| `/sessions` | GET | Lists all active Botpress sessions |
| `/sessions/{conv_id}` | DELETE | Clears a session + escalation flag (re-engages bot) |

Example:
```bash
curl http://localhost:8000/health
curl -X DELETE http://localhost:8000/sessions/3
```

---

## 8. Running Tests

Integration tests use real HTTP calls (no mocks). Set `TEST_CONV_ID` to an existing Chatwoot conversation ID before running:

```bash
TEST_ACCOUNT_ID=1 TEST_CONV_ID=3 pytest bot_press_test.py -v
```

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Bot replies twice | Duplicate webhook registered in Chatwoot | Delete the extra webhook under Settings → Integrations → Webhooks |
| Bot ignores messages after resolve | `conversation_resolved` event not clearing escalation | Check logs for "resolved by agent" line; restart server if session stuck |
| "Signature mismatch" in logs | Chatwoot hmac_token differs from UI secret | Leave `WEBHOOK_STRICT_SIGNATURE=false` |
| Bot reply timeout | Botpress bot not published, or `BOTPRESS_BOT_ID` is wrong | Confirm bot is published; verify `clientId` (not `botId`) from the config JSON |
| 400 on Botpress API | Wrong `clientId` (using botId or configUrl filename) | Re-read step 3b; use the value in `"clientId"` field of the JSON |
| Chatwoot not receiving webhook | Bridge not reachable from Docker | Use `host.docker.internal:8000` not `localhost:8000` in the webhook URL |
