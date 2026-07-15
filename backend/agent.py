"""
LangGraph orchestration for the real-time Voice Agent.

Pipeline (one conversational turn):

    ┌──────────────┐   ┌───────────┐   ┌──────────┐   ┌───────────────┐
    │ transcribe   │──▶│ retrieve  │──▶│ reason   │──▶│ synthesize    │
    │ (Parakeet)   │   │ (Chroma)  │   │ (Llama)  │   │ (TTS)         │
    └──────────────┘   └───────────┘   └──────────┘   └───────────────┘

The audio-facing nodes (`transcribe_node` / `synthesize_node`) are
placeholders. In production they are driven by the LiveKit media stream:
raw audio frames flow in, Parakeet emits transcripts + an End-of-Utterance
(EOU) signal, and the TTS node streams synthesized audio frames back out.

Run a quick text-only smoke test:

    python backend/agent.py "What are your strongest projects?"
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, List, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from rag import get_retriever

# Load the project-root .env explicitly (one level up from backend/) so env
# vars resolve regardless of the launch directory.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger("voice-agent")

# ── Core reasoning LLM (NVIDIA NIM) ─────────────────────────────────
# Requires NVIDIA_API_KEY in the environment (see .env.example).
llm = ChatNVIDIA(model="meta/llama-3.3-70b-instruct", temperature=0.3)

SYSTEM_PROMPT = (
    "You are the personal voice agent for a software engineer, speaking to "
    "hiring managers. Answer in a warm, concise, spoken style (1-3 sentences). "
    "Ground every claim in the retrieved resume and GitHub context provided. "
    "If the context does not cover the question, say so honestly."
)


# ── Graph state ─────────────────────────────────────────────────────
class AgentState(TypedDict, total=False):
    """Shared state threaded through every node in a single turn."""

    audio_in: bytes          # raw inbound audio frames (from LiveKit)
    transcript: str          # user's utterance text (from Parakeet)
    eou: bool                # True once Parakeet flags End-of-Utterance
    context: List[str]       # retrieved resume / project snippets
    response_text: str       # LLM answer to speak
    audio_out: bytes         # synthesized audio frames (to LiveKit)


# ── Nodes ───────────────────────────────────────────────────────────
def transcribe_node(state: AgentState) -> AgentState:
    """
    PLACEHOLDER — NVIDIA Parakeet Realtime ASR + EOU detection.

    Wire-up notes:
      * Stream `state["audio_in"]` frames to the Parakeet Realtime NIM
        (streaming gRPC/WebSocket ASR endpoint).
      * Parakeet returns interim + final transcripts and an End-of-Utterance
        signal. Only advance the graph once EOU fires so we reason on a
        complete thought rather than partial audio.
      * Populate `state["transcript"]` and `state["eou"]`.

    For now we pass through any transcript already placed on the state so the
    text-only smoke test works.
    """
    transcript = state.get("transcript", "")
    return {"transcript": transcript, "eou": True}


def retrieve_node(state: AgentState) -> AgentState:
    """Pull the most relevant resume / GitHub snippets from ChromaDB."""
    query = state.get("transcript", "")
    if not query:
        return {"context": []}
    retriever = get_retriever()
    docs = retriever.invoke(query)
    return {"context": [d.page_content for d in docs]}


def reason_node(state: AgentState) -> AgentState:
    """Core reasoning with meta/llama-3.3-70b-instruct via NVIDIA NIM."""
    context_block = "\n\n".join(state.get("context", [])) or "(no context found)"
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"Retrieved context:\n{context_block}\n\n"
                f"Question: {state.get('transcript', '')}"
            )
        ),
    ]
    answer = llm.invoke(messages)
    return {"response_text": answer.content}


def synthesize_node(state: AgentState) -> AgentState:
    """
    PLACEHOLDER — NVIDIA TTS synthesis.

    Wire-up notes:
      * Send `state["response_text"]` to the NVIDIA TTS NIM (e.g. a Riva /
        Magpie TTS voice) and stream the returned audio frames.
      * Publish those frames onto the outbound LiveKit audio track so the
        browser plays them in real time.
      * Populate `state["audio_out"]`.

    For now this is a no-op that leaves `response_text` for the caller.
    """
    return {"audio_out": b""}


# ── Graph assembly ──────────────────────────────────────────────────
def build_graph():
    """Compile the LangGraph turn pipeline."""
    graph = StateGraph(AgentState)

    graph.add_node("transcribe", transcribe_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("reason", reason_node)
    graph.add_node("synthesize", synthesize_node)

    graph.add_edge(START, "transcribe")
    graph.add_edge("transcribe", "retrieve")
    graph.add_edge("retrieve", "reason")
    graph.add_edge("reason", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


agent = build_graph()


# ── Voice graph (messages-based, for the LiveKit LLMAdapter) ─────────
# `livekit-plugins-langchain`'s LLMAdapter drives a graph with a chat-style
# `messages` state: it invokes/streams the graph with the running transcript
# as a list of messages and reads the AIMessage back out. The text-only graph
# above uses a `transcript` string instead, so we expose this thin variant
# that reuses the same retrieval + reasoning, keyed off the conversation's
# latest human turn. Wire it in agent_worker.py as:
#     llm = langchain.LLMAdapter(graph=voice_agent)


class VoiceState(TypedDict):
    """Chat-style state the LLMAdapter reads and writes."""

    # `add_messages` appends (rather than overwrites) each turn, so the graph
    # keeps full conversation history for follow-up questions.
    messages: Annotated[List[AnyMessage], add_messages]
    context: List[str]


def _latest_user_text(messages: List[AnyMessage]) -> str:
    """Pull the most recent human utterance from the message history."""
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "human":
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


def voice_retrieve_node(state: VoiceState) -> dict:
    """Retrieve context for the latest question (mirrors retrieve_node)."""
    query = _latest_user_text(state["messages"])
    # This log firing proves the transcribed audio actually reached the graph.
    logger.info("graph ← user turn: %r", query)
    if not query:
        return {"context": []}
    docs = get_retriever().invoke(query)
    logger.info("retrieved %d context chunk(s) from ChromaDB", len(docs))
    return {"context": [d.page_content for d in docs]}


def voice_reason_node(state: VoiceState) -> dict:
    """Reason over history + retrieved context, appending an AIMessage.

    The LLM call streams token-by-token when the LLMAdapter runs the graph in
    `stream_mode="messages"`, so the caller can start TTS before the full
    answer is ready.
    """
    context_block = "\n\n".join(state.get("context", [])) or "(no context found)"
    prompt = [
        SystemMessage(content=SYSTEM_PROMPT),
        SystemMessage(content=f"Retrieved context for the latest question:\n{context_block}"),
        *state["messages"],
    ]
    answer = llm.invoke(prompt)
    logger.info("graph → reply: %r", getattr(answer, "content", "")[:120])
    return {"messages": [answer]}


def build_voice_graph():
    """Compile the messages-based graph the LLMAdapter consumes."""
    graph = StateGraph(VoiceState)
    graph.add_node("retrieve", voice_retrieve_node)
    graph.add_node("reason", voice_reason_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "reason")
    graph.add_edge("reason", END)
    return graph.compile()


voice_agent = build_voice_graph()


# ── Text-only smoke test ────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "Tell me about your background."
    if not os.getenv("NVIDIA_API_KEY"):
        print("[!] NVIDIA_API_KEY is not set — check the project-root .env.")
    result = agent.invoke({"transcript": question})
    print(f"\nUSER : {question}")
    print(f"AGENT: {result.get('response_text', '(no response)')}\n")
