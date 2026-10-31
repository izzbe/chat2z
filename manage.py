"""Admin CLI. Uses ADMIN_DATABASE_URL (schema owner), never the runtime app's restricted role.

  ADMIN_DATABASE_URL=... python manage.py migrate
  ADMIN_DATABASE_URL=... python manage.py create-user admin --role admin --out .admin-credentials
"""
import argparse
import os
import re
import secrets
import sys
from pathlib import Path

import psycopg
from argon2 import PasswordHasher

import settings

SCHEMA = settings.SCHEMA


def connect():
    url = os.environ.get("ADMIN_DATABASE_URL", "")
    if not url:
        sys.exit("Set ADMIN_DATABASE_URL (the schema owner's connection string).")
    return psycopg.connect(re.sub(r"^postgres(ql)?(\+\w+)?://", "postgresql://", url), autocommit=True, prepare_threshold=None)


def migrate(_args):
    files = sorted(Path(__file__).parent.glob("migrations/*.sql"))
    with connect() as conn:
        conn.execute(f"create schema if not exists {SCHEMA}")
        conn.execute(
            f"create table if not exists {SCHEMA}.schema_migrations "
            f"(version text primary key, applied_at timestamptz not null default now())"
        )
        done = {r[0] for r in conn.execute(f"select version from {SCHEMA}.schema_migrations")}
        for f in files:
            if f.name in done:
                continue
            print(f"applying {f.name}")
            with conn.transaction():
                conn.execute(f.read_text().replace("{schema}", SCHEMA))
                conn.execute(f"insert into {SCHEMA}.schema_migrations (version) values (%s)", (f.name,))
    print("migrations up to date")


def create_user(args):
    username = args.username.lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,32}", username):
        sys.exit("username must be 3-32 chars of a-z 0-9 . _ -")
    password = args.password or secrets.token_urlsafe(18)
    with connect() as conn:
        if conn.execute(f"select 1 from {SCHEMA}.users where username = %s", (username,)).fetchone():
            sys.exit(f"user '{username}' already exists")
        conn.execute(
            f"insert into {SCHEMA}.users (username, display_name, password_hash, role, must_change_password) "
            f"values (%s, %s, %s, %s, false)",
            (username, args.display_name or username, PasswordHasher().hash(password), args.role),
        )
    if args.out:
        path = Path(args.out)
        path.write_text(f"username: {username}\npassword: {password}\nrole: {args.role}\n"
                        f"(change the password after first login; delete this file once saved)\n")
        path.chmod(0o600)
        print(f"created {args.role} '{username}'; credentials written to {path} (mode 600)")
    else:
        print(f"created {args.role} '{username}'\npassword: {password}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(required=True)
    sub.add_parser("migrate").set_defaults(fn=migrate)
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--role", choices=["admin", "user"], default="user")
    c.add_argument("--display-name")
    c.add_argument("--password")
    c.add_argument("--out")
    c.set_defaults(fn=create_user)
    a = p.parse_args()
    a.fn(a)
