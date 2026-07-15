"""
FastAPI server that ties the browser to the LiveKit WebRTC room.

Endpoints:
  GET  /health          -> liveness check
  POST /token           -> mint a LiveKit access token for a browser client
  POST /ask             -> text-only path into the LangGraph agent (debug/UI fallback)

The browser joins the LiveKit room with the minted token and streams
microphone audio over WebRTC (no phone network required). A LiveKit
agent worker (see agent.py placeholders) subscribes to that audio,
runs Parakeet ASR -> LangGraph -> TTS, and publishes the reply audio back.

Run:
    uvicorn server:app --reload --port 8000     # from the backend/ dir
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel

# Load the project-root .env explicitly (one level up from backend/).
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "secret")

app = FastAPI(title="Voice Agent Backend")

# Allow the local React dev server to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class TokenRequest(BaseModel):
    room: str = "voice-agent"
    identity: str = "hiring-manager"


class TokenResponse(BaseModel):
    token: str
    url: str


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/token", response_model=TokenResponse)
def create_token(req: TokenRequest) -> TokenResponse:
    """Mint a short-lived LiveKit join token for a browser participant."""
    if LIVEKIT_API_KEY == "devkey":
        # Non-fatal warning: fine for local `livekit-server --dev`, not prod.
        print("⚠️  Using default dev LiveKit credentials.")
    grant = api.VideoGrants(room_join=True, room=req.room)
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(req.identity)
        .with_name(req.identity)
        .with_grants(grant)
        .to_jwt()
    )
    return TokenResponse(token=token, url=LIVEKIT_URL)


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    """Text-only path into the agent (handy for UI fallback / debugging)."""
    # Imported lazily so the server still boots if ChatNVIDIA deps are missing.
    from agent import agent

    result = agent.invoke({"transcript": req.question})
    return {"answer": result.get("response_text", "")}
