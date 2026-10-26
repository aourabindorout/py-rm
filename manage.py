#!/usr/bin/env python3
import sys
import getpass
from db import init_db, create_or_update_user, get_connection


def print_help():
    print("py-rm User Management CLI")
    print("-------------------------")
    print("Usage:")
    print("  python manage.py init-db")
    print("  python manage.py create-user [username] [password]")
    print("  python manage.py list-users")
    print()


def cmd_init_db():
    init_db()
    print("Database initialized successfully.")


def cmd_create_user(args):
    init_db()
    if len(args) >= 2:
        username = args[0]
        password = args[1]
    elif len(args) == 1:
        username = args[0]
        password = getpass.getpass("Enter password: ")
        password_confirm = getpass.getpass("Confirm password: ")
        if password != password_confirm:
            print("Error: Passwords do not match.")
            sys.exit(1)
    else:
        username = input("Enter username: ")
        password = getpass.getpass("Enter password: ")
        password_confirm = getpass.getpass("Confirm password: ")
        if password != password_confirm:
            print("Error: Passwords do not match.")
            sys.exit(1)

    try:
        create_or_update_user(username, password)
        print(f"Success: User '{username.strip().lower()}' created/updated.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_list_users():
    init_db()
    with get_connection() as conn:
        cursor = conn.execute("SELECT id, username, created_at FROM users ORDER BY id ASC")
        rows = cursor.fetchall()
        if not rows:
            print("No users found in database.")
            return

        print(f"{'ID':<4} {'Username':<20} {'Created At'}")
        print("-" * 45)
        for row in rows:
            print(f"{row['id']:<4} {row['username']:<20} {row['created_at']}")


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "init-db":
        cmd_init_db()
    elif cmd == "create-user":
        cmd_create_user(sys.argv[2:])
    elif cmd == "list-users":
        cmd_list_users()
    else:
        print(f"Unknown command: {cmd}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

