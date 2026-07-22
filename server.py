"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import json
import logging
import os
import random
import ssl
from pathlib import Path
from typing import Optional

import aiohttp
import certifi
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Patch SSL context with certifi BEFORE any TLS-using import.
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

# Optional .env loading for local dev only. In production (VPS / Coolify),
# real environment variables are the single source of truth and are NEVER
# overridden by a .env file or by the Supabase `settings` table.
if os.getenv("OUTBOUNDAI_LOAD_DOTENV", "").lower() == "true":
    try:
        from dotenv import load_dotenv
        load_dotenv(".env", override=False)
    except Exception:
        pass

from db import (
    cancel_appointment, clear_errors, create_agent_profile, create_campaign,
    delete_agent_profile, delete_campaign, get_agent_profile, get_all_agent_profiles,
    get_all_appointments, get_all_calls, get_all_campaigns, get_all_settings,
    get_calls_by_phone, get_campaign, get_contacts, get_logs, get_setting,
    get_stats, init_db, log_error, save_settings, set_default_agent_profile,
    set_setting, update_agent_profile, update_call_notes, update_campaign_run_stats,
    update_campaign_status, DB_WRITABLE_KEYS,
)
from prompts import DEFAULT_SYSTEM_PROMPT


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

init_db()

SUPPORTED_GEMINI_MODEL = "gemini-3.1-flash-live-preview"

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    logger.warning("APScheduler not installed — campaign scheduling disabled")


app = FastAPI(title="OutboundAI Dashboard", version="1.0.0")


@app.on_event("startup")
async def _startup():
    if _scheduler:
        _scheduler.start()
        await _reschedule_all_campaigns()


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


def eff(key: str) -> str:
    """
    Effective config value for credentials / infra: real env vars only.
    DB-writable UI keys (system_prompt, ENABLED_TOOLS) should call get_setting
    instead.
    """
    return os.getenv(key, "")


def _lk_session() -> aiohttp.ClientSession:
    ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))


def _normalize_agent_model(model: Optional[str]) -> str:
    requested = (model or "").strip()
    if not requested or requested == SUPPORTED_GEMINI_MODEL:
        return SUPPORTED_GEMINI_MODEL
    logger.warning(
        "Unsupported model override '%s' requested; forcing %s",
        requested,
        SUPPORTED_GEMINI_MODEL,
    )
    return SUPPORTED_GEMINI_MODEL


# ── Request models ────────────────────────────────────────────────────────────


class CallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = SUPPORTED_GEMINI_MODEL
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class SettingsRequest(BaseModel):
    settings: dict


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class StatusRequest(BaseModel):
    status: str


class WhatsAppSendRequest(BaseModel):
    to: str
    message: str
    channel_id: Optional[str] = None
    waba_id: Optional[str] = None


class WhatsAppTemplateRequest(BaseModel):
    to: str
    template_name: str = "magazine_subscription"
    language_code: str = "en_US"
    channel_id: Optional[str] = None
    waba_id: Optional[str] = None
    components: Optional[list] = None
    category: Optional[str] = None


class WhatsAppChannelRequest(BaseModel):
    waba_id: str
    phone_number_id: str
    phone_number: str
    access_token: str
    display_name: str
    number_onboarding_mode: str = "bring_your_own"
    number_provider: str = "meta_direct"
    number_order_id: Optional[str] = None


# ── Dashboard ─────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Dashboard not found — place index.html in ui/</h1>", status_code=404
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── Call dispatch ─────────────────────────────────────────────────────────────


@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    url = eff("LIVEKIT_URL")
    key = eff("LIVEKIT_API_KEY")
    secret = eff("LIVEKIT_API_SECRET")

    if not all([url, key, secret]):
        raise HTTPException(400, "LiveKit credentials not configured. Go to Settings → LiveKit.")

    phone = req.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "Phone must be in E.164 format: +919876543210")

    effective_prompt = req.system_prompt
    effective_voice = effective_model = effective_tools = None

    if req.agent_profile_id:
        profile = await get_agent_profile(req.agent_profile_id)
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")

    if not effective_prompt:
        effective_prompt = await get_setting("system_prompt", "") or None

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    metadata: dict = {
        "phone_number": phone,
        "lead_name": req.lead_name,
        "business_name": req.business_name,
        "service_type": req.service_type,
        "system_prompt": effective_prompt,
    }
    if effective_voice:
        metadata["voice_override"] = effective_voice
    if effective_model:
        metadata["model_override"] = effective_model
    if effective_tools:
        metadata["tools_override"] = effective_tools

    try:
        from livekit import api as lk_api
        session = _lk_session()
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        await lk.room.create_room(
            lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5)
        )
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=json.dumps(metadata),
            )
        )
        await lk.aclose()
        await session.close()
        await log_error("server", f"Call dispatched to {phone}", f"room={room_name}", "info")
        return {"status": "dispatched", "room": room_name, "phone": phone}
    except Exception as exc:
        logger.error("Dispatch error: %s", exc)
        raise HTTPException(500, f"Dispatch failed: {exc}")


# ── Calls / Stats ─────────────────────────────────────────────────────────────


@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


# ── Appointments ──────────────────────────────────────────────────────────────


@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


# ── Prompt ────────────────────────────────────────────────────────────────────


@app.get("/api/prompt")
async def api_get_prompt():
    saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    await set_setting("system_prompt", req.prompt)
    return {"status": "saved"}


@app.delete("/api/prompt")
async def api_reset_prompt():
    await set_setting("system_prompt", "")
    return {"status": "reset", "prompt": DEFAULT_SYSTEM_PROMPT}


# ── Settings ──────────────────────────────────────────────────────────────────


@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    """
    Persist DB-writable UI settings (system_prompt, ENABLED_TOOLS) only.
    Any credential / infra key supplied here is ignored — those must be set
    in the VPS environment.
    """
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    result = await save_settings(filtered)
    return {
        "status": "saved",
        "saved": result["saved"],
        "ignored": result["ignored"],
        "writable_keys": sorted(DB_WRITABLE_KEYS),
        "note": "Credentials are sourced from VPS env vars only. Any non-writable key was ignored.",
    }


# ── SIP trunk setup ───────────────────────────────────────────────────────────


@app.post("/api/setup/trunk")
async def api_setup_trunk():
    url = eff("LIVEKIT_URL")
    key = eff("LIVEKIT_API_KEY")
    secret = eff("LIVEKIT_API_SECRET")
    sip_domain = eff("VOBIZ_SIP_DOMAIN")
    username = eff("VOBIZ_USERNAME")
    password = eff("VOBIZ_PASSWORD")
    phone = eff("VOBIZ_OUTBOUND_NUMBER")

    if not all([url, key, secret, sip_domain, username, password, phone]):
        raise HTTPException(400, "Configure LiveKit and Vobiz credentials in Settings first.")

    try:
        from livekit import api as lk_api
        session = _lk_session()
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(
                trunk=lk_api.SIPOutboundTrunkInfo(
                    name="Vobiz Outbound Trunk",
                    address=sip_domain,
                    auth_username=username,
                    auth_password=password,
                    numbers=[phone],
                )
            )
        )
        trunk_id = trunk.sip_trunk_id
        await lk.aclose()
        await session.close()
        # NOTE: We do NOT persist the trunk_id anywhere automatically. The
        # operator must add OUTBOUND_TRUNK_ID=<trunk_id> to the VPS env vars
        # and redeploy so the worker picks it up. This keeps env vars the
        # single source of truth.
        return {
            "status": "created",
            "trunk_id": trunk_id,
            "action_required": (
                f"Add OUTBOUND_TRUNK_ID={trunk_id} to your VPS / Coolify "
                f"environment variables and redeploy the container."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")


# ── Logs ──────────────────────────────────────────────────────────────────────


@app.get("/api/logs")
async def api_get_logs(
    limit: int = 200, level: Optional[str] = None, source: Optional[str] = None
):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


# ── CRM ───────────────────────────────────────────────────────────────────────


@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}


@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}


# ── WhatsApp (Vobiz) ──────────────────────────────────────────────────────────


@app.post("/api/whatsapp/send")
async def api_whatsapp_send(req: WhatsAppSendRequest):
    """Send a WhatsApp text message via Vobiz."""
    auth_id = eff("VOBIZ_AUTH_ID")
    auth_token = eff("VOBIZ_AUTH_TOKEN")
    if not (auth_id and auth_token):
        raise HTTPException(400, "Vobiz WhatsApp not configured. Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")
    try:
        from vobiz_client import send_whatsapp_session, VobizError
        result = await send_whatsapp_session(
            to=req.to, body=req.message,
            channel_id=req.channel_id, waba_id=req.waba_id,
        )
        try:
            from db import log_whatsapp_message
            msg_type = result.get("type", "text")
            await log_whatsapp_message(
                phone_number=req.to,
                message_type=msg_type,
                content=req.message if msg_type == "text" else "template: magazine_subscription",
                vobiz_message_id=result.get("id", ""),
                status=result.get("status", "pending"),
            )
        except Exception:
            pass
        return {"status": "sent", "message": result}
    except VobizError as exc:
        raise HTTPException(exc.status_code, f"Vobiz API error: {exc.message}")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"WhatsApp send failed: {exc}")


@app.post("/api/whatsapp/send-template")
async def api_whatsapp_send_template(req: WhatsAppTemplateRequest):
    """Send a WhatsApp template message via Vobiz."""
    auth_id = eff("VOBIZ_AUTH_ID")
    auth_token = eff("VOBIZ_AUTH_TOKEN")
    if not (auth_id and auth_token):
        raise HTTPException(400, "Vobiz WhatsApp not configured. Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")
    try:
        from vobiz_client import send_template_message, VobizError
        result = await send_template_message(
            to=req.to,
            template_name=req.template_name,
            language_code=req.language_code,
            channel_id=req.channel_id,
            waba_id=req.waba_id,
            components=req.components,
            category=req.category,
        )
        try:
            from db import log_whatsapp_message
            await log_whatsapp_message(
                phone_number=req.to,
                message_type="template",
                content=req.template_name,
                vobiz_message_id=result.get("id", ""),
                status=result.get("status", "pending"),
            )
        except Exception:
            pass
        return {"status": "sent", "message": result}
    except VobizError as exc:
        raise HTTPException(exc.status_code, f"Vobiz API error: {exc.message}")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"WhatsApp template send failed: {exc}")


@app.post("/api/whatsapp/open-session")
async def api_whatsapp_open_session(req: WhatsAppTemplateRequest):
    """Open a 24-hour WhatsApp conversational session by sending the magazine_subscription template."""
    auth_id = eff("VOBIZ_AUTH_ID")
    auth_token = eff("VOBIZ_AUTH_TOKEN")
    if not (auth_id and auth_token):
        raise HTTPException(400, "Vobiz WhatsApp not configured. Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")
    try:
        from vobiz_client import open_whatsapp_session, VobizError
        result = await open_whatsapp_session(
            to=req.to,
            template_name=req.template_name,
            language_code=req.language_code,
            channel_id=req.channel_id,
            waba_id=req.waba_id,
            components=req.components,
        )
        try:
            from db import log_whatsapp_message
            await log_whatsapp_message(
                phone_number=req.to,
                message_type="template",
                content=f"session_open: {req.template_name}",
                vobiz_message_id=result.get("id", ""),
                status=result.get("status", "pending"),
            )
        except Exception:
            pass
        return {
            "status": "session_initiated",
            "message": result,
            "note": "24-hour window opens once the recipient replies to this template.",
        }
    except VobizError as exc:
        raise HTTPException(exc.status_code, f"Vobiz API error: {exc.message}")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Failed to open WhatsApp session: {exc}")


@app.get("/api/whatsapp/channels")
async def api_whatsapp_list_channels():
    """List all WhatsApp channels connected to the Vobiz account."""
    auth_id = eff("VOBIZ_AUTH_ID")
    auth_token = eff("VOBIZ_AUTH_TOKEN")
    if not (auth_id and auth_token):
        raise HTTPException(400, "Vobiz WhatsApp not configured. Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")
    try:
        from vobiz_client import list_channels, VobizError
        channels = await list_channels()
        return {"channels": channels}
    except VobizError as exc:
        raise HTTPException(exc.status_code, f"Vobiz API error: {exc.message}")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Failed to list channels: {exc}")


@app.post("/api/whatsapp/channels")
async def api_whatsapp_create_channel(req: WhatsAppChannelRequest):
    """Create a new WhatsApp Business channel on Vobiz."""
    auth_id = eff("VOBIZ_AUTH_ID")
    auth_token = eff("VOBIZ_AUTH_TOKEN")
    if not (auth_id and auth_token):
        raise HTTPException(400, "Vobiz WhatsApp not configured. Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")
    try:
        from vobiz_client import create_channel, VobizError
        channel = await create_channel(
            waba_id=req.waba_id,
            phone_number_id=req.phone_number_id,
            phone_number=req.phone_number,
            access_token=req.access_token,
            display_name=req.display_name,
            number_onboarding_mode=req.number_onboarding_mode,
            number_provider=req.number_provider,
            number_order_id=req.number_order_id,
        )
        return {"status": "created", "channel": channel}
    except VobizError as exc:
        raise HTTPException(exc.status_code, f"Vobiz API error: {exc.message}")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Channel creation failed: {exc}")


@app.delete("/api/whatsapp/channels/{channel_id}")
async def api_whatsapp_delete_channel(channel_id: str):
    """Delete a WhatsApp channel by its ID."""
    auth_id = eff("VOBIZ_AUTH_ID")
    auth_token = eff("VOBIZ_AUTH_TOKEN")
    if not (auth_id and auth_token):
        raise HTTPException(400, "Vobiz WhatsApp not configured. Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")
    try:
        from vobiz_client import delete_channel, VobizError
        result = await delete_channel(channel_id)
        return {"status": "deleted", "result": result}
    except VobizError as exc:
        raise HTTPException(exc.status_code, f"Vobiz API error: {exc.message}")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Channel deletion failed: {exc}")


@app.get("/api/whatsapp/messages")
async def api_whatsapp_messages(limit: int = 50):
    """Get recent WhatsApp message logs from the database."""
    try:
        from db import get_whatsapp_messages
        messages = await get_whatsapp_messages(limit=limit)
        return {"data": messages}
    except Exception as exc:
        raise HTTPException(500, f"Failed to fetch WhatsApp messages: {exc}")


# ── Agent Profiles ────────────────────────────────────────────────────────────


@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    try:
        return await get_all_agent_profiles()
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    try:
        profile_id = await create_agent_profile(
            name=req.name, voice=req.voice, model=_normalize_agent_model(req.model),
            system_prompt=req.system_prompt, enabled_tools=req.enabled_tools,
            is_default=req.is_default,
        )
        return {"status": "created", "id": profile_id}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(
        profile_id,
        {
            "name": req.name,
            "voice": req.voice,
            "model": _normalize_agent_model(req.model),
            "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools,
            "is_default": 1 if req.is_default else 0,
        },
    )
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    try:
        await set_default_agent_profile(profile_id)
        return {"status": "default set"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Campaigns ─────────────────────────────────────────────────────────────────


async def _dispatch_one(
    lk, lk_api, contact: dict, room_name: str,
    prompt: Optional[str], profile: Optional[dict] = None,
) -> bool:
    try:
        saved_prompt = prompt or (await get_setting("system_prompt", "")) or None
        metadata: dict = {
            "phone_number": contact["phone"],
            "lead_name": contact.get("lead_name", "there"),
            "business_name": contact.get("business_name", "our company"),
            "service_type": contact.get("service_type", "our service"),
            "system_prompt": saved_prompt,
        }
        if profile:
            if not metadata["system_prompt"] and profile.get("system_prompt"):
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):
                metadata["voice_override"] = profile["voice"]
            if profile.get("model"):
                metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"):
                metadata["tools_override"] = profile["enabled_tools"]

        # Create the room first (idempotent)
        try:
            await lk.room.create_room(
                lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5)
            )
        except Exception:
            pass

        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=json.dumps(metadata),
            )
        )
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc)
        return False


async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts:
        return
    delay = int(campaign.get("call_delay_seconds") or 3)
    prompt = campaign.get("system_prompt")
    profile = None
    if campaign.get("agent_profile_id"):
        profile = await get_agent_profile(campaign["agent_profile_id"])

    url = eff("LIVEKIT_URL")
    key = eff("LIVEKIT_API_KEY")
    secret = eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        logger.error("Campaign %s: LiveKit not configured", campaign_id)
        return

    from livekit import api as lk_api_module
    session = _lk_session()
    ok_count = fail_count = 0
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for i, contact in enumerate(contacts):
            phone = (contact.get("phone") or "").strip()
            if not phone.startswith("+"):
                fail_count += 1
                continue
            room_name = f"camp-{campaign_id[:8]}-{phone.replace('+','')}-{random.randint(100,999)}"
            success = await _dispatch_one(lk, lk_api_module, contact, room_name, prompt, profile)
            if success:
                ok_count += 1
            else:
                fail_count += 1
            if i < len(contacts) - 1:
                await asyncio.sleep(delay)
        await lk.aclose()
    except Exception as exc:
        logger.error("Campaign run error: %s", exc)
    finally:
        await session.close()

    await update_campaign_run_stats(campaign_id, ok_count, fail_count)
    logger.info("Campaign %s done — %d dispatched, %d failed", campaign_id, ok_count, fail_count)


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    try:
        campaigns = await get_all_campaigns()
        for c in campaigns:
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c["id"], c["schedule_type"], c.get("schedule_time", "09:00"))
    except Exception as exc:
        logger.warning("Could not reschedule campaigns: %s", exc)


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    if schedule_type == "daily":
        trigger = CronTrigger(hour=hour, minute=minute)
    else:
        trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(
        _run_campaign, trigger=trigger, args=[campaign_id],
        id=job_id, replace_existing=True,
    )
    logger.info("Scheduled campaign %s (%s at %02d:%02d)", campaign_id, schedule_type, hour, minute)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")

    campaign_id = await create_campaign(
        name=req.name, contacts_json=json.dumps(req.contacts),
        schedule_type=req.schedule_type, schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds,
        system_prompt=req.system_prompt, agent_profile_id=req.agent_profile_id,
    )
    campaign = await get_campaign(campaign_id)

    if req.schedule_type == "once":
        asyncio.create_task(_run_campaign(campaign_id))
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)

    return {"status": "created", "campaign_id": campaign_id, "campaign": campaign}


@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    campaign = await get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "dispatching", "campaign_id": campaign_id}


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if req.status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be: active | paused | completed")
    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if req.status == "paused" and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(
                campaign_id, campaign["schedule_type"],
                campaign.get("schedule_time", "09:00"),
            )
    return {"status": req.status}
