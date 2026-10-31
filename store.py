"""All SQL for users, chats, messages and artifact metadata. Every chat/artifact query is scoped by user_id."""
import json

import db

S = db.S


# ---- users ----
async def get_user_by_username(username: str) -> dict | None:
    return await db.fetchone(f"select * from {S}.users where username = %s", username)


async def get_user(user_id) -> dict | None:
    return await db.fetchone(f"select * from {S}.users where id = %s", user_id)


async def list_users() -> list[dict]:
    return await db.fetch(
        f"select id, username, display_name, role, is_active, must_change_password, created_at, last_login_at "
        f"from {S}.users order by created_at"
    )


async def create_user(username, display_name, password_hash, role="user", must_change=True) -> dict:
    return await db.fetchone(
        f"insert into {S}.users (username, display_name, password_hash, role, must_change_password) "
        f"values (%s, %s, %s, %s, %s) returning id, username, display_name, role, is_active, must_change_password",
        username, display_name, password_hash, role, must_change,
    )


async def set_password(user_id, password_hash, must_change: bool, keep_token_hash: str | None = None) -> None:
    await db.execute(
        f"update {S}.users set password_hash = %s, must_change_password = %s where id = %s",
        password_hash, must_change, user_id,
    )
    # a password change signs out every other session
    await db.execute(
        f"delete from {S}.sessions where user_id = %s and (%s::text is null or token_hash <> %s)",
        user_id, keep_token_hash, keep_token_hash,
    )


async def update_user(user_id, **fields) -> dict | None:
    allowed = {"display_name", "role", "is_active"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if sets:
        cols = ", ".join(f"{k} = %s" for k in sets)
        await db.execute(f"update {S}.users set {cols} where id = %s", *sets.values(), user_id)
        if sets.get("is_active") is False:
            await db.execute(f"delete from {S}.sessions where user_id = %s", user_id)
    return await get_user(user_id)


async def count_active_admins() -> int:
    row = await db.fetchone(f"select count(*) as n from {S}.users where role = 'admin' and is_active")
    return row["n"]


async def touch_login(user_id) -> None:
    await db.execute(f"update {S}.users set last_login_at = now() where id = %s", user_id)


# ---- chats ----
async def ensure_chat(user_id, chat_id, bot_id: str, first_text: str) -> dict | None:
    """Create the chat if new; return it only if it belongs to this user (None otherwise)."""
    title = first_text.strip().replace("\n", " ")
    title = title[:40] + "…" if len(title) > 40 else title
    await db.execute(
        f"insert into {S}.chats (id, user_id, bot_id, title) values (%s, %s, %s, %s) on conflict (id) do nothing",
        chat_id, user_id, bot_id, title or "New chat",
    )
    return await db.fetchone(f"select * from {S}.chats where id = %s and user_id = %s", chat_id, user_id)


async def list_chats(user_id) -> list[dict]:
    return await db.fetch(
        f"select id, bot_id, title, updated_at from {S}.chats where user_id = %s order by updated_at desc limit 200",
        user_id,
    )


async def chat_detail(user_id, chat_id) -> dict | None:
    chat = await db.fetchone(
        f"select id, bot_id, title from {S}.chats where id = %s and user_id = %s", chat_id, user_id
    )
    if not chat:
        return None
    chat["messages"] = await db.fetch(
        f"select role, parts from {S}.messages where chat_id = %s order by id", chat_id
    )
    arts = await db.fetch(f"select id, kind, title from {S}.artifacts where chat_id = %s", chat_id)
    chat["artifacts"] = {a["id"]: a for a in arts}
    return chat


async def delete_chat(user_id, chat_id) -> list[str] | None:
    """Delete a chat; returns the GCS paths to clean up, or None if it isn't this user's."""
    chat = await db.fetchone(f"select id from {S}.chats where id = %s and user_id = %s", chat_id, user_id)
    if not chat:
        return None
    paths = [r["gcs_path"] for r in await db.fetch(f"select gcs_path from {S}.artifacts where chat_id = %s", chat_id)]
    await db.execute(f"delete from {S}.chats where id = %s", chat_id)
    return paths


# ---- messages ----
async def history(chat_id, limit: int = 20) -> list[dict]:
    """Prior turns as plain text for the model (tool/artifact parts are not replayed)."""
    rows = await db.fetch(
        f"select role, parts from (select id, role, parts from {S}.messages where chat_id = %s "
        f"order by id desc limit %s) m order by id",
        chat_id, limit,
    )
    out = []
    for r in rows:
        text = "".join(p.get("text", "") for p in r["parts"] if p.get("type") == "text")
        if text:
            out.append({"role": r["role"], "content": text})
    return out


async def add_message(chat_id, role: str, parts: list[dict]) -> None:
    async with db.tx() as conn:
        await conn.execute(
            f"insert into {S}.messages (chat_id, role, parts) values (%s, %s, %s::jsonb)",
            (chat_id, role, json.dumps(parts)),
        )
        await conn.execute(f"update {S}.chats set updated_at = now() where id = %s", (chat_id,))


# ---- artifacts ----
async def add_artifact(art_id, chat_id, user_id, kind, title, gcs_path, size) -> None:
    await db.execute(
        f"insert into {S}.artifacts (id, chat_id, user_id, kind, title, gcs_path, size_bytes) "
        f"values (%s, %s, %s, %s, %s, %s, %s) on conflict (id) do nothing",
        art_id, chat_id, user_id, kind, title, gcs_path, size,
    )


async def get_artifact(user_id, art_id) -> dict | None:
    return await db.fetchone(
        f"select id, kind, title, gcs_path from {S}.artifacts where id = %s and user_id = %s", art_id, user_id
    )
