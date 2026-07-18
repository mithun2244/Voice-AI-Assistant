"""
Offline tests for the reasoning graph and the human-in-the-loop hand-off.

Everything here is deterministic and network-free: the NVIDIA LLM/embeddings
and the Discord webhook are mocked, so CI never depends on live endpoints.

Run:  pytest backend/tests -q      (from the repo root)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# Put backend/ on the path so `import agent` / `import tools` resolve the same
# way they do when the app runs from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import tools  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def fake_retriever(monkeypatch):
    """Replace ChromaDB/NVIDIA embeddings with a static in-memory retriever."""

    class _Doc:
        page_content = "Built Insight-RAG and an Agentic FAQ Support Pipeline."

    class _Retriever:
        def invoke(self, _query):
            return [_Doc()]

    monkeypatch.setattr(agent, "get_retriever", lambda k=4: _Retriever())


@pytest.fixture
def webhook_posts(monkeypatch):
    """Capture Discord webhook POSTs instead of sending them; returns the list."""
    posts: list[dict] = []

    class _Resp:
        def raise_for_status(self):
            return None

    def _post(url, json=None, timeout=None):
        posts.append({"url": url, "json": json})
        return _Resp()

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/mock")
    monkeypatch.setattr(tools.requests, "post", _post)
    return posts


def _tool_call(cid="c1", reason="compensation"):
    return AIMessage(
        content="",
        tool_calls=[{"name": "transfer_to_author", "args": {"reason": reason}, "id": cid}],
    )


# ── Graph / helpers ──────────────────────────────────────────────────
def test_graph_compiles_with_expected_nodes():
    nodes = set(agent.voice_agent.get_graph().nodes)
    assert {"retrieve", "reason", "tools"} <= nodes


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Yes, please connect me.", True),
        ("Yeah sure, go ahead.", True),
        ("What is your expected salary?", False),
        ("Tell me about your projects.", False),
    ],
)
def test_consent_detection(text, expected):
    assert agent._caller_consented([HumanMessage(content=text)]) is expected


# ── Hand-off guard (idempotency + consent) ───────────────────────────
def test_guard_blocks_without_consent(webhook_posts):
    state = {"messages": [HumanMessage(content="What is your salary?"), _tool_call()], "context": []}
    out = agent.guarded_tools_node(state)
    assert webhook_posts == []                      # nothing posted
    assert out["messages"][0].status == "error"
    assert out["messages"][0].content.startswith("NOT SENT")


def test_guard_allows_with_consent(webhook_posts):
    state = {"messages": [HumanMessage(content="Yes, connect me."), _tool_call()], "context": []}
    out = agent.guarded_tools_node(state)
    assert len(webhook_posts) == 1                  # exactly one post
    assert "http://localhost:5173/" in webhook_posts[0]["json"]["content"]
    assert out["messages"][0].status != "error"


def test_guard_is_idempotent(webhook_posts):
    prior = ToolMessage(content="pinged the author", name="transfer_to_author", tool_call_id="old")
    state = {
        "messages": [prior, HumanMessage(content="yes connect me again"), _tool_call()],
        "context": [],
    }
    out = agent.guarded_tools_node(state)
    assert webhook_posts == []                      # no duplicate post
    assert out["messages"][0].status == "error"


def test_tool_degrades_when_webhook_unset(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    result = tools.transfer_to_author.invoke({"reason": "anything"})
    assert "noted your request" in result or "wasn't able" in result


# ── Full reasoning flow with a scripted (mock) LLM ───────────────────
class _ScriptedLLM:
    """Returns pre-baked AIMessages in order — stands in for ChatNVIDIA."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0

    def invoke(self, _prompt):
        msg = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return msg


def test_full_flow_exactly_one_post(monkeypatch, webhook_posts):
    offer = AIMessage(content="I don't have that detail. Want me to connect you with them?")
    confirm = AIMessage(content="The author has been notified.")
    # Tool-bound model: turn 1 -> offer (no tool), turn 2 -> tool call.
    monkeypatch.setattr(agent, "llm_with_tools", _ScriptedLLM([offer, _tool_call()]))
    # Tool-free model used after a tool runs -> spoken confirmation.
    monkeypatch.setattr(agent, "llm", _ScriptedLLM([confirm]))

    # Turn 1: unanswerable question -> agent offers, no webhook.
    s1 = agent.voice_agent.invoke({"messages": [HumanMessage(content="What's your salary?")]})
    assert webhook_posts == []
    assert s1["messages"][-1].content.startswith("I don't have that detail")

    # Turn 2: caller consents -> exactly one webhook post + spoken confirmation.
    s2 = agent.voice_agent.invoke(
        {"messages": s1["messages"] + [HumanMessage(content="Yes, connect me.")]}
    )
    assert len(webhook_posts) == 1
    assert s2["messages"][-1].content == "The author has been notified."
