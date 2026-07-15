"""
Headless smoke test for the voice agent — no browser required.

Acts as a synthetic caller: joins the LiveKit room exactly like the frontend
would, publishes a spoken question (synthesized with NVIDIA TTS), and listens
for the agent's spoken reply. Verifies the whole real-time loop:

    caller mic ─▶ Parakeet STT ─▶ LangGraph (ChromaDB + Llama) ─▶ Magpie TTS ─▶ caller

Prerequisites (same as a real call):
  • NVIDIA_API_KEY in the project-root .env
  • a LiveKit server running        (e.g. `livekit-server --dev`)
  • the agent worker running        (`python backend/agent_worker.py dev`)
  • a seeded ChromaDB store          (`python backend/rag.py`)

Usage (from the repo root or anywhere):
    python backend/scripts/smoke_call.py
    python backend/scripts/smoke_call.py "What is your strongest project?"

Exit code 0 = PASS (agent joined and spoke a reply); non-zero = FAIL.
Watch the worker console alongside this for the per-stage pipeline logs.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Project-root .env is two levels up from backend/scripts/.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from livekit import api, rtc  # noqa: E402
from livekit.plugins import nvidia  # noqa: E402

URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
SECRET = os.getenv("LIVEKIT_API_SECRET", "secret")
ROOM = os.getenv("SMOKE_ROOM", "voice-agent")

# Minimum reply audio (seconds) to consider the agent "responsive".
MIN_REPLY_SECONDS = 1.0
# How long to wait for the agent to join, and for it to answer.
AGENT_JOIN_TIMEOUT = 20.0
GREETING_GRACE = 6.0
REPLY_WAIT = 18.0


async def _synthesize(text: str) -> list[rtc.AudioFrame]:
    """Turn the question into audio frames via NVIDIA TTS."""
    tts = nvidia.TTS()
    frames: list[rtc.AudioFrame] = []
    stream = tts.synthesize(text)
    async for ev in stream:
        frames.append(ev.frame)
    await stream.aclose()
    if not frames:
        raise RuntimeError("TTS returned no audio frames (NVIDIA function degraded?)")
    return frames


async def run(question: str) -> bool:
    frames = await _synthesize(question)
    sr, ch = frames[0].sample_rate, frames[0].num_channels
    print(f"[smoke] question synthesized: {len(frames)} frames @ {sr}Hz x{ch}")

    room = rtc.Room()
    agent_joined = asyncio.Event()
    reply = {"frames": 0, "samples": 0, "sr": sr, "identity": None}

    @room.on("participant_connected")
    def _on_participant(p: rtc.RemoteParticipant) -> None:
        print(f"[smoke] agent joined: {p.identity!r}")
        agent_joined.set()

    @room.on("track_subscribed")
    def _on_track(track, pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"[smoke] subscribed to agent audio from {participant.identity!r}")
            reply["identity"] = participant.identity
            asyncio.create_task(_drain(track))

    async def _drain(track: rtc.Track) -> None:
        async for ev in rtc.AudioStream(track):
            reply["frames"] += 1
            reply["samples"] += ev.frame.samples_per_channel
            reply["sr"] = ev.frame.sample_rate

    token = (
        api.AccessToken(KEY, SECRET)
        .with_identity("smoke-caller")
        .with_grants(api.VideoGrants(room_join=True, room=ROOM))
        .to_jwt()
    )
    await room.connect(URL, token)
    present = [p.identity for p in room.remote_participants.values()]
    print(f"[smoke] connected to {ROOM!r}; participants already present: {present}")

    source = rtc.AudioSource(sr, ch)
    track = rtc.LocalAudioTrack.create_audio_track("smoke-mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    print("[smoke] published mic track")

    try:
        await asyncio.wait_for(agent_joined.wait(), timeout=AGENT_JOIN_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[smoke] FAIL: no agent joined within {AGENT_JOIN_TIMEOUT:.0f}s — "
              "is the worker running and dispatched to this room?")
        await room.disconnect()
        return False

    # Let the greeting play, then ask.
    await asyncio.sleep(GREETING_GRACE)
    print(f"[smoke] asking: {question!r}")
    t0 = time.time()
    for f in frames:
        await source.capture_frame(f)
    print(f"[smoke] finished speaking in {time.time() - t0:.1f}s; awaiting reply...")

    await asyncio.sleep(REPLY_WAIT)
    secs = reply["samples"] / reply["sr"] if reply["sr"] else 0.0
    print(f"[smoke] received {reply['frames']} agent audio frames "
          f"(~{secs:.1f}s of speech) @ {reply['sr']}Hz")
    await room.disconnect()

    return reply["frames"] > 0 and secs >= MIN_REPLY_SECONDS


def main() -> int:
    question = " ".join(sys.argv[1:]) or "Hi there! What projects have you built?"
    if not os.getenv("NVIDIA_API_KEY"):
        print("[smoke] FAIL: NVIDIA_API_KEY not set (check the project-root .env).")
        return 2
    try:
        ok = asyncio.run(run(question))
    except Exception as e:  # noqa: BLE001
        print(f"[smoke] ERROR: {type(e).__name__}: {e}")
        return 1
    print("[smoke] RESULT:", "[PASS]" if ok else "[FAIL]")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
