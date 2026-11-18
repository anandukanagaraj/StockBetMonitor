"""Print an ADMIN_PASSWORD_HASH line ready to paste into .env.

    docker compose run --rm stockmonitor python docker/hash_password.py
"""
import getpass

from werkzeug.security import generate_password_hash

password = getpass.getpass("Admin password (min 10 chars): ")
if len(password) < 10:
    raise SystemExit("Password must be at least 10 characters.")
if password != getpass.getpass("Repeat password: "):
    raise SystemExit("Passwords do not match.")
# Single quotes stop docker compose from treating the "$" in the hash as variables.
print(f"ADMIN_PASSWORD_HASH='{generate_password_hash(password)}'")
