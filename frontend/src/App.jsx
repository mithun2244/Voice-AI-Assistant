import { useState, useCallback } from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  useConnectionState,
  useLocalParticipant,
} from "@livekit/components-react";
import { ConnectionState } from "livekit-client";
import "@livekit/components-styles";
import "./App.css";

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || "http://localhost:8000";

export default function App() {
  const [connection, setConnection] = useState(null); // { token, url } | null
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState("");

  const startCall = useCallback(async () => {
    setConnecting(true);
    setError("");
    try {
      const res = await fetch(`${BACKEND_URL}/token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room: "voice-agent", identity: "hiring-manager" }),
      });
      if (!res.ok) throw new Error(`Backend returned ${res.status}`);
      const data = await res.json();
      setConnection({ token: data.token, url: data.url });
    } catch (e) {
      setError(
        "Couldn't reach the backend. Make sure it's running on " +
          `${BACKEND_URL} (uvicorn server:app --port 8000).`
      );
      console.error(e);
    } finally {
      setConnecting(false);
    }
  }, []);

  const endCall = useCallback(() => setConnection(null), []);

  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">AI Voice Agent</p>
        <h1>Talk to my AI</h1>
        <p className="subtitle">
          Ask about my experience, projects, and skills — out loud, in real time.
          Built on NVIDIA NIM, LangGraph, and WebRTC.
        </p>
      </header>

      {!connection ? (
        <LandingButton onClick={startCall} connecting={connecting} />
      ) : (
        <LiveKitRoom
          serverUrl={connection.url}
          token={connection.token}
          connect={true}
          audio={true}
          video={false}
          onDisconnected={endCall}
          data-lk-theme="default"
        >
          <CallPanel onEnd={endCall} />
          <RoomAudioRenderer />
        </LiveKitRoom>
      )}

      {error && <p className="error">{error}</p>}

      <footer className="footer">Powered by NVIDIA Parakeet · Llama 3.3 70B · LiveKit</footer>
    </div>
  );
}

function LandingButton({ onClick, connecting }) {
  return (
    <div className="mic-wrap">
      <button className="mic-button" onClick={onClick} disabled={connecting}>
        <MicIcon />
        <span>{connecting ? "Connecting…" : "Talk to my AI"}</span>
      </button>
      <p className="hint">Click and allow your microphone to start talking.</p>
    </div>
  );
}

/** Shown while connected to the LiveKit room. */
function CallPanel({ onEnd }) {
  const state = useConnectionState();
  const { isMicrophoneEnabled, localParticipant } = useLocalParticipant();

  const live = state === ConnectionState.Connected;

  const toggleMic = () =>
    localParticipant?.setMicrophoneEnabled(!isMicrophoneEnabled);

  return (
    <div className="call-panel">
      <div className={`status-orb ${live ? "live" : ""}`}>
        <span className="pulse" />
      </div>
      <p className="status-text">
        {live ? "Listening — go ahead and ask a question." : "Connecting…"}
      </p>

      <div className="controls">
        <button className="ctrl" onClick={toggleMic}>
          {isMicrophoneEnabled ? "Mute" : "Unmute"}
        </button>
        <button className="ctrl end" onClick={onEnd}>
          End
        </button>
      </div>
    </div>
  );
}

function MicIcon() {
  return (
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Z"
        fill="currentColor"
      />
      <path
        d="M19 11a1 1 0 1 0-2 0 5 5 0 0 1-10 0 1 1 0 1 0-2 0 7 7 0 0 0 6 6.92V21a1 1 0 1 0 2 0v-3.08A7 7 0 0 0 19 11Z"
        fill="currentColor"
      />
    </svg>
  );
}
