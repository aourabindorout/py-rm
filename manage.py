#!/usr/bin/env python3
import sys
import getpass
from db import init_db, create_or_update_user, delete_user, get_firestore_client


def print_help():
    print("py-rm User Management CLI (Firestore Native)")
    print("---------------------------------------------")
    print("Usage:")
    print("  python manage.py init-db")
    print("  python manage.py create-user [username] [password] [--superuser]")
    print("  python manage.py delete-user [username]")
    print("  python manage.py list-users")
    print()


def cmd_init_db():
    init_db()
    print("Google Cloud Firestore connection verified successfully.")


def cmd_create_user(args):
    init_db()
    is_superuser = False
    clean_args = []
    for arg in args:
        if arg == "--superuser":
            is_superuser = True
        else:
            clean_args.append(arg)

    if len(clean_args) >= 2:
        username = clean_args[0]
        password = clean_args[1]
    elif len(clean_args) == 1:
        username = clean_args[0]
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
        create_or_update_user(username, password, is_superuser=is_superuser)
        role_label = "Superuser" if is_superuser else "Student"
        print(f"Success: {role_label} '{username.strip().lower()}' created/updated in Firestore.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_delete_user(args):
    init_db()
    if len(args) >= 1:
        username = args[0].strip().lower()
    else:
        username = input("Enter username to delete: ").strip().lower()

    if not username:
        print("Error: Username cannot be empty.")
        sys.exit(1)

    confirm = input(f"Are you sure you want to permanently delete user '{username}' and their progress from Firestore? (y/N): ")
    if confirm.lower() != "y":
        print("Aborted.")
        return

    try:
        delete_user(username)
        print(f"Success: User '{username}' and all associated sessions/progress deleted from Firestore.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_list_users():
    init_db()
    fs = get_firestore_client()
    docs = list(fs.collection("users").stream())
    if not docs:
        print("No users found in Firestore.")
        return

    print(f"{'Username':<20} {'Role':<12} {'Created At'}")
    print("-" * 55)
    for doc in docs:
        data = doc.to_dict()
        uname = data.get("username", doc.id)
        role = "Superuser" if data.get("is_superuser") else "Student"
        print(f"{uname:<20} {role:<12} {data.get('created_at', '-')}")


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "init-db":
        cmd_init_db()
    elif cmd == "create-user":
        cmd_create_user(sys.argv[2:])
    elif cmd == "delete-user":
        cmd_delete_user(sys.argv[2:])
    elif cmd == "list-users":
        cmd_list_users()
    else:
        print(f"Unknown command: {cmd}")
        print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
