"""Admin tool for users (run on your machine; there is deliberately no user-admin API).

    python manage_users.py list
    python manage_users.py add USERNAME ROLE "Display Name"   # prompts for a temporary password
    python manage_users.py set-password USERNAME              # prompts; user must change it at next login
    python manage_users.py set-role USERNAME ROLE
    python manage_users.py disable USERNAME                   # login disabled immediately
"""
import getpass
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from app.auth import DISABLED_HASH, hash_password  # noqa: E402
from app.roles import ALL_ROLES  # noqa: E402

load_dotenv(Path(__file__).parent / ".env")


def prompt_password():
    pw = getpass.getpass("Temporary password (min 10 chars): ")
    if len(pw) < 10:
        sys.exit("Password too short.")
    if getpass.getpass("Repeat: ") != pw:
        sys.exit("Passwords do not match.")
    return pw


def check_role(role):
    if role not in ALL_ROLES:
        sys.exit(f"Unknown role '{role}'. Valid: {', '.join(sorted(ALL_ROLES))}")


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=15)
    try:
        with conn, conn.cursor() as cur:
            if cmd == "list":
                cur.execute("SELECT username, role, display_name, "
                            "CASE WHEN password_hash = %s THEN 'DISABLED' "
                            "WHEN must_change_password THEN 'must change password' ELSE 'active' END "
                            "FROM dim_user ORDER BY role, username", (DISABLED_HASH,))
                for r in cur.fetchall():
                    print("%-18s %-16s %-22s %s" % r)
            elif cmd == "add" and len(args) == 3:
                check_role(args[1])
                cur.execute("INSERT INTO dim_user (username, password_hash, role, display_name, "
                            "must_change_password) VALUES (%s, %s, %s, %s, true)",
                            (args[0].strip().lower(), hash_password(prompt_password()), args[1], args[2]))
                print("Created.")
            elif cmd == "set-password" and len(args) == 1:
                cur.execute("UPDATE dim_user SET password_hash = %s, must_change_password = true "
                            "WHERE lower(username) = lower(%s)", (hash_password(prompt_password()), args[0]))
                print("Updated." if cur.rowcount else "No such user.")
            elif cmd == "set-role" and len(args) == 2:
                check_role(args[1])
                cur.execute("UPDATE dim_user SET role = %s WHERE lower(username) = lower(%s)", (args[1], args[0]))
                print("Updated." if cur.rowcount else "No such user.")
            elif cmd == "disable" and len(args) == 1:
                cur.execute("UPDATE dim_user SET password_hash = %s WHERE lower(username) = lower(%s)",
                            (DISABLED_HASH, args[0]))
                print("Disabled." if cur.rowcount else "No such user.")
            else:
                sys.exit(__doc__)
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
