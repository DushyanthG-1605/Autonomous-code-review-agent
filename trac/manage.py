import argparse
import hashlib
import json
import re
import secrets
import sqlite3
import sys
import uuid
from typing import Optional

from trac.db import get_connection, init_db
from trac.models import now_iso


def create_user(
    github_login: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO users
                (
                    github_login, github_username, display_name, email,
                    created_at, public_id
                )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                github_login,
                github_login,
                display_name or github_login,
                email,
                now_iso(),
                str(uuid.uuid4()),
            ),
        )
        project_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO projects
                (id, slug, name, project_key, project_head_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                f"personal-{cursor.lastrowid}",
                f"{github_login}'s workspace",
                f"personal-{cursor.lastrowid}",
                cursor.lastrowid,
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
            """
            INSERT INTO projects
                (id, slug, name, project_key, project_head_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, slug, name, slug, owner["id"], now_iso()),
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


def map_github_repository(slug: str, repository: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository):
        raise ValueError("Repository must use the owner/repository format")
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT id FROM projects WHERE slug = ?", (slug,)
        ).fetchone()
        if project is None:
            raise ValueError(f"Unknown project: {slug}")
        integration_login = f"trac-github-{project['id'].replace('-', '')}"
        actor = conn.execute(
            "SELECT id FROM users WHERE github_login = ?", (integration_login,)
        ).fetchone()
        if actor is None:
            actor_id = conn.execute(
                """
                INSERT INTO users
                    (github_login, display_name, created_at, public_id, role)
                VALUES (?, 'GitHub Integration', ?, ?, 'integration')
                """,
                (integration_login, now_iso(), str(uuid.uuid4())),
            ).lastrowid
        else:
            actor_id = actor["id"]
        conn.execute(
            """
            INSERT INTO github_repository_mappings
                (repository, project_id, actor_user_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (repository, project["id"], actor_id, now_iso()),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ValueError("Repository is already mapped") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def unmap_github_repository(repository: str) -> bool:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM github_repository_mappings WHERE repository = ?",
            (repository,),
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
    create_parser.add_argument("--display-name")
    create_parser.add_argument("--email")
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
    map_repo_parser = subparsers.add_parser("map-github-repo")
    map_repo_parser.add_argument("slug")
    map_repo_parser.add_argument("repository")
    unmap_repo_parser = subparsers.add_parser("unmap-github-repo")
    unmap_repo_parser.add_argument("repository")
    args = parser.parse_args(argv)

    init_db()
    if args.command in {
        "create-user",
        "create-project",
        "add-project-member",
        "map-github-repo",
    }:
        try:
            if args.command == "create-user":
                token = create_user(
                    args.github_login, args.display_name, args.email
                )
            elif args.command == "create-project":
                token = create_project(args.slug, args.name, args.owner_login)
            elif args.command == "add-project-member":
                token = add_project_member(args.slug, args.github_login, args.role)
            else:
                map_github_repository(args.slug, args.repository)
        except (sqlite3.IntegrityError, ValueError) as exc:
            print(f"Could not update TRAC configuration: {exc}", file=sys.stderr)
            return 1
        if args.command == "map-github-repo":
            print(f"Mapped {args.repository} to project {args.slug}")
        else:
            print(token)
        return 0

    if args.command == "unmap-github-repo":
        if not unmap_github_repository(args.repository):
            print(f"Repository mapping not found: {args.repository}", file=sys.stderr)
            return 1
        print(f"Removed mapping for {args.repository}")
        return 0

    if not revoke_device(args.id):
        print(f"Device {args.id} was not found or is already revoked", file=sys.stderr)
        return 1
    print(f"Revoked device {args.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
