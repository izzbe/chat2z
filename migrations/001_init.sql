-- {schema} holds everything chat2z owns; nothing here touches public/bonds/etc.
create schema if not exists {schema};

create table if not exists {schema}.users (
    id uuid primary key default gen_random_uuid(),
    username text not null unique check (username ~ '^[a-z0-9._-]{3,32}$'),
    display_name text not null,
    password_hash text not null,
    role text not null default 'user' check (role in ('admin', 'user')),
    is_active boolean not null default true,
    must_change_password boolean not null default false,
    created_at timestamptz not null default now(),
    last_login_at timestamptz
);

-- Server-side sessions: the cookie holds a random token, only its sha256 is stored, so they can be revoked.
create table if not exists {schema}.sessions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references {schema}.users (id) on delete cascade,
    token_hash text not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    ip text,
    user_agent text
);
create index if not exists sessions_user_idx on {schema}.sessions (user_id);
create index if not exists sessions_expires_idx on {schema}.sessions (expires_at);

create table if not exists {schema}.chats (
    id uuid primary key,
    user_id uuid not null references {schema}.users (id) on delete cascade,
    bot_id text not null,
    title text not null default 'New chat',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists chats_user_idx on {schema}.chats (user_id, updated_at desc);

-- parts: [{type: text|tool|artifact|error, ...}] exactly as the UI renders them.
create table if not exists {schema}.messages (
    id bigint generated always as identity primary key,
    chat_id uuid not null references {schema}.chats (id) on delete cascade,
    role text not null check (role in ('user', 'assistant')),
    parts jsonb not null,
    created_at timestamptz not null default now()
);
create index if not exists messages_chat_idx on {schema}.messages (chat_id, id);

-- Metadata only; the payload lives in GCS at gcs_path.
create table if not exists {schema}.artifacts (
    id text primary key,
    chat_id uuid not null references {schema}.chats (id) on delete cascade,
    user_id uuid not null references {schema}.users (id) on delete cascade,
    kind text not null,
    title text not null,
    gcs_path text not null,
    size_bytes bigint not null,
    created_at timestamptz not null default now()
);
create index if not exists artifacts_chat_idx on {schema}.artifacts (chat_id);

-- Supabase's API roles must never reach these tables.
do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        execute 'revoke all on schema {schema} from anon';
        execute 'revoke all on all tables in schema {schema} from anon';
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        execute 'revoke all on schema {schema} from authenticated';
        execute 'revoke all on all tables in schema {schema} from authenticated';
    end if;
end $$;
