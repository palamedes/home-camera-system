"""Command-line account recovery.

    python -m nvr.admin list-users
    python -m nvr.admin reset-password
    python -m nvr.admin add-user
    python -m nvr.admin delete-user <name>

Exists because the web UI is the only way in, and a forgotten password would
otherwise mean deleting the database — which would take the camera list and
the entire recording index with it. Physical access to the box is the
recovery route, which is the right trust boundary for a LAN appliance.

Passwords are read with getpass rather than taken as arguments, so they do not
land in shell history or in the process list where any other user could read
them from /proc.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time

from . import auth, config as config_module
from .db import Database

MIN_PASSWORD_LENGTH = 8


def _db() -> Database:
    return Database(config_module.load().db_path)


def _prompt_password(prompt: str = "New password: ") -> str:
    while True:
        password = getpass.getpass(prompt)
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Too short — needs at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.")
            continue
        return password


def _resolve_user(db: Database, username: str | None):
    """Pick the target account, defaulting to the only one if unambiguous."""
    users = db.query("SELECT * FROM users ORDER BY id")
    if not users:
        sys.exit("No accounts exist. Open the web UI to create the first one.")

    if username is None:
        if len(users) == 1:
            return users[0]
        names = ", ".join(u["username"] for u in users)
        sys.exit(f"Several accounts exist ({names}); name the one you mean.")

    user = db.user_by_name(username)
    if not user:
        sys.exit(f"No account named {username!r}.")
    return user


def cmd_list_users(args: argparse.Namespace) -> None:
    db = _db()
    users = db.query("SELECT * FROM users ORDER BY id")
    if not users:
        print("No accounts yet.")
        return
    for user in users:
        created = time.strftime("%Y-%m-%d", time.localtime(user["created_at"]))
        sessions = db.one(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ? AND expires_at > ?",
            (user["id"], int(time.time())),
        )
        active = sessions["n"] if sessions else 0
        print(f"{user['username']:<20} created {created}   {active} active session(s)")


def cmd_reset_password(args: argparse.Namespace) -> None:
    db = _db()
    user = _resolve_user(db, args.username)
    password = _prompt_password(f"New password for {user['username']}: ")

    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (auth.hash_password(password), user["id"]),
    )
    # Anyone already signed in stays signed in unless we clear their sessions.
    # If the password is being reset, assume the old one is untrusted.
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    print(f"Password updated for {user['username']}. All sessions signed out.")


def cmd_add_user(args: argparse.Namespace) -> None:
    db = _db()
    username = args.username or input("Username: ").strip()
    if len(username) < 3:
        sys.exit("Username must be at least 3 characters.")
    if db.user_by_name(username):
        sys.exit(f"An account named {username!r} already exists.")
    db.create_user(username, auth.hash_password(_prompt_password()))
    print(f"Created {username}.")


def cmd_delete_user(args: argparse.Namespace) -> None:
    db = _db()
    user = _resolve_user(db, args.username)
    if db.user_count() == 1:
        sys.exit(
            "That is the only account — deleting it would lock you out.\n"
            "Add another account first, or reset this one's password instead."
        )
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    print(f"Deleted {user['username']}.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m nvr.admin",
        description="Account recovery for Sentry. Requires shell access to the box.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-users", help="show accounts and active sessions").set_defaults(
        func=cmd_list_users
    )

    reset = sub.add_parser("reset-password", help="set a new password")
    reset.add_argument("username", nargs="?", help="defaults to the only account")
    reset.set_defaults(func=cmd_reset_password)

    add = sub.add_parser("add-user", help="create another account")
    add.add_argument("username", nargs="?")
    add.set_defaults(func=cmd_add_user)

    delete = sub.add_parser("delete-user", help="remove an account")
    delete.add_argument("username", nargs="?")
    delete.set_defaults(func=cmd_delete_user)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
