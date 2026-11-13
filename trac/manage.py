import argparse
import hashlib
import secrets
import sqlite3
import sys

from trac.db import get_connection, init_db
from trac.models import now_iso


def create_user(github_login: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (github_login, display_name, created_at) VALUES (?, ?, ?)",
            (github_login, github_login, now_iso()),
        )
        conn.execute(
            """
            INSERT INTO devices (user_id, token_hash, label, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (cursor.lastrowid, token_hash, "CLI-created device", now_iso()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return token


def revoke_device(device_id: int) -> bool:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (now_iso(), device_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trac.manage")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create-user")
    create_parser.add_argument("github_login")
    revoke_parser = subparsers.add_parser("revoke-device")
    revoke_parser.add_argument("id", type=int)
    args = parser.parse_args(argv)

    init_db()
    if args.command == "create-user":
        try:
            token = create_user(args.github_login)
        except sqlite3.IntegrityError as exc:
            print(f"Could not create user: {exc}", file=sys.stderr)
            return 1
        print(token)
        return 0

    if not revoke_device(args.id):
        print(f"Device {args.id} was not found or is already revoked", file=sys.stderr)
        return 1
    print(f"Revoked device {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
