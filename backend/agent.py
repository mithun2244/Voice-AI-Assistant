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
import re
from pathlib import Path
from typing import Annotated, List, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from rag import get_retriever
from tools import transfer_to_author

# Load the project-root .env explicitly (one level up from backend/) so env
# vars resolve regardless of the launch directory.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger("voice-agent")

# ── Core reasoning LLM (NVIDIA NIM) ─────────────────────────────────
# Requires NVIDIA_API_KEY in the environment (see .env.example).
llm = ChatNVIDIA(model="meta/llama-3.3-70b-instruct", temperature=0.3)

# Tools the voice agent can call. The LLM used in the voice graph is bound to
# these; the text-only graph below uses the plain `llm` (no tools).
TOOLS = [transfer_to_author]
llm_with_tools = llm.bind_tools(TOOLS)

SYSTEM_PROMPT = (
    "You are the personal voice agent for a software engineer, speaking to "
    "hiring managers. Answer in a warm, concise, spoken style (1-3 sentences). "
    "Ground every claim in the retrieved resume and GitHub context provided.\n"
    "If the context does not cover the question, do NOT make something up. "
    "Say you don't have that detail, then OFFER to connect them directly to the "
    "author (e.g. 'Would you like me to connect you with them directly?'). "
    "Only if the caller says yes, call the `transfer_to_author` tool, passing a "
    "short summary of their question as the reason. After the tool runs, tell "
    "the caller the author has been notified. Never call the tool without the "
    "caller's explicit yes."
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

    On a normal turn the tool-bound LLM may return a `transfer_to_author` tool
    call (routed to the `tools` node below). Once a tool has just run, we reason
    with the *tool-free* LLM so the model speaks the confirmation instead of
    calling the tool again — otherwise it loops (reason → tools → reason → …)
    and fires the webhook repeatedly. The LLM call streams token-by-token when
    the LLMAdapter runs the graph in `stream_mode="messages"`.
    """
    messages = state["messages"]
    just_ran_tool = bool(messages) and getattr(messages[-1], "type", None) == "tool"
    model = llm if just_ran_tool else llm_with_tools

    context_block = "\n\n".join(state.get("context", [])) or "(no context found)"
    prompt = [
        SystemMessage(content=SYSTEM_PROMPT),
        SystemMessage(content=f"Retrieved context for the latest question:\n{context_block}"),
        *messages,
    ]
    answer = model.invoke(prompt)
    if getattr(answer, "tool_calls", None):
        logger.info("graph → tool call(s): %s", [tc["name"] for tc in answer.tool_calls])
    else:
        logger.info("graph → reply: %r", getattr(answer, "content", "")[:120])
    return {"messages": [answer]}


def _route_after_reason(state: VoiceState) -> str:
    """Send tool calls to the tools node; otherwise end the turn."""
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else END


# ── Deterministic guards for the hand-off ────────────────────────────
# The model decides *whether* to call transfer_to_author, but these guards
# decide whether it is actually *allowed* to fire — so a premature or repeated
# tool call can never post to Discord.
_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|please|ok|okay|connect me|go ahead|do it|"
    r"sounds good|that works|absolutely|definitely)\b",
    re.IGNORECASE,
)


def _already_transferred(messages: List[AnyMessage]) -> bool:
    """True if transfer_to_author already ran (successfully) this conversation."""
    return any(
        getattr(m, "type", None) == "tool"
        and getattr(m, "name", None) == "transfer_to_author"
        and getattr(m, "status", "success") != "error"
        and "NOT SENT" not in (m.content if isinstance(m.content, str) else "")
        for m in messages
    )


def _caller_consented(messages: List[AnyMessage]) -> bool:
    """True if the caller's most recent utterance is an affirmative."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "human":
            text = m.content if isinstance(m.content, str) else str(m.content)
            return bool(_AFFIRMATIVE.search(text))
    return False


def guarded_tools_node(state: VoiceState) -> dict:
    """Execute transfer_to_author ONLY with fresh consent and only once.

    Replaces the stock ToolNode so the two guarantees are enforced in code,
    not left to the LLM: (1) idempotency — at most one successful notification
    per conversation; (2) consent — the caller must have just said yes.
    Blocked calls return a ToolMessage that steers the model to do the right
    thing next (offer first, or reassure that the author was already pinged).
    """
    last = state["messages"][-1]
    out: List[ToolMessage] = []
    for call in getattr(last, "tool_calls", []) or []:
        if call["name"] != "transfer_to_author":
            continue
        call_id = call["id"]
        if _already_transferred(state["messages"]):
            logger.info("hand-off guard: BLOCKED (already notified this session)")
            out.append(ToolMessage(
                content="NOT SENT — the author was already notified in this "
                        "conversation. Reassure the caller they'll be in touch soon.",
                name="transfer_to_author", tool_call_id=call_id, status="error",
            ))
        elif not _caller_consented(state["messages"]):
            logger.info("hand-off guard: BLOCKED (no explicit consent yet)")
            out.append(ToolMessage(
                content="NOT SENT — the caller has not agreed to be connected "
                        "yet. Say you don't have that detail, offer to connect "
                        "them to the author, and only transfer after they say yes.",
                name="transfer_to_author", tool_call_id=call_id, status="error",
            ))
        else:
            logger.info("hand-off guard: ALLOWED — notifying author")
            result = transfer_to_author.invoke(call["args"])
            out.append(ToolMessage(
                content=result, name="transfer_to_author", tool_call_id=call_id,
            ))
    return {"messages": out}


def build_voice_graph():
    """Compile the messages-based graph the LLMAdapter consumes.

        retrieve ─▶ reason ─▶ END
                       │  ▲
                       ▼  │ (after tool runs, reason speaks the confirmation)
                     tools
    """
    graph = StateGraph(VoiceState)
    graph.add_node("retrieve", voice_retrieve_node)
    graph.add_node("reason", voice_reason_node)
    graph.add_node("tools", guarded_tools_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "reason")
    graph.add_conditional_edges("reason", _route_after_reason, {"tools": "tools", END: END})
    graph.add_edge("tools", "reason")
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
