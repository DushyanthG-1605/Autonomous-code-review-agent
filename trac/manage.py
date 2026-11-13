import argparse
import hashlib
import json
import secrets
import sqlite3
import sys
import uuid

from trac.db import get_connection, init_db
from trac.models import now_iso


def create_user(github_login: str) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO users (github_login, display_name, created_at, public_id)
            VALUES (?, ?, ?, ?)
            """,
            (github_login, github_login, now_iso(), str(uuid.uuid4())),
        )
        project_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
            (
                project_id,
                f"personal-{cursor.lastrowid}",
                f"{github_login}'s workspace",
                now_iso(),
            ),
        )
        conn.execute(
            """
            INSERT INTO project_memberships (project_id, user_id, role, created_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (project_id, cursor.lastrowid, now_iso()),
        )
        conn.execute(
            """
            INSERT INTO devices
                (user_id, token_hash, label, created_at, project_id, scopes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cursor.lastrowid,
                token_hash,
                "CLI-created device",
                now_iso(),
                project_id,
                '["events:ingest","events:read"]',
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return token


def _create_project_device(
    conn, user_id: int, project_id: str, label: str, role: str
) -> str:
    token = secrets.token_urlsafe(32)
    scopes = ["events:read"]
    if role in {"owner", "member"}:
        scopes.append("events:ingest")
    conn.execute(
        """
        INSERT INTO devices (user_id, token_hash, label, created_at, project_id, scopes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            label,
            now_iso(),
            project_id,
            json.dumps(scopes),
        ),
    )
    return token


def create_project(slug: str, name: str, owner_login: str) -> str:
    conn = get_connection()
    try:
        owner = conn.execute(
            "SELECT id FROM users WHERE github_login = ?", (owner_login,)
        ).fetchone()
        if owner is None:
            raise ValueError(f"Unknown user: {owner_login}")
        project_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
            (project_id, slug, name, now_iso()),
        )
        conn.execute(
            """
            INSERT INTO project_memberships (project_id, user_id, role, created_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (project_id, owner["id"], now_iso()),
        )
        token = _create_project_device(
            conn, owner["id"], project_id, "Project owner device", "owner"
        )
        conn.commit()
        return token
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_project_member(slug: str, github_login: str, role: str = "member") -> str:
    conn = get_connection()
    try:
        project = conn.execute(
            "SELECT id FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        user = conn.execute(
            "SELECT id FROM users WHERE github_login = ?", (github_login,)
        ).fetchone()
        if project is None:
            raise ValueError(f"Unknown project: {slug}")
        if user is None:
            raise ValueError(f"Unknown user: {github_login}")
        conn.execute(
            """
            INSERT INTO project_memberships (project_id, user_id, role, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (project["id"], user["id"], role, now_iso()),
        )
        token = _create_project_device(
            conn, user["id"], project["id"], f"{slug} {role} device", role
        )
        conn.commit()
        return token
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    project_parser = subparsers.add_parser("create-project")
    project_parser.add_argument("slug")
    project_parser.add_argument("name")
    project_parser.add_argument("owner_login")
    member_parser = subparsers.add_parser("add-project-member")
    member_parser.add_argument("slug")
    member_parser.add_argument("github_login")
    member_parser.add_argument("--role", choices=("member", "viewer"), default="member")
    revoke_parser = subparsers.add_parser("revoke-device")
    revoke_parser.add_argument("id", type=int)
    args = parser.parse_args(argv)

    init_db()
    if args.command in {"create-user", "create-project", "add-project-member"}:
        try:
            if args.command == "create-user":
                token = create_user(args.github_login)
            elif args.command == "create-project":
                token = create_project(args.slug, args.name, args.owner_login)
            else:
                token = add_project_member(args.slug, args.github_login, args.role)
        except (sqlite3.IntegrityError, ValueError) as exc:
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
