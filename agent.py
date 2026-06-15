"""LiveKit worker — Gemini Live voice AI entrypoint for OutboundAI.

Architecture (DO NOT change without reading the spec):
  1. Job dispatched with metadata containing phone_number + lead_name + overrides.
  2. We connect to the room first.
  3. We DIAL the SIP participant with wait_until_answered=True.
  4. ONLY AFTER the call is answered do we build & start the AgentSession.
  5. We watch participant_disconnected to keep the worker alive until hangup.
"""

import os
import ssl
import certifi

# SSL — must run before any TLS-using import (LiveKit, Supabase, Google).
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

import asyncio
import json
import logging
from typing import Optional

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
from livekit.plugins import noise_cancellation, silero

from db import init_db, get_default_agent_profile
from prompts import build_prompt
from tools import AppointmentTools

# .env is loaded ONLY for local-dev convenience and ONLY if explicitly enabled.
# In production (VPS / Coolify), real environment variables are the single
# source of truth and override is never allowed.
if os.getenv("OUTBOUNDAI_LOAD_DOTENV", "").lower() == "true":
    try:
        from dotenv import load_dotenv
        load_dotenv(".env", override=False)
    except Exception:
        pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

# Detect whether RoomOptions wrapper is available (newer livekit-agents).
try:
    from livekit.agents import RoomOptions  # noqa: F401
    _HAS_ROOM_OPTIONS = True
except Exception:
    _HAS_ROOM_OPTIONS = False


def _build_session(
    tools: list,
    system_prompt: str,
    model_override: Optional[str] = None,
    voice_override: Optional[str] = None,
) -> AgentSession:
    """Build an AgentSession backed by Gemini Live realtime audio.

    Per-call overrides are passed as locals so that concurrent calls running in
    the same worker process never share state via os.environ.
    """
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() == "true"
    model_name = model_override or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    voice = voice_override or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    api_key = os.getenv("GOOGLE_API_KEY", "")

    if use_realtime:
        try:
            from livekit.plugins import google as lk_google
            from google.genai import types as _gt

            realtime_kwargs = dict(
                model=model_name,
                voice=voice,
                api_key=api_key,
                instructions=system_prompt,
            )

            # Rule 6 — silence-prevention configs (all 3 mandatory)
            try:
                realtime_kwargs["session_resumption"] = _gt.SessionResumptionConfig(
                    transparent=True
                )
            except Exception:
                pass
            try:
                realtime_kwargs["context_window_compression"] = _gt.ContextWindowCompressionConfig(
                    trigger_tokens=25600,
                    sliding_window=_gt.SlidingWindow(target_tokens=12800),
                )
            except Exception:
                pass
            try:
                # Latency-tuned VAD:
                #   END_SENSITIVITY_HIGH    -> Gemini decides "user is done" aggressively.
                #   silence_duration_ms=500 -> waits only 0.5s of silence (was 2000ms,
                #                              which added ~2s to every single reply).
                #   prefix_padding_ms=200   -> keeps a small lead-in so first phonemes
                #                              aren't clipped.
                realtime_kwargs["realtime_input_config"] = _gt.RealtimeInputConfig(
                    automatic_activity_detection=_gt.AutomaticActivityDetection(
                        end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_HIGH,
                        silence_duration_ms=500,
                        prefix_padding_ms=200,
                    ),
                )
            except Exception:
                pass

            llm_realtime = lk_google.beta.realtime.RealtimeModel(**realtime_kwargs)
            return AgentSession(
                llm=llm_realtime,
                tools=tools,
                vad=silero.VAD.load(),
            )
        except Exception as exc:
            logger.exception("Failed to build Gemini realtime session, falling back: %s", exc)

    # Fallback pipeline (Deepgram STT + Gemini text LLM + Google TTS)
    from livekit.plugins import deepgram, google as lk_google
    return AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-2", language="multi"),
        llm=lk_google.LLM(model="gemini-2.0-flash", api_key=api_key),
        tts=lk_google.TTS(voice_name=voice),
        tools=tools,
    )


def _trunk_id_hint(trunk_id: str) -> str:
    if trunk_id and not trunk_id.startswith("ST_"):
        return (
            " OUTBOUND_TRUNK_ID should be the LiveKit outbound SIP trunk ID "
            "from LiveKit Cloud, usually starting with ST_; it should not be "
            "the Vobiz SIP domain/account UUID."
        )
    return ""


def _is_gemini_31_live_model(model_name: str) -> bool:
    model = (model_name or "").lower()
    return model.startswith("gemini-3.1-") and "live" in model


def _greeting_ready_delay() -> float:
    try:
        return max(float(os.getenv("GREETING_READY_DELAY_SECONDS", "0.1")), 0.0)
    except ValueError:
        return 0.1


class OutboundAssistant(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions, tools=[])


async def _safe_log(level: str, msg: str, detail: str = "") -> None:
    try:
        from db import log_error
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


async def _play_wav_file(ctx: agents.JobContext, filepath: str) -> bool:
    import wave
    if not os.path.exists(filepath):
        logger.warning(f"WAV file not found at {filepath}")
        return False

    logger.info(f"Playing WAV greeting from {filepath}...")
    try:
        with wave.open(filepath, "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()

            source = rtc.AudioSource(sample_rate, channels)
            track = rtc.LocalAudioTrack.create_audio_track("greeting-wav", source)
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            publication = await ctx.room.local_participant.publish_track(track, options)

            frame_duration = 0.02
            samples_per_channel = int(sample_rate * frame_duration)

            while True:
                data = wav.readframes(samples_per_channel)
                if not data:
                    break

                samples_read = len(data) // (channels * sample_width)
                if samples_read > 0:
                    frame = rtc.AudioFrame(
                        data=data,
                        sample_rate=sample_rate,
                        num_channels=channels,
                        samples_per_channel=samples_read,
                    )
                    await source.capture_frame(frame)
                await asyncio.sleep(frame_duration)

            logger.info("WAV greeting playback finished. Unpublishing track.")
            await ctx.room.local_participant.unpublish_track(publication.sid)
            return True
    except Exception as exc:
        logger.exception(f"Failed playing WAV file: {exc}")
        return False


async def entrypoint(ctx: agents.JobContext) -> None:
    logger.info("Connecting to room: %s", ctx.room.name)
    await ctx.connect()

    # ── Parse metadata (job + room) ──────────────────────────────────────────
    config_dict: dict = {}
    try:
        if ctx.job.metadata:
            config_dict.update(json.loads(ctx.job.metadata))
    except Exception:
        pass
    try:
        if ctx.room.metadata:
            config_dict.update(json.loads(ctx.room.metadata))
    except Exception:
        pass

    phone_number: Optional[str] = config_dict.get("phone_number")
    lead_name = config_dict.get("lead_name") or "there"
    business_name = config_dict.get("business_name") or "our company"
    service_type = config_dict.get("service_type") or "our service"
    custom_prompt = config_dict.get("system_prompt")

    # ── Per-call overrides (from agent_profile or single-call form) ─────────
    # Held as locals only — never written to os.environ so concurrent calls
    # don't leak voice/model across each other.
    voice_override: Optional[str] = config_dict.get("voice_override") or None
    model_override: Optional[str] = config_dict.get("model_override") or None

    enabled_tools: list = []
    if config_dict.get("tools_override"):
        try:
            enabled_tools = json.loads(config_dict["tools_override"])
            if not isinstance(enabled_tools, list):
                enabled_tools = []
        except Exception:
            enabled_tools = []

    # If no profile was supplied and a default exists, apply it (locally only).
    if not custom_prompt and not voice_override:
        try:
            default_profile = await get_default_agent_profile()
            if default_profile:
                if default_profile.get("voice") and not voice_override:
                    voice_override = default_profile["voice"]
                if default_profile.get("model") and not model_override:
                    model_override = default_profile["model"]
                if default_profile.get("system_prompt") and not custom_prompt:
                    custom_prompt = default_profile["system_prompt"]
                if default_profile.get("enabled_tools"):
                    try:
                        et = json.loads(default_profile["enabled_tools"])
                        if isinstance(et, list):
                            enabled_tools = et
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("Could not load default agent profile: %s", exc)

    system_prompt = build_prompt(
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        custom_prompt=custom_prompt,
        phone=phone_number or "",
    )

    tool_ctx = AppointmentTools(ctx, phone_number=phone_number, lead_name=lead_name)

    # ── DIAL FIRST (Rule 1) ──────────────────────────────────────────────────
    if phone_number:
        # If the SIP participant is already in the room (e.g. inbound), skip dial.
        already_here = any(
            "sip_" in p.identity for p in ctx.room.remote_participants.values()
        )
        if not already_here:
            trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")
            if not trunk_id:
                await _safe_log("error", "OUTBOUND_TRUNK_ID not set — cannot dial")
                ctx.shutdown()
                return
            await _safe_log("info", f"Dialing {phone_number} via trunk {trunk_id}")
            try:
                await ctx.api.sip.create_sip_participant(
                    api.CreateSIPParticipantRequest(
                        room_name=ctx.room.name,
                        sip_trunk_id=trunk_id,
                        sip_call_to=phone_number,
                        participant_identity=f"sip_{phone_number}",
                        wait_until_answered=True,
                    )
                )
            except Exception as exc:
                hint = _trunk_id_hint(trunk_id)
                await _safe_log("error", f"SIP dial failed for {phone_number}: {exc}{hint}")
                ctx.shutdown()
                return
            await _safe_log("info", f"Call ANSWERED — {phone_number}, starting AI session")

    # ── Play WAV in background while building/starting the session ──────────
    wav_path = os.path.join(os.path.dirname(__file__), "Greeting.wav")
    play_task = asyncio.create_task(_play_wav_file(ctx, wav_path))

    # ── Build & start session AFTER answer ───────────────────────────────────
    gemini_model = model_override or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    gemini_voice = voice_override or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    await _safe_log(
        "info",
        f"Building AI session — model={gemini_model} voice={gemini_voice}",
    )
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _safe_log("info", f"Tools loaded: {[t.__name__ for t in active_tools]}")

    session = _build_session(
        tools=active_tools,
        system_prompt=system_prompt,
        model_override=model_override,
        voice_override=voice_override,
    )

    # Rule 2 — never close_on_disconnect=True with SIP.
    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_options=_RO(
                input_options=RoomInputOptions(
                    noise_cancellation=noise_cancellation.BVCTelephony()
                )
            ),
        )
    else:
        session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_input_options=RoomInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony()
            ),
        )

    await session.start(**session_kwargs)
    await _safe_log("info", "Agent session started in parallel with WAV playback")

    # Wait for the WAV playback task to complete
    wav_played = await play_task

    if not wav_played:
        # ── Greeting FIRST (zero dead air) ───────────────────────────────────────
        # Gemini 3.1 Live rejects send_client_content after initial history seeding,
        # which makes generate_reply() wait until it times out. Use session.say()
        # for the opener and keep the tiny readiness pause configurable for Docker.
        greeting_delay = _greeting_ready_delay()
        if greeting_delay > 0:
            await asyncio.sleep(greeting_delay)
        if phone_number:
            greeting_text = f"Hi, am I speaking with {lead_name}?"
        else:
            greeting_text = "Hi, this is Priya from TBD Campus. How can I help?"

        greeting_fired = False
        try:
            await session.say(greeting_text, allow_interruptions=True)
            greeting_fired = True
            await _safe_log("info", f"Greeting via session.say(): {greeting_text}")
        except Exception as exc:
            await _safe_log("warning", f"session.say() failed: {exc}")

        if not greeting_fired and _is_gemini_31_live_model(gemini_model):
            await _safe_log(
                "warning",
                "Skipping generate_reply fallback for Gemini 3.1 Live; "
                "it is blocked by LiveKit agents#5260 / Gemini send_client_content limits.",
            )
        elif not greeting_fired:
            try:
                await session.generate_reply(
                    user_input=(
                        "[SYSTEM: outbound call just connected and the lead has picked up]"
                    ),
                    instructions=(
                        f"You must speak FIRST. Say exactly: \"{greeting_text}\""
                    ),
                )
                await _safe_log("info", "Greeting via generate_reply()")
            except Exception as exc:
                await _safe_log("warning", f"generate_reply failed: {exc}")

    # ── Optional S3 recording (fire-and-forget so it never blocks greeting) ──
    if phone_number:
        _aws_key = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
        _aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        _aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
        _s3_endpoint = os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
        _s3_region = os.getenv("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
        if _aws_key and _aws_secret and _aws_bucket:
            try:
                _recording_path = f"recordings/{ctx.room.name}.ogg"
                _egress_req = api.RoomCompositeEgressRequest(
                    room_name=ctx.room.name,
                    audio_only=True,
                    file_outputs=[
                        api.EncodedFileOutput(
                            file_type=api.EncodedFileType.OGG,
                            filepath=_recording_path,
                            s3=api.S3Upload(
                                access_key=_aws_key, secret=_aws_secret,
                                bucket=_aws_bucket, region=_s3_region,
                                endpoint=_s3_endpoint,
                            ),
                        )
                    ],
                )
                _egress = await ctx.api.egress.start_room_composite_egress(_egress_req)
                _ep = _s3_endpoint.rstrip("/")
                tool_ctx.recording_url = (
                    f"{_ep}/{_aws_bucket}/{_recording_path}"
                    if _ep else f"s3://{_aws_bucket}/{_recording_path}"
                )
                await _safe_log("info", f"Recording started: egress={_egress.egress_id}")
            except Exception as exc:
                await _safe_log("warning", f"Recording start failed (non-fatal): {exc}")

    # ── Keep alive until SIP participant disconnects ─────────────────────────
    if phone_number:
        sip_identity = f"sip_{phone_number}"
        disconnect_event = asyncio.Event()

        def _on_participant_disconnected(participant: rtc.RemoteParticipant):
            if participant.identity == sip_identity:
                disconnect_event.set()

        def _on_disconnected(*_args, **_kwargs):
            disconnect_event.set()

        ctx.room.on("participant_disconnected", _on_participant_disconnected)
        ctx.room.on("disconnected", _on_disconnected)

        try:
            await asyncio.wait_for(disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _safe_log("warning", "Call hit 1-hour safety timeout — shutting down")

        await _safe_log("info", f"SIP participant disconnected — ending session for {phone_number}")
        try:
            await session.aclose()
        except Exception:
            pass
    else:
        done = asyncio.Event()
        ctx.room.on("disconnected", lambda *a, **kw: done.set())
        try:
            await asyncio.wait_for(done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller")
    )
