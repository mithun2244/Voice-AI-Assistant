"""
LiveKit agent worker — the real-time audio bridge.

This is the process that actually replaces the two audio *placeholders* in
`agent.py` (`transcribe_node` / `synthesize_node`). It joins the same LiveKit
room the browser connects to, subscribes to the caller's microphone track, and
runs the full speech loop:

    caller mic ──▶ Parakeet STT (ASR + EOU) ──▶ LangGraph reasoning ──▶ TTS ──▶ caller

The high-level `AgentSession` owns the audio plumbing (VAD, turn-taking,
barge-in, interruption). We hand it three plugins:

  • stt : NVIDIA Parakeet Realtime  (streaming transcription + End-of-Utterance)
  • llm : our compiled LangGraph, wrapped by the LangChain adapter
  • tts : an NVIDIA voice

Because the session plugins now do the ASR and TTS, the graph's own
`transcribe_node` / `synthesize_node` become no-ops on this path — only the
`retrieve` (ChromaDB) and `reason` (Llama 3.3 70B) nodes do real work here.

────────────────────────────────────────────────────────────────────────────
Prerequisites (all installed via requirements.txt):
  • NVIDIA_API_KEY in the project-root .env  (Parakeet STT, Magpie TTS, Llama)
  • a reachable LiveKit server (e.g. `livekit-server --dev`)
  • a seeded ChromaDB store            (`python backend/rag.py`)

Run the worker (it long-polls LiveKit for jobs and auto-joins new rooms):

    python backend/agent_worker.py dev      # dev mode, hot-reload
    python backend/agent_worker.py start    # production

Then open the frontend, click "Talk to my AI", and watch this console — the
event handlers below log each stage of the audio pipeline.
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load the project-root .env BEFORE importing `agent` (which builds ChatNVIDIA)
# or constructing the NVIDIA plugins — all of them read NVIDIA_API_KEY.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
logger = logging.getLogger("voice-agent-worker")

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
)

# ── Plugins ──────────────────────────────────────────────────────────
from livekit.plugins import langchain as lk_langchain
from livekit.plugins import nvidia, silero

# The messages-based graph (ChromaDB + Llama 3.3 70B) built for the LLMAdapter.
from agent import voice_agent

INSTRUCTIONS = (
    "You are the personal voice agent for a software engineer, speaking to "
    "hiring managers. Keep replies warm, concise, and spoken (1-3 sentences). "
    "Ground every claim in the retrieved resume and GitHub context."
)


class InterviewAgent(Agent):
    """The persona the hiring manager talks to."""

    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)


def _optional(**pairs) -> dict:
    """Keep only the kwargs whose value is set (non-empty)."""
    return {k: v for k, v in pairs.items() if v}


def build_stt() -> nvidia.STT:
    """NVIDIA Parakeet STT, overridable via env when a function is degraded."""
    return nvidia.STT(
        **_optional(
            model=os.getenv("NVIDIA_STT_MODEL"),
            function_id=os.getenv("NVIDIA_STT_FUNCTION_ID"),
        )
    )


def build_tts() -> nvidia.TTS:
    """NVIDIA Magpie TTS, overridable via env.

    NVIDIA occasionally marks a hosted function DEGRADED ("cannot be invoked"),
    which yields zero audio frames and a silent agent. When that happens, pick
    another ACTIVE voice/function from the registry and set NVIDIA_TTS_VOICE /
    NVIDIA_TTS_FUNCTION_ID in .env — no code change needed.
    """
    return nvidia.TTS(
        **_optional(
            voice=os.getenv("NVIDIA_TTS_VOICE"),
            function_id=os.getenv("NVIDIA_TTS_FUNCTION_ID"),
        )
    )


async def entrypoint(ctx: JobContext) -> None:
    """Called once per LiveKit room the worker is dispatched to."""
    await ctx.connect()
    logger.info("worker connected to room %r", ctx.room.name)

    # ── Audio-arrival instrumentation (room level) ───────────────────
    # If you DON'T see these when the browser connects and unmutes, the
    # problem is upstream of the agent (frontend not publishing, wrong
    # room, or the worker never got dispatched) — not the LangGraph.
    for p in ctx.room.remote_participants.values():
        logger.info("participant already present: %r", p.identity)

    @ctx.room.on("participant_connected")
    def _on_participant(p) -> None:
        logger.info("participant connected: %r", p.identity)

    @ctx.room.on("track_subscribed")
    def _on_track(track, publication, participant) -> None:
        logger.info(
            "track subscribed: kind=%s from %r  <-- browser mic audio is routing",
            track.kind, participant.identity,
        )

    session = AgentSession(
        # ── STT · NVIDIA Parakeet Realtime (ASR + End-of-Utterance) ──
        # The default model is Parakeet streaming ASR; it emits interim +
        # final transcripts and endpointing so AgentSession knows when the
        # caller has finished speaking. Reads NVIDIA_API_KEY from the env.
        stt=build_stt(),
        #
        # ── LLM · our LangGraph reasoning core ──
        # LLMAdapter bridges the compiled LangGraph into the voice pipeline.
        # `voice_agent` (agent.py) is the messages-based graph it expects:
        # retrieve (ChromaDB) → reason (Llama 3.3 70B), streamed back
        # token-by-token (stream_mode="messages") for low-latency TTS.
        llm=lk_langchain.LLMAdapter(graph=voice_agent),
        #
        # ── TTS · NVIDIA Magpie voice ──
        tts=build_tts(),
        #
        # ── Turn detection / voice activity ──
        vad=silero.VAD.load(),
    )

    # ── Pipeline instrumentation ─────────────────────────────────────
    # These handlers make the audio path observable. Watch the worker
    # console while you talk:
    #   • "USER transcribed"  → browser mic audio reached Parakeet STT
    #   • agent.py "graph ← user turn" → that transcript reached LangGraph
    #   • "conversation item" (assistant) → TTS is about to speak the reply
    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        # listening -> speaking means VAD is hearing the mic audio frames.
        logger.info("user state: %s -> %s", ev.old_state, ev.new_state)

    @session.on("user_input_transcribed")
    def _on_user_transcribed(ev) -> None:
        logger.info("USER transcribed (final=%s): %r", ev.is_final, ev.transcript)

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        # thinking -> speaking means TTS is producing audio back to the caller.
        logger.info("agent state: %s -> %s", ev.old_state, ev.new_state)

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        logger.info("conversation item [%s]: %r", ev.item.role, ev.item.text_content)

    @session.on("error")
    def _on_error(ev) -> None:
        msg = str(ev.error)
        logger.error("session error (source=%s): %s", getattr(ev, "source", "?"), msg)
        if "DEGRADED" in msg or "cannot be invoked" in msg:
            logger.error(
                "↑ NVIDIA reports this hosted function DEGRADED — it's an "
                "NVIDIA-side outage, not your code. Retry shortly, or set "
                "NVIDIA_TTS_FUNCTION_ID / NVIDIA_STT_FUNCTION_ID in .env to "
                "another ACTIVE function."
            )

    await session.start(
        agent=InterviewAgent(),
        room=ctx.room,
        room_input_options=RoomInputOptions(),
    )
    logger.info("session started — waiting for the caller to speak")

    # Speak first so the hiring manager knows the line is live.
    await session.generate_reply(
        instructions="Greet the hiring manager warmly and invite their first question."
    )


if __name__ == "__main__":
    # `voice_agent` is imported above so the graph compiles at worker startup
    # (fail fast if NVIDIA_API_KEY / deps are misconfigured).
    assert voice_agent is not None
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
