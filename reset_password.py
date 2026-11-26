"""Set a user's password directly in the database (recovery tool).

Laptop:  .venv\\Scripts\\python.exe reset_password.py admin
Docker:  docker compose exec stockmonitor python reset_password.py admin
"""
import argparse
import getpass

from werkzeug.security import generate_password_hash

from db import get_user_by_username, set_user_password
from services.schema import ensure_schema


def main():
    parser = argparse.ArgumentParser(description="Reset a StockMonitor user's password.")
    parser.add_argument("username", nargs="?", default="admin")
    args = parser.parse_args()

    ensure_schema()
    user = get_user_by_username(args.username)
    if not user:
        raise SystemExit(f"User not found: {args.username}")

    password = getpass.getpass(f"New password for {args.username} (10+ characters): ")
    if len(password) < 10:
        raise SystemExit("Password must be at least 10 characters.")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")

    set_user_password(user["id"], generate_password_hash(password))
    print(f"Password updated for {args.username}.")


if __name__ == "__main__":
    main()
