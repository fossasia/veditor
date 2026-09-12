import argparse
import getpass
import secrets
import sys

from sqlalchemy.orm import Session

from app import models
from app.auth import hash_api_key, lock_active_admins
from app.db import SessionLocal
from app.security import hash_password, is_valid_email


def create_client(session: Session, event_name: str | None, event_id: int | None):
    if not event_name and not event_id:
        print("Error: Must provide either --event-name or --event-id.")
        sys.exit(1)

    if event_name and event_id:
        print("Error: Cannot provide both --event-name and --event-id.")
        sys.exit(1)

    if event_name:
        event = models.Event(name=event_name)
        session.add(event)
        session.commit()
        session.refresh(event)
        selected_event_id = event.id
        print(f"Created Event '{event.name}' with ID {selected_event_id}")
    else:
        # Verify event_id exists
        event = session.query(models.Event).filter(models.Event.id == event_id).first()
        if not event:
            print(f"Error: Event with ID {event_id} does not exist.")
            sys.exit(1)
        selected_event_id = event.id
        print(f"Using existing Event '{event.name}' with ID {selected_event_id}")

    raw_api_key = secrets.token_urlsafe(32)
    hashed_key = hash_api_key(raw_api_key)

    client = models.Client(
        hashed_key=hashed_key,
        event_ids=[selected_event_id],
    )
    session.add(client)
    session.commit()
    session.refresh(client)

    print(f"Created Client with ID {client.id}")
    print(f"API Key: {raw_api_key}")
    print("Store this key safely! It will not be shown again.")


def create_admin(session: Session, email: str, password: str):
    clean_email = email.strip().lower() if email else ""
    if not is_valid_email(clean_email):
        print("Error: Invalid email format.")
        sys.exit(1)

    if not password or len(password) < 8:
        print("Error: Password must be at least 8 characters long.")
        sys.exit(1)

    existing = (
        session.query(models.User).filter(models.User.email == clean_email).first()
    )
    if existing:
        print(f"Error: User with email '{clean_email}' already exists.")
        sys.exit(1)

    user = models.User(
        email=clean_email,
        hashed_password=hash_password(password),
        role="admin",
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    print(f"Created admin user '{user.email}' with ID {user.id}")


def promote_user(session: Session, email: str, role: str):
    clean_email = email.strip().lower() if email else ""
    user = session.query(models.User).filter(models.User.email == clean_email).first()
    if not user:
        print(f"Error: User with email '{clean_email}' not found.")
        sys.exit(1)

    if role not in ("user", "organizer", "admin"):
        print(f"Error: Invalid role '{role}'. Must be one of: user, organizer, admin.")
        sys.exit(1)

    if user.role == "admin" and user.is_active and role != "admin":
        admin_ids = lock_active_admins(session)
        session.refresh(user)
        if user.role == "admin" and user.is_active and len(admin_ids) <= 1:
            print(
                "Error: Cannot demote user; operation would leave zero active administrators."
            )
            sys.exit(1)

    user.role = role
    session.commit()
    session.refresh(user)
    print(f"Updated user '{user.email}' role to '{user.role}'.")


def list_users(session: Session):
    users = session.query(models.User).order_by(models.User.id.asc()).all()
    if not users:
        print("No users found.")
        return

    header = f"{'ID':<6} {'Email':<32} {'Role':<12} {'Active':<8} {'Created At'}"
    print(header)
    print("-" * len(header))
    for u in users:
        created_str = (
            u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else "N/A"
        )
        print(f"{u.id:<6} {u.email:<32} {u.role:<12} {u.is_active!s:<8} {created_str}")


def main():
    parser = argparse.ArgumentParser(description="VEditor CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # `admin` command group
    admin_parser = subparsers.add_parser("admin", help="Admin commands")
    admin_subparsers = admin_parser.add_subparsers(dest="subcommand", required=True)

    # `admin create-client` command
    create_client_parser = admin_subparsers.add_parser(
        "create-client", help="Create a new client with an API key"
    )
    create_client_parser.add_argument(
        "--event-name",
        type=str,
        help="Name of the new event to create and scope the client to",
    )
    create_client_parser.add_argument(
        "--event-id", type=int, help="ID of an existing event to scope the client to"
    )

    # `admin create-admin` command
    create_admin_parser = admin_subparsers.add_parser(
        "create-admin", help="Create a new administrator user"
    )
    create_admin_parser.add_argument(
        "--email", type=str, required=True, help="Email address of the administrator"
    )

    # `admin promote-user` command
    promote_user_parser = admin_subparsers.add_parser(
        "promote-user", help="Promote or update a user's role"
    )
    promote_user_parser.add_argument(
        "--email", type=str, required=True, help="Email address of the user"
    )
    promote_user_parser.add_argument(
        "--role",
        type=str,
        required=True,
        choices=["user", "organizer", "admin"],
        help="Target role (user, organizer, admin)",
    )

    # `admin list-users` command
    admin_subparsers.add_parser("list-users", help="List all registered users")

    args = parser.parse_args()

    if args.command == "admin":
        db = SessionLocal()
        try:
            if args.subcommand == "create-client":
                create_client(db, args.event_name, args.event_id)
            elif args.subcommand == "create-admin":
                if not sys.stdin.isatty():
                    password = sys.stdin.readline().rstrip("\r\n")
                else:
                    password = getpass.getpass("Password: ")
                create_admin(db, args.email, password)
            elif args.subcommand == "promote-user":
                promote_user(db, args.email, args.role)
            elif args.subcommand == "list-users":
                list_users(db)
        finally:
            db.close()


if __name__ == "__main__":
    main()
