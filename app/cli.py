import argparse
import secrets
import sys

from sqlalchemy.orm import Session

from app import models
from app.auth import hash_api_key
from app.db import SessionLocal


def create_client(
    session: Session,
    event_name: str | None,
    event_id: int | None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
):
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

    if webhook_secret and not webhook_url:
        print("Error: Cannot provide --webhook-secret without --webhook-url.")
        sys.exit(1)

    secret = None
    if webhook_url:
        secret = webhook_secret or secrets.token_urlsafe(32)

    client = models.Client(
        hashed_key=hashed_key,
        event_ids=[selected_event_id],
        webhook_url=webhook_url,
        webhook_secret=secret,
    )
    session.add(client)
    session.commit()
    session.refresh(client)

    print(f"Created Client with ID {client.id}")
    print(f"API Key: {raw_api_key}")
    if client.webhook_url:
        print(f"Webhook URL: {client.webhook_url}")
        print(f"Webhook Secret: {client.webhook_secret}")
    print("Store this key safely! It will not be shown again.")


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
    create_client_parser.add_argument(
        "--webhook-url",
        type=str,
        default=None,
        help="Outbound webhook notification URL",
    )
    create_client_parser.add_argument(
        "--webhook-secret",
        type=str,
        default=None,
        help="Shared secret for signing webhook notifications (auto-generated if omitted)",
    )

    args = parser.parse_args()

    if args.command == "admin" and args.subcommand == "create-client":
        db = SessionLocal()
        try:
            if args.webhook_url is not None or args.webhook_secret is not None:
                create_client(
                    db,
                    args.event_name,
                    args.event_id,
                    args.webhook_url,
                    args.webhook_secret,
                )
            else:
                create_client(db, args.event_name, args.event_id)
        finally:
            db.close()


if __name__ == "__main__":
    main()
