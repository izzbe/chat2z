# chat2z

Multi-bot chat agent ("2z"). Python monolith: FastAPI backend + one static HTML frontend, served from a single Cloud Run service. Bots are LLM agents (Vertex AI open model) that call tools over MCP and can emit artifacts (tables, charts, HTML...) shown in a side panel.

## Layout
- `main.py` — FastAPI app. `GET /api/bots`, `POST /api/chat/{bot_id}` (SSE agent loop: model -> tool calls -> model, max 6 steps), `GET /` serves the UI. SSE events are typed: `text | tool | artifact | error`.
- `bots.py` — bot registry (`name`, `description`, `system_prompt`, optional `model`, optional `mcp_servers` allowlist). Add a bot = add an entry; it appears in the UI picker.
- `mcp_tools.py` — MCP client + artifact extraction. Artifact contract is documented in its module docstring. MCP servers come from the `MCP_SERVERS` env var (JSON list); empty = plain chat.
- `static/index.html` — whole frontend (no build step). Artifact kinds render via the `renderers` map; add a kind = one function there. Chats persist in browser localStorage.
- `tests/` — pytest (artifact parsing). Run: `pip install -r requirements.txt -r requirements-dev.txt && pytest`.

## Run locally
`PROJECT=meteo-512021 uvicorn main:app --reload --port 8080` (needs `gcloud auth application-default login`). Add `MCP_SERVERS='[{"name":"x","url":"http://localhost:9001/mcp"}]'` to test tools.

## Model
`openai/gpt-oss-120b-maas` via Vertex AI's OpenAI-compatible endpoint (us-central1). Llama 4 Maverick / Qwen3 are NOT available in this project/region. Only `content` deltas are shown; `reasoning_content` is dropped.

## Deploy (automatic)
Push to `main` -> `.github/workflows/deploy.yml`: pytest -> docker build (gha layer cache) -> push to Artifact Registry -> `deploy-cloudrun`. PRs run tests + a build only. Typical push-to-live is a couple of minutes; check with `gh run watch`.
- GCP project `meteo-512021`, region `us-central1`, service `chat2z`, image repo `us-central1-docker.pkg.dev/meteo-512021/chat2z/chat2z`.
- Auth is Workload Identity Federation (pool `github`, provider `chat2z`, restricted to repo `izzbe/chat2z`) impersonating `chat2z-deployer@meteo-512021.iam.gserviceaccount.com` (roles: run.developer, artifactregistry.writer, serviceAccountUser on the runtime SA). No keys anywhere.
- Runtime identity: `chat2z-sa@meteo-512021.iam.gserviceaccount.com` (roles/aiplatform.user only).
- Service is public (`--allow-unauthenticated`), max 2 instances, in-memory per-IP rate limit (20/min) — the limiter is per-instance.
- Config lives in the workflow `env_vars` (non-secret). Secrets (e.g. MCP server auth headers inside `MCP_SERVERS`) must go in Secret Manager and be mounted with `secrets:` in the deploy step — the repo is PUBLIC, never commit them.
- Rollback: `gcloud run services update-traffic chat2z --region us-central1 --to-revisions=<rev>=100`.
- Note: `/healthz` is reserved by Cloud Run's frontend (404); don't use it.

## Conventions
- Keep it a monolith until there is a reason not to; no frontend build step.
- Comments only for non-obvious "why".
