import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import agents
import artifacts as gcs
import auth
import db
import settings
import store
from agents import AgentContext

log = logging.getLogger("chat2z")
STATIC = Path(__file__).parent / "static"
MAX_HISTORY = 20

chat_limiter = auth.RateLimiter(20, 60)       # per user
login_limiter = auth.RateLimiter(8, 600)      # per ip+username


@asynccontextmanager
async def lifespan(_):
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security(request: Request, call_next):
    # Cookies are SameSite=Lax; this additionally refuses cross-origin state-changing requests.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return Response("Cross-origin request refused.", status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def client_ip(request: Request) -> str:
    return (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()


def user_json(u: dict) -> dict:
    return {k: u[k] for k in ("id", "username", "display_name", "role", "must_change_password")}


# ---------------- auth ----------------
class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=200)


@app.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response):
    username = body.username.strip().lower()
    login_limiter.check(f"{client_ip(request)}:{username}", "Too many attempts. Try again in a few minutes.")
    user = await store.get_user_by_username(username)
    ok = auth.verify_password(user["password_hash"] if user else None, body.password)
    if not (ok and user["is_active"]):
        raise HTTPException(401, "Invalid username or password.")
    token = await auth.create_session(user["id"], client_ip(request), request.headers.get("user-agent", ""))
    await store.touch_login(user["id"])
    response.set_cookie(
        auth.COOKIE, token, max_age=settings.SESSION_DAYS * 86400, httponly=True,
        secure=settings.COOKIE_SECURE, samesite="lax", path="/",
    )
    return user_json(user)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE)
    if token:
        await auth.delete_session(token)
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: dict = Depends(auth.current_user)):
    return user_json(user)


class PasswordIn(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


@app.post("/api/auth/password")
async def change_password(body: PasswordIn, request: Request, user: dict = Depends(auth.current_user)):
    row = await store.get_user(user["id"])
    if not auth.verify_password(row["password_hash"], body.current):
        raise HTTPException(403, "Current password is incorrect.")
    auth.check_password_strength(body.new)
    token = request.cookies.get(auth.COOKIE)
    await store.set_password(user["id"], auth.hash_password(body.new), False, auth._token_hash(token))
    return {"ok": True}


# ---------------- admin ----------------
class NewUserIn(BaseModel):
    username: str = Field(max_length=32)
    display_name: str = Field(default="", max_length=64)
    role: str = "user"
    password: str | None = Field(default=None, max_length=200)


@app.get("/api/admin/users")
async def admin_list_users(_: dict = Depends(auth.require_admin)):
    return await store.list_users()


@app.post("/api/admin/users")
async def admin_create_user(body: NewUserIn, _: dict = Depends(auth.require_admin)):
    username = body.username.strip().lower()
    if not auth.USERNAME_RE.match(username):
        raise HTTPException(422, "Username: 3-32 characters, a-z 0-9 . _ - only.")
    if body.role not in ("admin", "user"):
        raise HTTPException(422, "Role must be admin or user.")
    if await store.get_user_by_username(username):
        raise HTTPException(409, "That username is taken.")
    password = body.password or auth.temp_password()
    auth.check_password_strength(password)
    user = await store.create_user(
        username, body.display_name.strip() or username, auth.hash_password(password), body.role, must_change=True
    )
    # the generated/entered password is returned once; only its hash is stored
    return {"user": user, "password": password}


class UpdateUserIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    role: str | None = None
    is_active: bool | None = None
    reset_password: bool = False


@app.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: uuid.UUID, body: UpdateUserIn, admin: dict = Depends(auth.require_admin)):
    target = await store.get_user(user_id)
    if not target:
        raise HTTPException(404, "No such user.")
    if body.role not in (None, "admin", "user"):
        raise HTTPException(422, "Role must be admin or user.")
    loses_admin = target["role"] == "admin" and target["is_active"] and (
        body.role == "user" or body.is_active is False
    )
    if loses_admin and (target["id"] == admin["id"] or await store.count_active_admins() <= 1):
        raise HTTPException(409, "You can't remove the last active admin (or yourself).")
    user = await store.update_user(user_id, display_name=body.display_name, role=body.role, is_active=body.is_active)
    password = None
    if body.reset_password:
        password = auth.temp_password()
        await store.set_password(user_id, auth.hash_password(password), True)
    return {"user": {k: user[k] for k in ("id", "username", "display_name", "role", "is_active")}, "password": password}


# ---------------- bots / chats ----------------
@app.get("/api/bots")
def bots(_: dict = Depends(auth.current_user)):
    return [{"id": a.id, "name": a.name, "description": a.description} for a in agents.all_agents()]


@app.get("/api/chats")
async def list_chats(user: dict = Depends(auth.current_user)):
    return await store.list_chats(user["id"])


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: uuid.UUID, user: dict = Depends(auth.current_user)):
    chat = await store.chat_detail(user["id"], chat_id)
    if not chat:
        raise HTTPException(404, "No such chat.")
    return chat


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: uuid.UUID, user: dict = Depends(auth.current_user)):
    paths = await store.delete_chat(user["id"], chat_id)
    if paths is None:
        raise HTTPException(404, "No such chat.")
    if paths:
        asyncio.create_task(_cleanup(paths))
    return {"ok": True}


async def _cleanup(paths: list[str]) -> None:
    try:
        await asyncio.to_thread(gcs.delete, paths)
    except Exception:
        log.exception("artifact cleanup failed")


@app.get("/api/artifacts/{art_id}")
async def get_artifact(art_id: str, user: dict = Depends(auth.current_user)):
    meta = await store.get_artifact(user["id"], art_id)
    if not meta:
        raise HTTPException(404, "No such artifact.")
    try:
        doc = await asyncio.to_thread(gcs.get, meta["gcs_path"])
    except Exception:
        log.exception("artifact fetch failed")
        raise HTTPException(502, "Artifact is unavailable.")
    return {"id": meta["id"], "kind": meta["kind"], "title": meta["title"], "content": doc.get("content")}


class ChatIn(BaseModel):
    chat_id: uuid.UUID
    text: str = Field(min_length=1, max_length=settings.MAX_MESSAGE_CHARS)


def sse(**kw) -> str:
    return f"data: {json.dumps(kw)}\n\n"


@app.post("/api/chat/{bot_id}")
async def chat(bot_id: str, body: ChatIn, user: dict = Depends(auth.current_user)):
    agent = agents.get(bot_id)  # the URL/DB "bot_id" is the agent's id
    if agent is None:
        raise HTTPException(404, "Unknown bot")
    chat_limiter.check(str(user["id"]))
    row = await store.ensure_chat(user["id"], body.chat_id, bot_id, body.text)
    if row is None or row["bot_id"] != bot_id:
        raise HTTPException(404, "No such chat.")

    prior = await store.history(row["id"], MAX_HISTORY)
    await store.add_message(row["id"], "user", [{"type": "text", "text": body.text}])
    ctx = AgentContext(
        messages=[*prior, {"role": "user", "content": body.text}],
        user_id=str(user["id"]), username=user["username"], chat_id=str(row["id"]),
    )

    async def events():
        parts: list[dict] = []

        async def save():
            for p in parts:
                if p["type"] == "tool" and p["status"] == "running":
                    p["status"] = "error"
            if parts:
                await store.add_message(row["id"], "assistant", parts)

        try:
            async for ev in agent.run(ctx):
                kind = ev["type"]
                if kind == "text":
                    if parts and parts[-1]["type"] == "text":
                        parts[-1]["text"] += ev["text"]
                    else:
                        parts.append({"type": "text", "text": ev["text"]})
                elif kind == "tool":
                    p = next((x for x in parts if x["type"] == "tool" and x["id"] == ev["id"]), None)
                    if p:
                        p["status"] = ev["status"]
                    else:
                        parts.append({"type": "tool", "id": ev["id"], "name": ev["name"], "status": ev["status"]})
                elif kind == "artifact":
                    path = f"artifacts/{user['id']}/{row['id']}/{ev['id']}.json"
                    try:
                        size = await asyncio.to_thread(
                            gcs.put, path, {"kind": ev["kind"], "title": ev["title"], "content": ev["content"]}
                        )
                        await store.add_artifact(ev["id"], row["id"], user["id"], ev["kind"], ev["title"], path, size)
                    except Exception:
                        log.exception("could not persist artifact")
                    parts.append({"type": "artifact", "id": ev["id"]})
                elif kind == "error":
                    parts.append({"type": "error", "text": ev["error"]})
                yield sse(**ev)
        finally:
            # runs on completion and on client disconnect, so partial replies are kept too
            await asyncio.shield(save())

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
