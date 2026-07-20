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
