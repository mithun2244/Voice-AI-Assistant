# Deployment guide

Take the Voice Agent live for your CV:

| Piece | Host | What runs there |
|-------|------|-----------------|
| Frontend (React/Vite) | **Vercel** | the `frontend/` static site |
| API server (FastAPI) | **Render** (Web Service) | mints LiveKit tokens (`voice-agent-api`) |
| AI worker (LangGraph) | **Render** (Background Worker) | STT → reason → TTS (`voice-agent-worker`) |
| WebRTC infra | **LiveKit Cloud** | rooms / media routing |

```
Browser ── Vercel ──HTTPS──▶ Render (FastAPI)  ──▶ mints LiveKit token
   │                                                     │
   └──────────────── WebRTC ──▶ LiveKit Cloud ◀── Render (worker) joins room
```

> **Heads-up on cost:** Render **Background Workers are not free** (Starter tier
> and up). The Web Service, Vercel, LiveKit Cloud, and NVIDIA build.nvidia.com
> all have free tiers. Budget for the worker before you start.

---

## 0. Gather your credentials first

You'll paste these into dashboards below, so collect them up front:

- [ ] **NVIDIA** — `NVIDIA_API_KEY` from <https://build.nvidia.com>
- [ ] **LiveKit Cloud** — create a project at <https://cloud.livekit.io>, then
      Project → **Settings → Keys** gives you three values:
  - `LIVEKIT_URL`  (looks like `wss://<your-project>.livekit.cloud`)
  - `LIVEKIT_API_KEY`
  - `LIVEKIT_API_SECRET`
- [ ] **Discord** — a webhook URL (`DISCORD_WEBHOOK_URL`): Server Settings →
      Integrations → Webhooks → New Webhook → Copy URL

Keep these somewhere safe — **never commit them** (they belong only in the
dashboards, and your local `.env`, which is git-ignored).

---

## 1. Deploy the backend to Render (do this first)

The backend must exist before the frontend, because the frontend needs its URL.

1. [ ] Push your latest code to GitHub (this repo).
2. [ ] Render dashboard → **New → Blueprint** → connect this repo. Render reads
       `render.yaml` and proposes two services: `voice-agent-api` (web) and
       `voice-agent-worker` (worker).
3. [ ] It will prompt for every `sync: false` env var. Fill them in:

   **`voice-agent-api` (Web Service)**

   | Variable | Value |
   |----------|-------|
   | `LIVEKIT_URL` | `wss://<your-project>.livekit.cloud` |
   | `LIVEKIT_API_KEY` | from LiveKit Cloud |
   | `LIVEKIT_API_SECRET` | from LiveKit Cloud |
   | `NVIDIA_API_KEY` | from build.nvidia.com |
   | `CORS_ALLOW_ORIGINS` | *leave as a placeholder for now* — set in Step 3 |

   **`voice-agent-worker` (Background Worker)**

   | Variable | Value |
   |----------|-------|
   | `NVIDIA_API_KEY` | from build.nvidia.com |
   | `LIVEKIT_URL` | `wss://<your-project>.livekit.cloud` |
   | `LIVEKIT_API_KEY` | from LiveKit Cloud |
   | `LIVEKIT_API_SECRET` | from LiveKit Cloud |
   | `DISCORD_WEBHOOK_URL` | your Discord webhook |
   | `APP_URL` | *placeholder for now* — set in Step 3 |

4. [ ] Click **Apply** and wait for both services to build.
5. [ ] Copy the Web Service URL — it looks like
       `https://voice-agent-api.onrender.com`. **You'll need it in Step 2.**
6. [ ] Sanity check: open `https://voice-agent-api.onrender.com/health` — it
       should return `{"status":"ok"}`.

---

## 2. Deploy the frontend to Vercel

1. [ ] Vercel dashboard → **Add New → Project** → import this GitHub repo.
2. [ ] **Root Directory:** set to `frontend`.
3. [ ] Framework preset: **Vite** (Vercel usually auto-detects it).
4. [ ] Add an Environment Variable:

   | Variable | Value |
   |----------|-------|
   | `VITE_BACKEND_URL` | your Render Web Service URL from Step 1.5 (e.g. `https://voice-agent-api.onrender.com`) |

5. [ ] Deploy. Copy the resulting URL, e.g. `https://your-app.vercel.app`.

---

## 3. Wire the two together (the part everyone forgets)

The frontend URL and backend URL reference each other, so finish the loop:

1. [ ] In **Render → `voice-agent-api` → Environment**, set:

   | Variable | Value |
   |----------|-------|
   | `CORS_ALLOW_ORIGINS` | `https://your-app.vercel.app` (your exact Vercel URL, no trailing slash) |

2. [ ] In **Render → `voice-agent-worker` → Environment**, set:

   | Variable | Value |
   |----------|-------|
   | `APP_URL` | `https://your-app.vercel.app` (so the Discord hand-off links to the live app, not localhost) |

3. [ ] Save — Render redeploys the affected services automatically.

> To allow multiple origins (e.g. a preview URL too), make `CORS_ALLOW_ORIGINS`
> a comma-separated list: `https://your-app.vercel.app,https://staging.vercel.app`.

---

## 4. Test the live deployment

1. [ ] Open your Vercel URL, click **Talk to my AI**, allow the microphone.
2. [ ] Ask about a project — you should hear a grounded answer.
3. [ ] Ask something off-book (e.g. salary), then say **"yes"** when it offers to
       connect you — check your Discord channel for the 🔔 hand-off message.
4. [ ] If it's silent, check **Render → `voice-agent-worker` → Logs** for the
       per-stage pipeline logs (`track subscribed`, `USER transcribed`,
       `graph → tool call(s)`, etc.).

---

## Environment variable reference

Everything each host needs, at a glance:

| Variable | Render `api` | Render `worker` | Vercel | Source |
|----------|:---:|:---:|:---:|--------|
| `NVIDIA_API_KEY` | ✅ | ✅ | — | build.nvidia.com |
| `LIVEKIT_URL` | ✅ | ✅ | — | LiveKit Cloud |
| `LIVEKIT_API_KEY` | ✅ | ✅ | — | LiveKit Cloud |
| `LIVEKIT_API_SECRET` | ✅ | ✅ | — | LiveKit Cloud |
| `CORS_ALLOW_ORIGINS` | ✅ | — | — | your Vercel URL |
| `DISCORD_WEBHOOK_URL` | — | ✅ | — | Discord |
| `APP_URL` | — | ✅ | — | your Vercel URL |
| `VITE_BACKEND_URL` | — | — | ✅ | your Render `api` URL |

`PYTHON_VERSION` is set automatically by `render.yaml` — you don't touch it.

---

## Troubleshooting

- **CORS error in the browser console** → `CORS_ALLOW_ORIGINS` on
  `voice-agent-api` doesn't exactly match your Vercel origin (scheme + host, no
  trailing slash). Fix it and redeploy.
- **Token request fails / 502** → the free Render Web Service may be asleep;
  the first request after idle takes ~30–60s to wake. Retry.
- **Agent joins but stays silent** → check the worker logs. A `DEGRADED` line
  is a transient NVIDIA outage; retry, or set `NVIDIA_TTS_FUNCTION_ID` /
  `NVIDIA_STT_FUNCTION_ID` to another ACTIVE function (see `.env` comments).
- **Answers are ungrounded / generic** → the worker's ChromaDB didn't seed
  (transient NVIDIA blip at build time). Trigger a **Manual Deploy → Clear
  build cache & deploy** on `voice-agent-worker` to re-seed.
- **Worker never joins the room** → confirm the worker's `LIVEKIT_URL` /
  key / secret match the same LiveKit Cloud project the token server uses.

---

## Appendix: hosting the agent worker (always-on)

`render.yaml` deploys only the free **web** service. The agent worker
(`backend/agent_worker.py`) is a long-running process that connects out to
LiveKit Cloud and answers calls — it is **not** hosted by that Blueprint.

> **Why this matters:** if you run the worker locally
> (`python backend/agent_worker.py start`), your live site only responds while
> your machine is on and that command is running. For a CV link that works
> anytime, host the worker on an always-on box.

**What the worker needs, wherever it runs:**

- Python 3.12, `pip install -r requirements.txt`
- ChromaDB seeded once: `python backend/rag.py`
- Start command: `python backend/agent_worker.py start`
- These env vars:

  | Variable | Value |
  |----------|-------|
  | `NVIDIA_API_KEY` | build.nvidia.com |
  | `LIVEKIT_URL` | `wss://<your-project>.livekit.cloud` (same project as the API) |
  | `LIVEKIT_API_KEY` | LiveKit Cloud |
  | `LIVEKIT_API_SECRET` | LiveKit Cloud |
  | `DISCORD_WEBHOOK_URL` | your Discord webhook |
  | `APP_URL` | your Vercel URL (so the hand-off link points to the live app) |

### Option A — Render Background Worker (most consistent with this repo)

Render Workers need a **paid** plan (Starter, ~$7/mo). Add this service back to
`render.yaml` and re-sync the Blueprint (or create a Background Worker manually
in the dashboard with the same settings):

```yaml
  - type: worker
    name: voice-agent-worker
    runtime: python
    plan: starter
    branch: main
    buildCommand: pip install -r requirements.txt && cd backend && (python rag.py || echo "WARN seeding skipped")
    startCommand: cd backend && python agent_worker.py start
    envVars:
      - key: PYTHON_VERSION
        value: "3.12.10"
      - key: NVIDIA_API_KEY
        sync: false
      - key: LIVEKIT_URL
        sync: false
      - key: LIVEKIT_API_KEY
        sync: false
      - key: LIVEKIT_API_SECRET
        sync: false
      - key: DISCORD_WEBHOOK_URL
        sync: false
      - key: APP_URL
        sync: false
```

### Option B — Railway / Fly.io / Koyeb

Any host that runs a persistent process works:

- **Railway:** New Project → Deploy from GitHub → set **Root Directory** blank
  (repo root), **Start Command** `cd backend && python agent_worker.py start`,
  add the env vars above. (Seed once via a one-off `python backend/rag.py` or
  prepend it to the start command.)
- **Fly.io:** `fly launch` (no public ports needed — it's a worker), set secrets
  with `fly secrets set NVIDIA_API_KEY=… LIVEKIT_URL=… …`, start command as above.

### Option C — a small VPS (systemd)

On any Linux box, run it as a service so it restarts on reboot/crash:

```ini
# /etc/systemd/system/voice-agent-worker.service
[Unit]
Description=Voice Agent Worker
After=network-online.target

[Service]
WorkingDirectory=/opt/Voice-AI-Assistant/backend
EnvironmentFile=/opt/Voice-AI-Assistant/.env
ExecStart=/opt/Voice-AI-Assistant/.venv/bin/python agent_worker.py start
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now voice-agent-worker
journalctl -u voice-agent-worker -f   # tail logs
```

### Option D — LiveKit Cloud Agents (native)

LiveKit Cloud can host agents directly (no separate PaaS). If you prefer to keep
everything on LiveKit, see their Agents deployment docs
(<https://docs.livekit.io/agents/ops/deployment/>) — you deploy the same
`agent_worker.py` via the LiveKit CLI and it runs on their infra.

> Whichever you pick: the worker uses **automatic dispatch** (no `agent_name` in
> `WorkerOptions`), so it auto-joins any room in the LiveKit project. Just make
> sure its `LIVEKIT_*` env vars point at the **same** project as the token
> server, and only run **one** worker (or a couple) — multiple workers will
> load-balance jobs between them.
