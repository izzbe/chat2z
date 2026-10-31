# chat2z

Multi-bot chat agent ("2z"). Python monolith: FastAPI backend + one static HTML frontend, served from a single Cloud Run service. Each "bot" is an agent implementing one shared interface (one per trading function). Agents are built on LangChain, run on a Vertex AI open model, and can emit artifacts (tables, charts, HTML...) shown in a side panel. Only `carmen` (a plain chatbot, no tools) exists so far.

## Layout
- `main.py` — FastAPI routes: auth, admin (user management), chats, artifacts, and `POST /api/chat/{bot_id}` (SSE; persists the user turn, streams, then saves the assistant turn even on client disconnect). SSE events are typed: `text | tool | artifact | error`. Every `/api/*` route except login requires a session.
- `agents/` — the agent framework (see "Agents" below). `main.py` knows only the `Agent` protocol and the registry.
- `auth.py` — argon2id passwords, server-side sessions (cookie holds a random token, only its sha256 is stored), rate limiters.
- `store.py` — all SQL. Every chat/artifact query is scoped by `user_id`; keep it that way.
- `db.py`, `settings.py` — pool + config. `artifacts.py` — GCS payloads. `manage.py` — admin CLI (migrate, create-user). `migrations/*.sql` — schema, applied by `manage.py migrate`.
- `mcp_tools.py` — NOT wired in. A standalone MCP client from the earlier design; kept in case tools arrive via MCP (wrap them as LangChain tools in an agent's `tools()`, e.g. with `langchain-mcp-adapters`). Delete it if unused.
- `static/index.html` — whole frontend (no build step): login, chat UI, admin modal, artifact panel. Artifact kinds render via the `renderers` map; add a kind = one function there. The client holds no durable state; history comes from the API.
- `tests/` — pytest; offline (agents use a scripted fake chat model). Run: `pip install -r requirements.txt -r requirements-dev.txt && pytest`.

## Agents
The interface is `agents.base.Agent`, a `typing.Protocol`: `id`, `name`, `description` and `async def run(ctx: AgentContext) -> AsyncIterator[Event]`. No base class required; `ctx.messages` is the conversation ending with the new user message, plus `user_id`/`username`/`chat_id`. Events (`text | tool | artifact | error`, built with `agents.events`) stream to the UI as SSE unchanged. `id` is the URL segment (`/api/chat/<id>`) and the DB `chats.bot_id`, so never change one once chats exist.

**Add an agent:** create `agents/library/<name>.py`:
```python
from agents.langchain_agent import LangChainAgent
from agents.registry import register

@register
class Momentum(LangChainAgent):
    id = "momentum"; name = "Momentum"; description = "..."
    system_prompt = "..."
    def tools(self): return [my_langchain_tool]   # optional
```
Every module in `agents/library/` is auto-imported at startup, so that is all: it appears in the picker. `LangChainAgent` streams the model and, if `tools()` is non-empty, runs a tool loop (max 6 steps). A tool can return a plain value, or `{"summary": ..., "artifacts": [{"kind","title","content"}]}` to show something in the side panel (the model only sees the summary). Override `build_model()` for another model (default: `agents.models.vertex_chat_model`, open models on Vertex via its OpenAI-compatible endpoint with a per-call token), or override `run()` / write a plain class for custom control flow (LangGraph etc.). Errors inside `run()` become an `error` event, never a broken stream. Shared pieces (`AgentContext` fields, event kinds) are the extension points; add to them rather than reaching for globals.

## Data model (Postgres, schema `chat2z`)
Lives in the shared Supabase database (the same one carmen uses; tables `bonds.*`, `public.*` are NOT ours — never touch them). Tables: `users`, `sessions`, `chats`, `messages` (parts jsonb), `artifacts` (metadata only). Artifact payloads are JSON docs in GCS at `gs://meteo-512021-chat2z-artifacts/artifacts/<user>/<chat>/<id>.json`; deleting a chat deletes its objects.
- Runtime connects as the restricted role `chat2z_app` (can only touch schema `chat2z`) via secret `chat2z-database-url`. The shared superuser URL (`database-url`) is used ONLY for migrations/admin CLI as `ADMIN_DATABASE_URL`.
- Supabase's pooler is transaction mode: no server-side prepared statements (`prepare_threshold=None`), no startup `options` — so all SQL is schema-qualified.
- Schema changes: add `migrations/NNN_name.sql` (use `{schema}` placeholder), run `ADMIN_DATABASE_URL=$(gcloud secrets versions access latest --secret database-url) python manage.py migrate`. Migrations are NOT run by deploys; run them before pushing code that needs them. New tables need grants to `chat2z_app` (default privileges in setup_infra.sh cover tables created by the same owner).
- Never test against the real schema: use `DB_SCHEMA=chat2z_test`, then `drop schema chat2z_test cascade`.

## Auth
Username + password, no self-signup. Admins create users in the UI (account menu -> Manage users); new users get a temp password and must change it on first login. Cookie `session`: HttpOnly, SameSite=Lax, Secure. Cross-origin POSTs are refused by an Origin check. Login is limited to 8 attempts / 10 min per ip+username (in memory, per instance). Admin accounts: `python manage.py create-user <name> --role admin --out .admin-credentials` (file is gitignored).

## Run locally
```
export DATABASE_URL=$(gcloud secrets versions access latest --secret chat2z-database-url)
DB_SCHEMA=chat2z_test ARTIFACT_BUCKET=meteo-512021-chat2z-artifacts COOKIE_SECURE=0 PROJECT=meteo-512021 uvicorn main:app --reload --port 8080
```
(needs `gcloud auth application-default login`; `COOKIE_SECURE=0` only for plain-http localhost; migrate `chat2z_test` first with the admin URL). 

## Model
`openai/gpt-oss-120b-maas` via Vertex AI's OpenAI-compatible endpoint (us-central1), through `langchain-openai`'s `ChatOpenAI`. Llama 4 Maverick / Qwen3 are NOT available in this project/region. Only `content` deltas are shown; `reasoning_content` is dropped.

## Deploy (automatic)
Push to `main` -> `.github/workflows/deploy.yml`: pytest -> docker build (gha layer cache) -> push to Artifact Registry -> `deploy-cloudrun`. PRs run tests + a build only. Typical push-to-live is a couple of minutes; check with `gh run watch`.
- GCP project `meteo-512021`, region `us-central1`, service `chat2z`, image repo `us-central1-docker.pkg.dev/meteo-512021/chat2z/chat2z`.
- Auth is Workload Identity Federation (pool `github`, provider `chat2z`, restricted to repo `izzbe/chat2z`) impersonating `chat2z-deployer@meteo-512021.iam.gserviceaccount.com` (roles: run.developer, artifactregistry.writer, serviceAccountUser on the runtime SA). No keys anywhere.
- Runtime identity: `chat2z-sa@meteo-512021.iam.gserviceaccount.com` (roles/aiplatform.user only).
- Service is public (`--allow-unauthenticated`), max 2 instances, in-memory per-IP rate limit (20/min) — the limiter is per-instance.
- Config lives in the workflow `env_vars` (non-secret). Secrets (`DATABASE_URL`; add others the same way) come from Secret Manager via the deploy step's `secrets:` — the repo is PUBLIC, never commit them.
- One-time infra (bucket, `chat2z_app` role, secrets): `scripts/setup_infra.sh`, run by a human with owner rights (needs the `chat2z` schema to exist first, so migrate before running it).
- Rollback: `gcloud run services update-traffic chat2z --region us-central1 --to-revisions=<rev>=100`.
- Note: `/healthz` is reserved by Cloud Run's frontend (404); don't use it.

## Conventions
- Keep it a monolith until there is a reason not to; no frontend build step.
- Comments only for non-obvious "why".
