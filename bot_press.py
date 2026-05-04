"""
Chatwoot ↔ Botpress Bridge  —  Chat API edition
================================================

Flow (synchronous, no ngrok, no callback URL needed):
  1. Customer sends message → Chatwoot → POST /webhook  (this server)
  2. This server → Botpress Chat API  (chat.botpress.cloud)
       • First message from a conversation → create Botpress user + conversation
       • Subsequent messages → reuse the same user + conversation
  3. This server polls Botpress for the bot's reply
  4. Reply is injected into the same Chatwoot thread via Chatwoot API

Authentication:
  • Botpress Chat API uses x-user-key (obtained by calling POST /users once).
    Your Personal Access Token / API key is NOT needed for chat operations.
  • Chatwoot API uses api_access_token (your bot agent token).

─── .env ────────────────────────────────────────────────────────────────────
CHATWOOT_BASE_URL=http://localhost:3000
CHATWOOT_BOT_TOKEN=<your-chatwoot-bot-agent-token>

# Open your webchat configUrl in a browser → copy the "clientId" value
# e.g. https://files.bpcontent.cloud/.../20260426041718-RA5PS4UY.json
#      → { "clientId": "9161c8f4-5692-4783-a505-918316c3106d", ... }
BOTPRESS_BOT_ID=9161c8f4-5692-4783-a505-918316c3106d

─── Run ─────────────────────────────────────────────────────────────────────
  uvicorn bot_press:app --host 0.0.0.0 --port 8000 --reload

Chatwoot outgoing webhook → http://host.docker.internal:8000/webhook
"""

import asyncio
import hashlib
import hmac
import httpx
import logging
import os
import time
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

CHATWOOT_BASE_URL       = os.getenv("CHATWOOT_BASE_URL",       "http://localhost:3000")
CHATWOOT_BOT_TOKEN      = os.getenv("CHATWOOT_BOT_TOKEN",      "")
CHATWOOT_WEBHOOK_SECRET = os.getenv("CHATWOOT_WEBHOOK_SECRET", "KeWe2baZqdYPrJZLFZeqVmwW")

# Optional: auto-assign escalated conversations to a specific agent or team.
# Set one of these in .env — agent takes priority over team.
# Leave both empty to rely on Chatwoot's own auto-assignment rules.
CHATWOOT_AGENT_ID = os.getenv("CHATWOOT_AGENT_ID", "")  # numeric agent ID
CHATWOOT_TEAM_ID = os.getenv("CHATWOOT_TEAM_ID", "")  # numeric team ID

# Your Botpress clientId — from the webchat configUrl JSON → "clientId" field
# e.g. https://files.bpcontent.cloud/.../20260426041718-RA5PS4UY.json → open it → copy "clientId"
BOTPRESS_BOT_ID = os.getenv("BOTPRESS_BOT_ID", "9161c8f4-5692-4783-a505-918316c3106d")
BOTPRESS_CHAT_URL = f"https://chat.botpress.cloud/{BOTPRESS_BOT_ID}"

# How long to wait for a bot reply before giving up (seconds)
REPLY_TIMEOUT_S = int(os.getenv("REPLY_TIMEOUT_S", "20"))
# How often to poll for a reply (seconds)
REPLY_POLL_S = float(os.getenv("REPLY_POLL_S", "1.5"))

ESCALATION_PHRASES = [
    "human", "agent", "real person", "speak to someone",
    "talk to someone", "representative", "supervisor", "person please",
]

# ─── In-memory state ─────────────────────────────────────────────────────────
# Bot sessions: chatwoot_conv_id → { "user_key", "user_id", "conv_id" }
_sessions: dict[int, dict] = {}
# Escalated conversations: bot ignores all incoming messages for these conv IDs
# until the conversation is resolved/reopened and the entry is removed.
_escalated: set[int] = set()
# Per-conversation locks: ensures only one message per conversation is processed
# at a time, preventing double-replies when Chatwoot fires duplicate events.
_conv_locks: dict[int, asyncio.Lock] = {}
# Recently processed message IDs (content-hash dedup for duplicate webhook events)
_recent_msg_ids: dict[str, float] = {}   # msg_fingerprint → timestamp
_MSG_DEDUP_TTL = 10.0                    # seconds to remember a processed message


def _conv_lock(conv_id: int) -> asyncio.Lock:
    if conv_id not in _conv_locks:
        _conv_locks[conv_id] = asyncio.Lock()
    return _conv_locks[conv_id]


def _dedup_key(conv_id: int, content: str) -> str:
    return f"{conv_id}:{content}"


def _is_duplicate(conv_id: int, content: str) -> bool:
    """Return True if we already processed this exact message recently."""
    key = _dedup_key(conv_id, content)
    now = time.monotonic()
    # Purge expired entries
    expired = [k for k, t in _recent_msg_ids.items() if now - t > _MSG_DEDUP_TTL]
    for k in expired:
        del _recent_msg_ids[k]
    if key in _recent_msg_ids:
        return True
    _recent_msg_ids[key] = now
    return False

app = FastAPI(title="Chatwoot ↔ Botpress Bridge")


# ─── Botpress Chat API helpers ────────────────────────────────────────────────

async def _get_or_create_session(cw_conv_id: int) -> dict:
    """
    Return existing Botpress (user_key, conv_id) for this Chatwoot conversation,
    or create a fresh user + conversation on first contact.
    """
    if cw_conv_id in _sessions:
        log.info(f"Conv {cw_conv_id}: reusing session {_sessions[cw_conv_id]['conv_id']}")
        return _sessions[cw_conv_id]

    async with httpx.AsyncClient(timeout=10) as client:

        # 1. Create an anonymous Botpress user → returns { id, key, ... }
        user_r = await client.post(
            f"{BOTPRESS_CHAT_URL}/users",
            headers={"Content-Type": "application/json"},
            json={},
        )
        log.info(f"POST /users → {user_r.status_code} {user_r.text[:300]}")
        user_r.raise_for_status()
        user_data = user_r.json()
        user_key = (
                user_data.get("key")
                or user_data.get("user", {}).get("key")
                or ""
        )
        user_id = (
                user_data.get("user", {}).get("id")
                or user_data.get("id")
                or ""
        )
        if not user_key:
            raise ValueError(f"createUser returned no key: {user_data}")
        log.info(f"Conv {cw_conv_id}: created Botpress user id={user_id}")

        # 2. Create a conversation for that user → returns { id, ... }
        conv_r = await client.post(
            f"{BOTPRESS_CHAT_URL}/conversations",
            headers={"x-user-key": user_key, "Content-Type": "application/json"},
            json={},
        )
        log.info(f"POST /conversations → {conv_r.status_code} {conv_r.text[:300]}")
        conv_r.raise_for_status()
        conv_data = conv_r.json()
        bp_conv_id = (
                conv_data.get("id")
                or conv_data.get("conversation", {}).get("id")
                or ""
        )
        if not bp_conv_id:
            raise ValueError(f"createConversation returned no id: {conv_data}")
        log.info(f"Conv {cw_conv_id}: created Botpress conversation {bp_conv_id}")

    session = {"user_key": user_key, "user_id": user_id, "conv_id": bp_conv_id}
    _sessions[cw_conv_id] = session
    return session


async def _send_and_wait(user_key: str, user_id: str, bp_conv_id: str, text: str) -> str:
    """
    POST a message to Botpress, then poll until the bot replies.
    Returns the bot's reply text, or "" on timeout.
    """
    headers = {"x-user-key": user_key}

    async with httpx.AsyncClient(timeout=10) as client:

        # Send the user's message
        send_r = await client.post(
            f"{BOTPRESS_CHAT_URL}/messages",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "conversationId": bp_conv_id,
                "payload": {
                    "type": "text",
                    "text": text,
                },
            },
        )
        log.info(f"POST /messages → {send_r.status_code} {send_r.text[:300]}")
        send_r.raise_for_status()
        send_data = send_r.json()
        sent_id = (
                send_data.get("id")
                or send_data.get("message", {}).get("id")
                or ""
        )
        log.info(f"Message sent to Botpress, id={sent_id}")

        # Poll for the bot's reply
        deadline = time.monotonic() + REPLY_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(REPLY_POLL_S)

            msgs_r = await client.get(
                f"{BOTPRESS_CHAT_URL}/conversations/{bp_conv_id}/messages",
                headers=headers,
            )
            log.info(f"GET /messages → {msgs_r.status_code} {msgs_r.text[:400]}")
            msgs_r.raise_for_status()
            raw = msgs_r.json()
            # Messages may be under "messages" key or be the list itself
            messages = raw if isinstance(raw, list) else raw.get("messages", [])

            # Walk newest-first; the first message whose userId differs from
            # our user is the bot's reply.
            for msg in messages:
                if msg.get("id") == sent_id:
                    break  # reached our own message — no reply above it yet

                msg_user_id = msg.get("userId", "")
                if msg_user_id and msg_user_id != user_id:
                    payload = msg.get("payload") or {}
                    reply_text = payload.get("text") or msg.get("text") or ""
                    if reply_text:
                        log.info(f"Bot replied: {reply_text[:100]!r}")
                        return reply_text

            log.debug("No bot reply yet, polling again…")

    log.warning(f"Bot reply timed out after {REPLY_TIMEOUT_S}s")
    return ""


# ─── Chatwoot helpers ─────────────────────────────────────────────────────────

async def _send_chatwoot_message(account_id: int, conv_id: int, content: str):
    url = (
        f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}"
        f"/conversations/{conv_id}/messages"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            url,
            json={"content": content, "message_type": "outgoing", "private": False},
            headers={"api_access_token": CHATWOOT_BOT_TOKEN},
        )
        log.info(f"Chatwoot send → {r.status_code}")
        return r


async def _handoff_to_human(account_id: int, conv_id: int):
    """
    1. Set conversation status to 'open' so it appears in the agent queue.
    2. Assign to a specific agent or team if configured.
    3. Mark the conversation as escalated so the bot stops responding.
    """
    headers = {"api_access_token": CHATWOOT_BOT_TOKEN}

    async with httpx.AsyncClient(timeout=10) as client:
        # Set status → open (makes it visible in agent inbox)
        r = await client.patch(
            f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}/conversations/{conv_id}",
            json={"status": "open"},
            headers=headers,
        )
        log.info(f"Chatwoot set open → {r.status_code}")

        # Assign to agent or team
        assign_url = (
            f"{CHATWOOT_BASE_URL}/api/v1/accounts/{account_id}"
            f"/conversations/{conv_id}/assignments"
        )
        if CHATWOOT_AGENT_ID:
            ar = await client.post(
                assign_url,
                json={"assignee_id": int(CHATWOOT_AGENT_ID)},
                headers=headers,
            )
            log.info(f"Chatwoot assign agent {CHATWOOT_AGENT_ID} → {ar.status_code}")
        elif CHATWOOT_TEAM_ID:
            ar = await client.post(
                assign_url,
                json={"team_id": int(CHATWOOT_TEAM_ID)},
                headers=headers,
            )
            log.info(f"Chatwoot assign team {CHATWOOT_TEAM_ID} → {ar.status_code}")
        else:
            log.info("No CHATWOOT_AGENT_ID / CHATWOOT_TEAM_ID set — relying on Chatwoot auto-assignment")

    # Stop the bot from responding to this conversation
    _escalated.add(conv_id)
    _sessions.pop(conv_id, None)
    log.info(f"Conv {conv_id}: marked as escalated — bot will ignore future messages")


def _wants_human(text: str) -> bool:
    return any(phrase in text.lower() for phrase in ESCALATION_PHRASES)


def _verify_chatwoot_signature(body: bytes, signature_header: str) -> bool:
    """
    Chatwoot signs webhook payloads with HMAC-SHA256 using the webhook secret.
    The header value is the raw hex digest (no 'sha256=' prefix).
    """
    if not CHATWOOT_WEBHOOK_SECRET:
        return True  # secret not configured — skip verification
    expected = hmac.new(
        CHATWOOT_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ─── /webhook — Chatwoot → this server ───────────────────────────────────────

@app.post("/webhook")
async def chatwoot_webhook(request: Request):
    body = await request.body()

    # Signature verification
    # Chatwoot sends:  X-Chatwoot-Signature: sha256=<hex>
    # Set WEBHOOK_STRICT_SIGNATURE=true in .env to reject mismatches (off by default
    # until you confirm the correct HMAC token from Chatwoot's database).
    if CHATWOOT_WEBHOOK_SECRET:
        raw_sig  = request.headers.get("X-Chatwoot-Signature", "")
        received = raw_sig.removeprefix("sha256=")
        expected = hmac.new(
            CHATWOOT_WEBHOOK_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        if received and hmac.compare_digest(expected, received):
            log.debug("Webhook signature OK")
        else:
            if os.getenv("WEBHOOK_STRICT_SIGNATURE", "").lower() == "true":
                log.warning(f"Signature mismatch — rejecting (received={received!r} expected={expected!r})")
                raise HTTPException(status_code=401, detail="Invalid signature")
            else:
                log.warning(f"Signature mismatch — passing through (set WEBHOOK_STRICT_SIGNATURE=true to enforce)")

    import json
    data       = json.loads(body)
    event      = data.get("event", "")
    msg_type   = data.get("message_type", "")
    content    = (data.get("content") or "").strip()
    account_id = (data.get("account") or {}).get("id")

    # conv_id location differs by event type:
    #   message_created / conversation_status_changed → data["conversation"]["id"]
    #   conversation_resolved                         → data["id"]  (top-level)
    conv_id = (data.get("conversation") or {}).get("id") or (
        data.get("id") if event in (
            "conversation_resolved",
            "conversation_created",
            "conversation_updated",
        ) else None
    )

    log.info(f"Webhook: event={event} type={msg_type} conv={conv_id} msg={content[:80]!r}")

    # ── Human → AI handoff on conversation resolve ────────────────────────────
    # When an agent resolves the conversation, clear the escalation flag so the
    # bot takes over again when the customer next sends a message.
    is_resolved = (
        event == "conversation_resolved"
        or (
            event == "conversation_status_changed"
            and (
                data.get("current_status") == "resolved"
                or (data.get("conversation") or {}).get("status") == "resolved"
            )
        )
    )
    if is_resolved:
        if conv_id:
            _escalated.discard(conv_id)
            _sessions.pop(conv_id, None)   # fresh Botpress session on next contact
            log.info(f"Conv {conv_id}: resolved by agent — bot re-engaged for next message")
        else:
            log.warning(f"Resolve event received but could not extract conv_id — raw keys: {list(data.keys())}")
        return JSONResponse({"status": "bot_reengaged"})

    # Only process real incoming customer messages — ignore everything else
    # to prevent loops (bot echoes, agent messages, status events, etc.)
    if event != "message_created" or msg_type != "incoming":
        return JSONResponse({"status": "ignored"})

    if not content or not account_id or not conv_id:
        return JSONResponse({"status": "missing_data"})

    # ── Dedup: drop if we already started processing this exact message ────────
    # Chatwoot sometimes fires two identical webhook events for the same message
    # (even with a single webhook configured). The dedup cache prevents the second
    # event from spawning a parallel Botpress request.
    if _is_duplicate(conv_id, content):
        log.info(f"Conv {conv_id}: duplicate event dropped (same content within {_MSG_DEDUP_TTL}s)")
        return JSONResponse({"status": "duplicate"})

    # ── Per-conversation lock: serialize processing ────────────────────────────
    # Ensures that even if two events slip through the dedup check (e.g. slightly
    # different whitespace), only one is processed at a time per conversation.
    async with _conv_lock(conv_id):

        # If this conversation has been handed off to a human agent, do nothing —
        # let the agent handle it. The bot re-engages only if the conversation is
        # resolved and a new session starts (entry removed from _escalated).
        if conv_id in _escalated:
            log.info(f"Conv {conv_id}: human is handling — bot ignoring message")
            return JSONResponse({"status": "human_handling"})

        # Human escalation shortcut
        if _wants_human(content):
            log.info(f"Conv {conv_id}: escalation keyword")
            await _send_chatwoot_message(
                account_id, conv_id,
                "Absolutely! Let me connect you with one of our agents. Please hold on. 🙏",
            )
            await _handoff_to_human(account_id, conv_id)
            return JSONResponse({"status": "escalated"})

        # Get or create the Botpress session for this Chatwoot conversation
        try:
            session = await _get_or_create_session(conv_id)
        except Exception as e:
            log.error(f"Conv {conv_id}: session error — {e}")
            await _send_chatwoot_message(
                account_id, conv_id,
                "I'm having trouble reaching the assistant. Let me get a human agent for you.",
            )
            await _handoff_to_human(account_id, conv_id)
            return JSONResponse({"status": "session_error", "detail": str(e)})

        # Send message to Botpress and wait for reply
        try:
            reply = await _send_and_wait(session["user_key"], session["user_id"], session["conv_id"], content)
        except Exception as e:
            log.error(f"Conv {conv_id}: Botpress error — {e}")
            _sessions.pop(conv_id, None)  # drop bad session so next attempt starts fresh
            await _send_chatwoot_message(
                account_id, conv_id,
                "I'm having trouble reaching the assistant. Let me get a human agent for you.",
            )
            await _handoff_to_human(account_id, conv_id)
            return JSONResponse({"status": "botpress_error", "detail": str(e)})

        if not reply:
            await _send_chatwoot_message(
                account_id, conv_id,
                "I didn't receive a response in time. Let me connect you with a human agent.",
            )
            await _handoff_to_human(account_id, conv_id)
            return JSONResponse({"status": "timeout"})

        # AI-triggered escalation signal
        if "[ESCALATE]" in reply:
            clean = reply.replace("[ESCALATE]", "").strip()
            await _send_chatwoot_message(account_id, conv_id, clean)
            await _handoff_to_human(account_id, conv_id)
            return JSONResponse({"status": "ai_escalated"})

        # Deliver reply to Chatwoot
        await _send_chatwoot_message(account_id, conv_id, reply)
        log.info(f"Conv {conv_id}: reply delivered ✓")
        return JSONResponse({"status": "ok"})


# ─── /health ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "botpress_url": BOTPRESS_CHAT_URL,
        "chatwoot": CHATWOOT_BASE_URL,
        "active_sessions": list(_sessions.keys()),
        "escalated": list(_escalated),
        "reply_timeout_s": REPLY_TIMEOUT_S,
    }


@app.get("/sessions")
async def list_sessions():
    return {
        "sessions": {k: {"conv_id": v["conv_id"]} for k, v in _sessions.items()},
        "escalated": list(_escalated),
    }


@app.delete("/sessions/{conv_id}")
async def clear_session(conv_id: int):
    """Remove bot session AND escalation flag — re-engages the bot for this conversation."""
    removed_session = _sessions.pop(conv_id, None)
    removed_escalated = conv_id in _escalated
    _escalated.discard(conv_id)
    return {"conv_id": conv_id, "session_removed": bool(removed_session), "escalation_cleared": removed_escalated}
