import os
import re

# The shared Supabase secret uses a SQLAlchemy-style scheme (postgresql+psycopg://); psycopg wants plain.
DATABASE_URL = re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql://", os.environ.get("DATABASE_URL", ""))
SCHEMA = os.environ.get("DB_SCHEMA", "chat2z")
if not re.fullmatch(r"[a-z_][a-z0-9_]*", SCHEMA):
    raise RuntimeError("DB_SCHEMA must be a plain lowercase identifier")

ARTIFACT_BUCKET = os.environ.get("ARTIFACT_BUCKET", "")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"  # set COOKIE_SECURE=0 only for plain-http dev
SESSION_DAYS = 14
MAX_MESSAGE_CHARS = 8000
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
