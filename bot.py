"""
Chatwoot Agent Bot — OpenAI powered, with escalation to human agent.

Setup:
  pip install fastapi uvicorn openai httpx python-dotenv

Run:
  uvicorn bot:app --host 0.0.0.0 --port 8000 --reload

Chatwoot Outgoing URL:
  http://host.docker.internal:8000/webhook
"""

import httpx
import logging
import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

CHATWOOT_BASE_URL = os.getenv("CHATWOOT_BASE_URL", "http://localhost:3000")
CHATWOOT_BOT_TOKEN = os.getenv("CHATWOOT_BOT_TOKEN", "")  # Agent Bot Access Token from super_admin
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", (
    "You are a helpful and friendly customer support assistant for Priyo. "
    "Be concise, warm, and professional. "
    "If you cannot help with something or the user asks for a human, "
    "say you'll connect them with an agent and end your reply with exactly: [ESCALATE]"
))

ESCALATION_PHRASES = [
    "human", "agent", "real person", "speak to someone",
    "talk to someone", "representative", "supervisor", "person please"
]

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# In-memory conversation history per conversation_id
conversation_history: dict[int, list[dict]] = {}

app = FastAPI(title="Chatwoot AI Bot")


# ─── Chatwoot API helpers ─────────────────────────────────────────────────────

async def send_message(account_id: int, conversation_id: int, content: str):
    """Send a reply message to a Chatwoot conversation."""
    url = f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": CHATWOOT_BOT_TOKEN}
    payload = {"content": content, "message_type": "outgoing", "private": False}
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
        log.info(f"send_message → {r.status_code}")
        return r


async def handoff_to_human(account_id: int, conversation_id: int):
    """Remove the bot and set conversation to open so a human agent can pick it up."""
    url = f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/conversations/{conversation_id}/update"
    headers = {"api_access_token": CHATWOOT_BOT_TOKEN}
    # Setting meta.assignee_id to None removes the bot assignment
    payload = {"status": "open"}
    async with httpx.AsyncClient() as client:
        r = await client.patch(url, json=payload, headers=headers)
        log.info(f"handoff_to_human → {r.status_code}")
        return r


# ─── AI logic ─────────────────────────────────────────────────────────────────

def wants_human(message: str) -> bool:
    msg = message.lower()
    return any(phrase in msg for phrase in ESCALATION_PHRASES)


def get_ai_reply(conversation_id: int, user_message: str) -> str:
    """Build conversation history and get a reply from OpenAI."""
    history = conversation_history.setdefault(conversation_id, [])
    history.append({"role": "user", "content": user_message})

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    response = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        max_tokens=500,
        temperature=0.7,
    )

    reply = response.choices[0].message.content.strip()
    history.append({"role": "assistant", "content": reply})

    # Keep history to last 20 messages to avoid token bloat
    if len(history) > 20:
        conversation_history[conversation_id] = history[-20:]

    return reply


# ─── Webhook endpoint ─────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    event = data.get("event", "")
    msg_type = data.get("message_type", "")
    content = data.get("content", "").strip()
    account_id = data.get("account", {}).get("id")
    conv_id = data.get("conversation", {}).get("id")

    log.info(f"Event: {event} | Type: {msg_type} | Conv: {conv_id} | Msg: {content[:60]}")

    # Only handle new incoming messages from visitors
    if event != "message_created" or msg_type != "incoming":
        return JSONResponse({"status": "ignored"})

    if not content or not account_id or not conv_id:
        return JSONResponse({"status": "missing_data"})

    # ── Escalation check (keyword-based) ─────────────────────────────────────
    if wants_human(content):
        log.info(f"Conv {conv_id}: keyword escalation triggered")
        await send_message(account_id, conv_id,
                           "Absolutely! Let me connect you with one of our agents right away. "
                           "Please hold on a moment. 🙏")
        await handoff_to_human(account_id, conv_id)
        conversation_history.pop(conv_id, None)
        return JSONResponse({"status": "escalated"})

    # ── AI reply ──────────────────────────────────────────────────────────────
    try:
        reply = get_ai_reply(conv_id, content)
    except Exception as e:
        log.error(f"OpenAI error: {e}")
        await send_message(account_id, conv_id,
                           "I'm having a little trouble right now. Let me get a human agent for you.")
        await handoff_to_human(account_id, conv_id)
        return JSONResponse({"status": "openai_error", "detail": str(e)})

    # ── AI-triggered escalation ([ESCALATE] tag) ──────────────────────────────
    if "[ESCALATE]" in reply:
        clean_reply = reply.replace("[ESCALATE]", "").strip()
        log.info(f"Conv {conv_id}: AI-triggered escalation")
        await send_message(account_id, conv_id, clean_reply)
        await handoff_to_human(account_id, conv_id)
        conversation_history.pop(conv_id, None)
        return JSONResponse({"status": "ai_escalated"})

    await send_message(account_id, conv_id, reply)
    return JSONResponse({"status": "ok"})


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "model": OPENAI_MODEL, "chatwoot": CHATWOOT_BASE_URL}
