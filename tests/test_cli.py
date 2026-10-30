import io
import sys
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.auth import hash_api_key
from app.cli import create_admin, create_client, list_users, main, promote_user
from app.security import hash_password, verify_password
from tests.conftest import TestSessionLocal


def _session_factory(db_session: Session) -> Callable[[], Session]:
    """Return a factory of CLI sessions bound to the test connection."""

    def factory() -> Session:
        return TestSessionLocal(bind=db_session.get_bind())

    return factory


def _create_user(
    db: Session,
    *,
    email: str,
    role: str = "user",
    is_active: bool = True,
    password: str = "validpassword123",
) -> models.User:
    user = models.User(
        email=email,
        hashed_password=hash_password(password),
        role=role,
        is_active=is_active,
    )
    db.add(user)
    db.commit()
    return user


def _printed_api_key(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("API Key: "):
            return line.removeprefix("API Key: ")
    raise AssertionError(f"API key not found in output: {output!r}")


def test_create_client_with_new_event(
    db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    create_client(db_session, event_name="New Event", event_id=None)

    db_session.expire_all()
    event = db_session.scalars(select(models.Event)).one()
    assert event.name == "New Event"

    client = db_session.scalars(select(models.Client)).one()
    assert client.event_ids == [event.id]
    raw_key = _printed_api_key(capsys.readouterr().out)
    assert client.hashed_key == hash_api_key(raw_key)


def test_create_client_with_existing_event(
    db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    event = models.Event(name="Existing")
    db_session.add(event)
    db_session.commit()

    create_client(db_session, event_name=None, event_id=event.id)

    db_session.expire_all()
    client = db_session.scalars(select(models.Client)).one()
    assert client.event_ids == [event.id]
    raw_key = _printed_api_key(capsys.readouterr().out)
    assert client.hashed_key == hash_api_key(raw_key)


def test_create_client_missing_args(db_session: Session) -> None:
    with pytest.raises(SystemExit) as excinfo:
        create_client(db_session, event_name=None, event_id=None)
    assert excinfo.value.code == 1


def test_create_client_conflicting_args(db_session: Session) -> None:
    with pytest.raises(SystemExit) as excinfo:
        create_client(db_session, event_name="A", event_id=1)
    assert excinfo.value.code == 1


def test_create_client_invalid_event_id(db_session: Session) -> None:
    with pytest.raises(SystemExit) as excinfo:
        create_client(db_session, event_name=None, event_id=99)
    assert excinfo.value.code == 1
    assert db_session.scalars(select(models.Client)).all() == []


def test_cli_main_create_client(
    db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    test_args = ["veditor", "admin", "create-client", "--event-name", "Test"]
    with (
        patch("app.cli.SessionLocal", side_effect=_session_factory(db_session)),
        patch.object(sys, "argv", test_args),
    ):
        main()

    db_session.expire_all()
    event = db_session.scalars(select(models.Event)).one()
    assert event.name == "Test"
    client = db_session.scalars(select(models.Client)).one()
    assert client.event_ids == [event.id]
    assert "Created Client" in capsys.readouterr().out


def test_create_admin_success(db_session: Session) -> None:
    create_admin(db_session, email="Admin@Example.COM", password="validpassword123")

    db_session.expire_all()
    user = db_session.scalars(select(models.User)).one()
    assert user.email == "admin@example.com"
    assert user.role == "admin"
    assert user.is_active is True
    assert verify_password("validpassword123", user.hashed_password)


def test_create_admin_invalid_email(db_session: Session) -> None:
    with pytest.raises(SystemExit) as exc:
        create_admin(db_session, email="invalid-email", password="validpassword123")
    assert exc.value.code == 1
    assert db_session.scalars(select(models.User)).all() == []


def test_create_admin_short_password(db_session: Session) -> None:
    with pytest.raises(SystemExit) as exc:
        create_admin(db_session, email="admin@example.com", password="short")
    assert exc.value.code == 1
    assert db_session.scalars(select(models.User)).all() == []


def test_create_admin_duplicate_email(db_session: Session) -> None:
    _create_user(db_session, email="admin@example.com")

    with pytest.raises(SystemExit) as exc:
        create_admin(db_session, email="admin@example.com", password="validpassword123")
    assert exc.value.code == 1
    assert db_session.scalars(select(models.User)).all() != []


def test_promote_user_success(db_session: Session) -> None:
    user = _create_user(db_session, email="user@example.com", role="user")

    promote_user(db_session, email="user@example.com", role="organizer")

    db_session.expire_all()
    reloaded = db_session.get(models.User, user.id)
    assert reloaded is not None
    assert reloaded.role == "organizer"


def test_promote_user_not_found(db_session: Session) -> None:
    with pytest.raises(SystemExit) as exc:
        promote_user(db_session, email="missing@example.com", role="admin")
    assert exc.value.code == 1


def test_promote_user_invalid_role(db_session: Session) -> None:
    user = _create_user(db_session, email="user@example.com", role="user")

    with pytest.raises(SystemExit) as exc:
        promote_user(db_session, email="user@example.com", role="superhero")
    assert exc.value.code == 1
    db_session.expire_all()
    reloaded = db_session.get(models.User, user.id)
    assert reloaded is not None
    assert reloaded.role == "user"


def test_promote_user_guard_cannot_demote_last_admin(db_session: Session) -> None:
    user = _create_user(db_session, email="admin@example.com", role="admin")

    with pytest.raises(SystemExit) as exc:
        promote_user(db_session, email="admin@example.com", role="organizer")
    assert exc.value.code == 1
    db_session.expire_all()
    reloaded = db_session.get(models.User, user.id)
    assert reloaded is not None
    assert reloaded.role == "admin"


def test_promote_user_demote_admin_allowed_with_multiple_admins(
    db_session: Session,
) -> None:
    _create_user(db_session, email="other-admin@example.com", role="admin")
    user = _create_user(db_session, email="admin@example.com", role="admin")

    promote_user(db_session, email="admin@example.com", role="organizer")

    db_session.expire_all()
    reloaded = db_session.get(models.User, user.id)
    assert reloaded is not None
    assert reloaded.role == "organizer"


def test_list_users_empty(
    capsys: pytest.CaptureFixture[str], db_session: Session
) -> None:
    list_users(db_session)
    captured = capsys.readouterr()
    assert "No users found." in captured.out


def test_list_users_populated(
    capsys: pytest.CaptureFixture[str], db_session: Session
) -> None:
    _create_user(db_session, email="first@example.com", role="admin")
    _create_user(db_session, email="second@example.com", role="user", is_active=False)

    list_users(db_session)
    captured = capsys.readouterr()
    assert "first@example.com" in captured.out
    assert "admin" in captured.out
    assert "second@example.com" in captured.out
    assert "False" in captured.out


@patch("getpass.getpass", return_value="pass123456")
def test_cli_main_create_admin(
    mock_getpass: MagicMock,
    db_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    test_args = [
        "veditor",
        "admin",
        "create-admin",
        "--email",
        "admin@example.com",
    ]
    with (
        patch("app.cli.SessionLocal", side_effect=_session_factory(db_session)),
        patch.object(sys, "argv", test_args),
        patch("sys.stdin.isatty", return_value=True),
    ):
        main()

    mock_getpass.assert_called_once()
    db_session.expire_all()
    user = db_session.scalars(select(models.User)).one()
    assert user.email == "admin@example.com"
    assert verify_password("pass123456", user.hashed_password)
    assert "Created admin user" in capsys.readouterr().out


def test_cli_main_create_admin_piped_stdin(
    db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    test_args = [
        "veditor",
        "admin",
        "create-admin",
        "--email",
        "admin@example.com",
    ]
    fake_stdin = io.StringIO("pipedpassword123\n")
    with (
        patch("app.cli.SessionLocal", side_effect=_session_factory(db_session)),
        patch.object(sys, "argv", test_args),
        patch("sys.stdin", fake_stdin),
    ):
        main()

    db_session.expire_all()
    user = db_session.scalars(select(models.User)).one()
    assert verify_password("pipedpassword123", user.hashed_password)
    assert "Created admin user" in capsys.readouterr().out


def test_cli_main_promote_user(
    db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    user = _create_user(db_session, email="user@example.com", role="user")

    test_args = [
        "veditor",
        "admin",
        "promote-user",
        "--email",
        "user@example.com",
        "--role",
        "organizer",
    ]
    with (
        patch("app.cli.SessionLocal", side_effect=_session_factory(db_session)),
        patch.object(sys, "argv", test_args),
    ):
        main()

    db_session.expire_all()
    reloaded = db_session.get(models.User, user.id)
    assert reloaded is not None
    assert reloaded.role == "organizer"
    assert "Updated user" in capsys.readouterr().out


def test_cli_main_list_users(
    db_session: Session, capsys: pytest.CaptureFixture[str]
) -> None:
    _create_user(db_session, email="listed@example.com", role="user")

    test_args = ["veditor", "admin", "list-users"]
    with (
        patch("app.cli.SessionLocal", side_effect=_session_factory(db_session)),
        patch.object(sys, "argv", test_args),
    ):
        main()

    assert "listed@example.com" in capsys.readouterr().out


@patch("app.retention.run_retention_sweep")
def test_cli_main_run_retention_sweep_sync(
    mock_sweep: MagicMock,
    db_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_sweep.return_value = [1, 2]
    test_args = ["veditor", "admin", "run-retention-sweep"]
    with (
        patch("app.cli.SessionLocal", side_effect=_session_factory(db_session)),
        patch.object(sys, "argv", test_args),
    ):
        main()

    mock_sweep.assert_called_once()
    captured = capsys.readouterr()
    assert "Cleaned 2 talks: [1, 2]" in captured.out


@patch("app.retention.enqueue_retention_sweep")
def test_cli_main_run_retention_sweep_enqueue(
    mock_enqueue: MagicMock,
    db_session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mock_job = MagicMock()
    mock_job.id = "job-sweep-123"
    mock_enqueue.return_value = mock_job

    test_args = ["veditor", "admin", "run-retention-sweep", "--enqueue"]
    with (
        patch("app.cli.SessionLocal", side_effect=_session_factory(db_session)),
        patch.object(sys, "argv", test_args),
    ):
        main()

    mock_enqueue.assert_called_once()
    captured = capsys.readouterr()
    assert "Enqueued retention sweep job with ID job-sweep-123" in captured.out
