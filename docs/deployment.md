# Deployment plan: Vercel (frontend) + Railway (backend)

## Target shape

```text
Browser ──► Vercel (static/ : index.html, app.js, styles.css)
               │  rewrite /api/*  (server-side proxy, same origin for the browser)
               ▼
           Railway service: FastAPI (uvicorn)  ──► Groq API
               │
               └── Railway volume /data  (assistant.db)
```

Why this shape:

- `static/app.js` calls relative paths (`/api/schemes`, `/api/threads`, ...). A Vercel rewrite keeps those calls same-origin, so **no JavaScript changes and no CORS setup** are needed.
- SQLite needs a persistent disk, so the backend runs as **one Railway service with one volume and one replica**. Do not scale replicas; a volume attaches to a single instance.
- The embedding model is baked into the Docker image, so cold starts do not download it.

## Files in the repository

| File | Purpose |
| --- | --- |
| `Dockerfile` | Backend image: CPU-only PyTorch, MiniLM baked in, offline Hugging Face at runtime |
| `scripts/start.sh` | Entrypoint: runs the first ingest in the background when the volume is empty, then starts uvicorn on `$PORT` |
| `app/bootstrap.py` | `python -m app.bootstrap` exits 0 when the corpus has no documents (used by `start.sh`) |
| `.dockerignore` | Keeps `.venv`, `data/`, `.env`, tests and docs out of the image |
| `railway.json` | Dockerfile build, 1 replica, `/api/health` health check, restart on failure |
| `vercel.json` | Serves `static/` with no build and proxies `/api/*` to Railway |
| `.vercelignore` | Uploads only `static/` and `vercel.json`, so Vercel never tries to build the Python app |
| `scripts/set-railway-domain.sh` | Writes the Railway domain into `vercel.json` |

FastAPI also keeps serving `static/` at `/`, which is useful for checking the Railway URL directly.

## Phase 0: pre-launch hardening (done in code)

1. **Thread privacy.** Each browser gets an anonymous `mf_client` cookie (HttpOnly, SameSite=Lax, Secure over HTTPS). Threads are stored with that id, `GET /api/threads` lists only that browser's threads, and another browser's thread returns 404. The cookie passes through the Vercel rewrite because the browser only sees the Vercel origin. Threads created before this change have no owner and are hidden.
2. **Rate limiting.** `POST /api/threads/{id}/messages` returns 429 with `Retry-After` above `RATE_LIMIT_PER_MINUTE` (default 10) per client IP, or `RATE_LIMIT_GLOBAL_PER_MINUTE` (default 200) in total. The limiter is in process memory, which is correct for a single replica. Still set a spending limit in the Groq console.
3. **Region.** Pick Railway's Asia region (Singapore) for users in India.

## Phase A: prepare the repository

1. The files above live on branch `deploy/vercel-railway`.
2. Optional, if Docker is installed: build and run the image locally:
   ```text
   docker build -t mf-faq .
   docker run -p 8000:8000 -v %cd%\data:/data -e GROQ_API_KEY=... mf-faq
   ```
   Check `http://127.0.0.1:8000/api/health`. With an empty `data/` the first ingest runs on its own; `documents` reaches 17 once it finishes.
3. Run `pytest` and confirm it passes.
4. Merge to `main` and push to GitHub.

## Phase B: Railway (backend first; the frontend needs its URL)

1. Railway → **New Project** → **Deploy from GitHub repo** → select this repo, branch `main`. Railway picks up `railway.json` and builds the Dockerfile.
2. **Volume:** service → **Volumes** → add a volume mounted at `/data`.
3. **Variables:**

   | Variable | Value |
   | --- | --- |
   | `GROQ_API_KEY` | your Groq key (mark as secret) |
   | `DATABASE_PATH` | `/data/assistant.db` (already the image default) |
   | `GROQ_MODEL` | leave unset for `llama-3.3-70b-versatile` |
   | `RATE_LIMIT_PER_MINUTE` | optional, default `10` per client IP |
   | `RATE_LIMIT_GLOBAL_PER_MINUTE` | optional, default `200` in total |
   | `AUTO_INGEST` | optional, `0` turns off the first-boot ingest |

   `PORT` is injected by Railway; do not set it.
4. **Settings:** region Singapore, replicas = 1. Give the service at least 1 GB RAM (torch + MiniLM).
5. **Networking:** **Generate Domain**. Note the `*.up.railway.app` domain.
6. **First ingest** happens automatically: on boot, `start.sh` sees an empty volume and runs `python -m app.ingest` in the background while the server is already healthy. Watch the deploy logs for the ingest summary. The database lives on the volume, so later deploys skip this. To run it by hand: `railway link`, `railway ssh`, `python -m app.ingest`.
7. Verify: `https://<railway-domain>/api/health` returns `"ok": true, "schemes": 10, "documents": 17`, and `https://<railway-domain>/` loads the UI.

## Phase C: Vercel (frontend)

1. Put the Railway domain into `vercel.json` (`sh scripts/set-railway-domain.sh <name>.up.railway.app`, or replace `RAILWAY_DOMAIN` by hand) and push to `main`.
2. Vercel → **Add New Project** → import the repo. Framework preset **Other**, root directory = repository root. `vercel.json` sets the output directory to `static` with no build, and `.vercelignore` keeps the Python code out of the upload.
3. No environment variables are needed on Vercel; the Groq key stays on Railway only.
4. Deploy, then verify on the `*.vercel.app` URL:
   - Page loads, the scheme list and **View on Groww** link work.
   - DevTools → Network: `/api/*` requests go to the Vercel origin and return 200.
   - Ask the three example questions; each reply has one citation and a `Last updated from sources` footer.
   - A message containing a fake PAN is stored as `[removed: personal data]`.
   - A private window shows an empty sidebar (threads are per browser).
5. Optional: add a custom domain in Vercel → **Domains**.

## Phase D: operations

| Task | How |
| --- | --- |
| Deploy | Push to `main`. Both platforms auto-deploy. Railway restarts only after `/api/health` passes. |
| Previews | Vercel builds a preview per PR; they hit the same production backend, so test write-heavy changes carefully. |
| Refresh sources | `railway ssh` → `python -m app.ingest`. Run after UTI publishes a new factsheet (monthly). The footer date updates only on ingest. |
| Backups | Periodically copy `/data/assistant.db` out (`railway ssh` + `sqlite3 .backup`), or enable Railway volume backups. |
| Logs | Railway service logs (uvicorn) and Vercel request logs. The Groq key is never logged by the app. |
| Rollback | Railway → Deployments → **Redeploy** a previous build; Vercel → Deployments → **Promote** a previous build. The volume is not rolled back. |
| Rotate Groq key | Update `GROQ_API_KEY` in Railway; the service redeploys. |

## Known constraints

- **Single instance.** SQLite on a volume means one replica and a short restart gap during deploys. Moving to Railway Postgres would be needed to scale out.
- **First answer after a deploy is slower** while the model loads into memory (it is on disk already, not downloaded).
- **Proxy timeouts.** Vercel external rewrites have a response time limit; a slow Groq call can surface as a 504 from Vercel. Groq replies are normally well under that limit.
