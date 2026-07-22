"""Async client for the Vobiz WhatsApp Messaging API.

Docs: https://vobiz.ai/docs/whatsapp/api
Base URL: https://api.vobiz.ai/api/v1/messaging
Auth: X-Auth-ID + X-Auth-Token headers (or Authorization: Bearer <token>)
"""

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("vobiz-client")

VOBIZ_BASE_URL = "https://api.vobiz.ai/api/v1/messaging"

DEFAULT_TEMPLATE_NAME = "magazine_subscription"


def _auth_headers() -> dict:
    """Build auth headers from env vars."""
    auth_id = os.getenv("VOBIZ_AUTH_ID", "")
    auth_token = os.getenv("VOBIZ_AUTH_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if auth_id and auth_token:
        headers["X-Auth-ID"] = auth_id
        headers["X-Auth-Token"] = auth_token
    elif auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


def _is_configured() -> bool:
    return bool(os.getenv("VOBIZ_AUTH_ID", "") and os.getenv("VOBIZ_AUTH_TOKEN", ""))


class VobizError(Exception):
    """Raised when the Vobiz API returns an error response."""

    def __init__(self, status_code: int, message: str, detail: str = ""):
        self.status_code = status_code
        self.message = message
        self.detail = detail
        super().__init__(f"Vobiz API {status_code}: {message} {detail}".strip())


async def send_text_message(
    to: str,
    body: str,
    channel_id: Optional[str] = None,
    waba_id: Optional[str] = None,
) -> dict:
    """Send a WhatsApp text message via Vobiz.

    Args:
        to: Recipient phone in E.164 (e.g. +919876543210).
        body: Message text body.
        channel_id: Vobiz channel UUID. Falls back to VOBIZ_WA_CHANNEL_ID env var.
        waba_id: WhatsApp Business Account ID. Falls back to VOBIZ_WA_WABA_ID env var.

    Returns:
        The created Message object from the Vobiz API (201 Created).

    Raises:
        VobizError: On API error.
        RuntimeError: If Vobiz is not configured.
    """
    if not _is_configured():
        raise RuntimeError("Vobiz WhatsApp not configured — set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")

    channel_id = channel_id or os.getenv("VOBIZ_WA_CHANNEL_ID", "")
    waba_id = waba_id or os.getenv("VOBIZ_WA_WABA_ID", "")

    if not channel_id:
        raise RuntimeError("channel_id is required — set VOBIZ_WA_CHANNEL_ID or pass it explicitly.")
    if not waba_id:
        raise RuntimeError("waba_id is required — set VOBIZ_WA_WABA_ID or pass it explicitly.")

    payload = {
        "channel_id": channel_id,
        "waba_id": waba_id,
        "to": to,
        "type": "text",
        "text": {"body": body},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{VOBIZ_BASE_URL}/messages",
            headers=_auth_headers(),
            json=payload,
        )

    if resp.status_code >= 400:
        raise VobizError(resp.status_code, resp.text[:500])
    return resp.json()


async def send_template_message(
    to: str,
    template_name: str = DEFAULT_TEMPLATE_NAME,
    language_code: str = "en_US",
    channel_id: Optional[str] = None,
    waba_id: Optional[str] = None,
    components: Optional[list] = None,
    category: Optional[str] = None,
) -> dict:
    """Send a WhatsApp template message via Vobiz.

    Templates must be pre-approved in Meta Business Manager.
    Sending a template (re)opens a fresh 24-hour service window once the
    customer replies — after that, free-form text messages are allowed.

    Args:
        to: Recipient phone in E.164.
        template_name: Template name from Meta Business Manager.
            Defaults to "magazine_subscription".
        language_code: Language code (e.g. en_US, hi_IN).
        channel_id: Vobiz channel UUID. Falls back to env var.
        waba_id: WABA ID. Falls back to env var.
        components: Optional array of component objects for template variables.
        category: Optional template category.

    Returns:
        The created Message object from the Vobiz API.

    Raises:
        VobizError: On API error.
        RuntimeError: If Vobiz is not configured.
    """
    if not _is_configured():
        raise RuntimeError("Vobiz WhatsApp not configured — set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")

    channel_id = channel_id or os.getenv("VOBIZ_WA_CHANNEL_ID", "")
    waba_id = waba_id or os.getenv("VOBIZ_WA_WABA_ID", "")

    if not channel_id:
        raise RuntimeError("channel_id is required — set VOBIZ_WA_CHANNEL_ID or pass it explicitly.")
    if not waba_id:
        raise RuntimeError("waba_id is required — set VOBIZ_WA_WABA_ID or pass it explicitly.")

    template_obj: dict = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template_obj["components"] = components
    if category:
        template_obj["category"] = category

    payload = {
        "channel_id": channel_id,
        "waba_id": waba_id,
        "to": to,
        "type": "template",
        "template": template_obj,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{VOBIZ_BASE_URL}/messages",
            headers=_auth_headers(),
            json=payload,
        )

    if resp.status_code >= 400:
        raise VobizError(resp.status_code, resp.text[:500])
    return resp.json()


async def open_whatsapp_session(
    to: str,
    template_name: str = DEFAULT_TEMPLATE_NAME,
    language_code: str = "en_US",
    channel_id: Optional[str] = None,
    waba_id: Optional[str] = None,
    components: Optional[list] = None,
) -> dict:
    """Open a 24-hour WhatsApp conversational session by sending a template.

    WhatsApp only allows free-form text messages inside a 24-hour window
    that is opened by the customer messaging you, or by you sending an
    approved template and the customer replying to it.

    This function sends the "magazine_subscription" template (or a custom
    one) to (re)open that window.  Once the recipient replies, the 24-hour
    timer resets and free-form messages become deliverable.

    Args:
        to: Recipient phone in E.164.
        template_name: Meta-approved template name.  Defaults to
            "magazine_subscription".
        language_code: Language code (e.g. en_US, hi_IN).
        channel_id: Vobiz channel UUID. Falls back to env var.
        waba_id: WABA ID. Falls back to env var.
        components: Optional template variable components.

    Returns:
        The created Message object from the Vobiz API.
    """
    return await send_template_message(
        to=to,
        template_name=template_name,
        language_code=language_code,
        channel_id=channel_id,
        waba_id=waba_id,
        components=components,
    )


async def send_whatsapp_session(
    to: str,
    body: str,
    template_name: str = DEFAULT_TEMPLATE_NAME,
    language_code: str = "en_US",
    channel_id: Optional[str] = None,
    waba_id: Optional[str] = None,
    components: Optional[list] = None,
) -> dict:
    """Send a WhatsApp text message, opening a 24h session first if needed.

    Tries to send a free-form text message first.  If WhatsApp rejects it
    (because there is no active 24-hour window), falls back to sending the
    template to open the session, then retries the text message.

    Args:
        to: Recipient phone in E.164.
        body: Message text body.
        template_name: Template to use if session must be opened.
        language_code: Language code for the template.
        channel_id: Vobiz channel UUID. Falls back to env var.
        waba_id: WABA ID. Falls back to env var.
        components: Optional template variable components.

    Returns:
        The created Message object from the Vobiz API (the text message if
        it succeeded, or the template message if text was rejected).
    """
    try:
        return await send_text_message(
            to=to, body=body, channel_id=channel_id, waba_id=waba_id,
        )
    except VobizError as exc:
        if exc.status_code in (400, 403):
            logger.info("Text rejected (likely outside 24h window), sending template to open session")
            return await open_whatsapp_session(
                to=to,
                template_name=template_name,
                language_code=language_code,
                channel_id=channel_id,
                waba_id=waba_id,
                components=components,
            )
        raise


async def send_media_message(
    to: str,
    media_type: str,
    link: Optional[str] = None,
    media_id: Optional[str] = None,
    caption: Optional[str] = None,
    filename: Optional[str] = None,
    channel_id: Optional[str] = None,
    waba_id: Optional[str] = None,
) -> dict:
    """Send a WhatsApp media message (image, audio, video, document, sticker).

    Args:
        to: Recipient phone in E.164.
        media_type: One of image, audio, video, document, sticker.
        link: Public HTTPS URL of the media (alternative to media_id).
        media_id: Previously uploaded Meta media ID (alternative to link).
        caption: Optional caption (for image, video, document).
        filename: Optional filename (for document).
        channel_id: Vobiz channel UUID. Falls back to env var.
        waba_id: WABA ID. Falls back to env var.

    Returns:
        The created Message object from the Vobiz API.

    Raises:
        VobizError: On API error.
        RuntimeError: If Vobiz is not configured.
    """
    if not _is_configured():
        raise RuntimeError("Vobiz WhatsApp not configured — set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")

    channel_id = channel_id or os.getenv("VOBIZ_WA_CHANNEL_ID", "")
    waba_id = waba_id or os.getenv("VOBIZ_WA_WABA_ID", "")

    if not channel_id:
        raise RuntimeError("channel_id is required — set VOBIZ_WA_CHANNEL_ID or pass it explicitly.")
    if not waba_id:
        raise RuntimeError("waba_id is required — set VOBIZ_WA_WABA_ID or pass it explicitly.")

    if media_type not in ("image", "audio", "video", "document", "sticker"):
        raise ValueError(f"Invalid media_type: {media_type}")

    media_obj: dict = {}
    if link:
        media_obj["link"] = link
    elif media_id:
        media_obj["id"] = media_id
    else:
        raise ValueError("Either link or media_id is required for media messages.")
    if caption:
        media_obj["caption"] = caption
    if filename:
        media_obj["filename"] = filename

    payload = {
        "channel_id": channel_id,
        "waba_id": waba_id,
        "to": to,
        "type": media_type,
        "media": media_obj,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{VOBIZ_BASE_URL}/messages",
            headers=_auth_headers(),
            json=payload,
        )

    if resp.status_code >= 400:
        raise VobizError(resp.status_code, resp.text[:500])
    return resp.json()


async def list_channels() -> list:
    """List all WhatsApp channels connected to the Vobiz account.

    Returns:
        List of channel objects.

    Raises:
        VobizError: On API error.
        RuntimeError: If Vobiz is not configured.
    """
    if not _is_configured():
        raise RuntimeError("Vobiz WhatsApp not configured — set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{VOBIZ_BASE_URL}/channels/whatsapp",
            headers=_auth_headers(),
        )

    if resp.status_code >= 400:
        raise VobizError(resp.status_code, resp.text[:500])
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return [data] if data else []


async def create_channel(
    waba_id: str,
    phone_number_id: str,
    phone_number: str,
    access_token: str,
    display_name: str,
    number_onboarding_mode: str = "bring_your_own",
    number_provider: str = "meta_direct",
    number_order_id: Optional[str] = None,
) -> dict:
    """Create a new WhatsApp Business channel on Vobiz.

    Args:
        waba_id: WhatsApp Business Account ID from Meta.
        phone_number_id: Phone Number ID from Meta.
        phone_number: Phone number in E.164 format.
        access_token: Meta system-user access token (starts with EAA...).
        display_name: Business display name shown to customers.
        number_onboarding_mode: One of buy_from_vobiz, bring_your_own, embedded_signup.
        number_provider: Number provider, e.g. meta_direct.
        number_order_id: UUID of a completed Vobiz number order (optional).

    Returns:
        The created channel object (201 Created).

    Raises:
        VobizError: On API error.
        RuntimeError: If Vobiz is not configured.
    """
    if not _is_configured():
        raise RuntimeError("Vobiz WhatsApp not configured — set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")

    payload: dict = {
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "phone_number": phone_number,
        "number_onboarding_mode": number_onboarding_mode,
        "number_provider": number_provider,
        "access_token": access_token,
        "display_name": display_name,
    }
    if number_order_id:
        payload["number_order_id"] = number_order_id

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{VOBIZ_BASE_URL}/channels/whatsapp",
            headers=_auth_headers(),
            json=payload,
        )

    if resp.status_code >= 400:
        raise VobizError(resp.status_code, resp.text[:500])
    return resp.json()


async def delete_channel(channel_id: str) -> dict:
    """Delete a WhatsApp channel by its ID.

    Args:
        channel_id: The UUID of the channel to delete.

    Returns:
        Response from the Vobiz API.

    Raises:
        VobizError: On API error.
        RuntimeError: If Vobiz is not configured.
    """
    if not _is_configured():
        raise RuntimeError("Vobiz WhatsApp not configured — set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{VOBIZ_BASE_URL}/channels/whatsapp/{channel_id}",
            headers=_auth_headers(),
        )

    if resp.status_code >= 400:
        raise VobizError(resp.status_code, resp.text[:500])
    return resp.json() if resp.content else {"status": "deleted"}


async def update_channel(
    channel_id: str,
    display_name: Optional[str] = None,
    access_token: Optional[str] = None,
) -> dict:
    """Update an existing WhatsApp channel's display name or access token.

    Args:
        channel_id: The UUID of the channel to update.
        display_name: New display name (optional).
        access_token: New Meta access token (optional).

    Returns:
        The updated channel object (200 OK).

    Raises:
        VobizError: On API error.
        RuntimeError: If Vobiz is not configured.
    """
    if not _is_configured():
        raise RuntimeError("Vobiz WhatsApp not configured — set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN.")

    payload: dict = {}
    if display_name:
        payload["display_name"] = display_name
    if access_token:
        payload["access_token"] = access_token
    if not payload:
        raise ValueError("At least one of display_name or access_token must be provided.")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.put(
            f"{VOBIZ_BASE_URL}/channels/whatsapp/{channel_id}",
            headers=_auth_headers(),
            json=payload,
        )

    if resp.status_code >= 400:
        raise VobizError(resp.status_code, resp.text[:500])
    return resp.json()
