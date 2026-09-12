import sys
from unittest.mock import MagicMock, patch

import pytest

from app import models
from app.cli import create_admin, create_client, list_users, main, promote_user


@patch("app.cli.secrets.token_urlsafe")
@patch("app.cli.hash_api_key")
def test_create_client_with_new_event(mock_hash, mock_secrets):
    mock_secrets.return_value = "raw-key"
    mock_hash.return_value = "hashed-key"

    mock_session = MagicMock()

    # We'll use a side effect to set `id` on the event/client when `refresh` is called
    def mock_refresh(obj):
        obj.id = 1

    mock_session.refresh.side_effect = mock_refresh

    create_client(mock_session, event_name="New Event", event_id=None)

    # Check that add was called twice (Event, then Client)
    assert mock_session.add.call_count == 2

    added_event = mock_session.add.call_args_list[0][0][0]
    assert isinstance(added_event, models.Event)
    assert added_event.name == "New Event"

    added_client = mock_session.add.call_args_list[1][0][0]
    assert isinstance(added_client, models.Client)
    assert added_client.hashed_key == "hashed-key"
    assert added_client.event_ids == [1]


@patch("app.cli.secrets.token_urlsafe")
@patch("app.cli.hash_api_key")
def test_create_client_with_existing_event(mock_hash, mock_secrets):
    mock_secrets.return_value = "raw-key"
    mock_hash.return_value = "hashed-key"

    mock_session = MagicMock()
    mock_event = models.Event(id=2, name="Existing")
    mock_session.query.return_value.filter.return_value.first.return_value = mock_event

    def mock_refresh(obj):
        obj.id = 2

    mock_session.refresh.side_effect = mock_refresh

    create_client(mock_session, event_name=None, event_id=2)

    # Should only add Client
    assert mock_session.add.call_count == 1
    added_client = mock_session.add.call_args_list[0][0][0]
    assert isinstance(added_client, models.Client)
    assert added_client.hashed_key == "hashed-key"
    assert added_client.event_ids == [2]


def test_create_client_missing_args():
    mock_session = MagicMock()
    with pytest.raises(SystemExit) as excinfo:
        create_client(mock_session, event_name=None, event_id=None)
    assert excinfo.value.code == 1


def test_create_client_conflicting_args():
    mock_session = MagicMock()
    with pytest.raises(SystemExit) as excinfo:
        create_client(mock_session, event_name="A", event_id=1)
    assert excinfo.value.code == 1


def test_create_client_invalid_event_id():
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(SystemExit) as excinfo:
        create_client(mock_session, event_name=None, event_id=99)
    assert excinfo.value.code == 1


@patch("app.cli.create_client")
@patch("app.cli.SessionLocal")
def test_cli_main_create_client(mock_session_local, mock_create_client):
    test_args = ["veditor", "admin", "create-client", "--event-name", "Test"]
    with patch.object(sys, "argv", test_args):
        main()

    mock_session_local.assert_called_once()
    mock_create_client.assert_called_once_with(
        mock_session_local.return_value, "Test", None
    )
    mock_session_local.return_value.close.assert_called_once()


@patch("app.cli.hash_password")
def test_create_admin_success(mock_hash_password):
    mock_hash_password.return_value = "hashed-secret"
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = None

    def mock_refresh(obj):
        obj.id = 10

    mock_session.refresh.side_effect = mock_refresh

    create_admin(mock_session, email="Admin@Example.COM", password="validpassword123")

    mock_session.add.assert_called_once()
    user = mock_session.add.call_args[0][0]
    assert isinstance(user, models.User)
    assert user.email == "admin@example.com"
    assert user.role == "admin"
    assert user.is_active is True
    assert user.hashed_password == "hashed-secret"
    mock_session.commit.assert_called_once()
    mock_session.refresh.assert_called_once_with(user)


def test_create_admin_invalid_email():
    mock_session = MagicMock()
    with pytest.raises(SystemExit) as exc:
        create_admin(mock_session, email="invalid-email", password="validpassword123")
    assert exc.value.code == 1
    mock_session.add.assert_not_called()


def test_create_admin_short_password():
    mock_session = MagicMock()
    with pytest.raises(SystemExit) as exc:
        create_admin(mock_session, email="admin@example.com", password="short")
    assert exc.value.code == 1
    mock_session.add.assert_not_called()


def test_create_admin_duplicate_email():
    mock_session = MagicMock()
    existing_user = models.User(id=1, email="admin@example.com")
    mock_session.query.return_value.filter.return_value.first.return_value = (
        existing_user
    )

    with pytest.raises(SystemExit) as exc:
        create_admin(
            mock_session, email="admin@example.com", password="validpassword123"
        )
    assert exc.value.code == 1
    mock_session.add.assert_not_called()


def test_promote_user_success():
    mock_session = MagicMock()
    target_user = models.User(
        id=2, email="user@example.com", role="user", is_active=True
    )
    mock_session.query.return_value.filter.return_value.first.return_value = target_user

    promote_user(mock_session, email="user@example.com", role="organizer")

    assert target_user.role == "organizer"
    mock_session.commit.assert_called_once()
    mock_session.refresh.assert_called_once_with(target_user)


def test_promote_user_not_found():
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(SystemExit) as exc:
        promote_user(mock_session, email="missing@example.com", role="admin")
    assert exc.value.code == 1


def test_promote_user_invalid_role():
    mock_session = MagicMock()
    target_user = models.User(
        id=3, email="user@example.com", role="user", is_active=True
    )
    mock_session.query.return_value.filter.return_value.first.return_value = target_user

    with pytest.raises(SystemExit) as exc:
        promote_user(mock_session, email="user@example.com", role="superhero")
    assert exc.value.code == 1


def test_promote_user_guard_cannot_demote_last_admin():
    mock_session = MagicMock()
    admin_user = models.User(
        id=1, email="admin@example.com", role="admin", is_active=True
    )

    filter_mock = MagicMock()
    # First call: find user by email -> admin_user
    # Second call: active admins query -> [(1,)]
    filter_mock.first.return_value = admin_user
    filter_mock.count.return_value = 1
    filter_mock.order_by.return_value.with_for_update.return_value.all.return_value = [
        (1,)
    ]
    mock_session.query.return_value.filter.return_value = filter_mock

    with pytest.raises(SystemExit) as exc:
        promote_user(mock_session, email="admin@example.com", role="organizer")
    assert exc.value.code == 1


def test_promote_user_demote_admin_allowed_with_multiple_admins():
    mock_session = MagicMock()
    admin_user = models.User(
        id=1, email="admin@example.com", role="admin", is_active=True
    )

    filter_mock = MagicMock()
    filter_mock.first.return_value = admin_user
    filter_mock.count.return_value = 2
    filter_mock.order_by.return_value.with_for_update.return_value.all.return_value = [
        (1,),
        (2,),
    ]
    mock_session.query.return_value.filter.return_value = filter_mock

    promote_user(mock_session, email="admin@example.com", role="organizer")
    assert admin_user.role == "organizer"
    mock_session.commit.assert_called_once()


def test_list_users_empty(capsys):
    mock_session = MagicMock()
    mock_session.query.return_value.order_by.return_value.all.return_value = []

    list_users(mock_session)
    captured = capsys.readouterr()
    assert "No users found." in captured.out


def test_list_users_populated(capsys):
    mock_session = MagicMock()
    user1 = models.User(id=1, email="first@example.com", role="admin", is_active=True)
    user2 = models.User(id=2, email="second@example.com", role="user", is_active=False)
    mock_session.query.return_value.order_by.return_value.all.return_value = [
        user1,
        user2,
    ]

    list_users(mock_session)
    captured = capsys.readouterr()
    assert "first@example.com" in captured.out
    assert "admin" in captured.out
    assert "second@example.com" in captured.out
    assert "False" in captured.out


@patch("getpass.getpass", return_value="pass123456")
@patch("app.cli.create_admin")
@patch("app.cli.SessionLocal")
def test_cli_main_create_admin(mock_session_local, mock_create_admin, mock_getpass):
    test_args = [
        "veditor",
        "admin",
        "create-admin",
        "--email",
        "admin@example.com",
    ]
    with (
        patch.object(sys, "argv", test_args),
        patch("sys.stdin.isatty", return_value=True),
    ):
        main()

    mock_session_local.assert_called_once()
    mock_create_admin.assert_called_once_with(
        mock_session_local.return_value, "admin@example.com", "pass123456"
    )
    mock_session_local.return_value.close.assert_called_once()


@patch("app.cli.create_admin")
@patch("app.cli.SessionLocal")
def test_cli_main_create_admin_piped_stdin(mock_session_local, mock_create_admin):
    import io

    test_args = [
        "veditor",
        "admin",
        "create-admin",
        "--email",
        "admin@example.com",
    ]
    fake_stdin = io.StringIO("pipedpassword123\n")
    with patch.object(sys, "argv", test_args), patch("sys.stdin", fake_stdin):
        main()

    mock_create_admin.assert_called_once_with(
        mock_session_local.return_value, "admin@example.com", "pipedpassword123"
    )


@patch("app.cli.promote_user")
@patch("app.cli.SessionLocal")
def test_cli_main_promote_user(mock_session_local, mock_promote_user):
    test_args = [
        "veditor",
        "admin",
        "promote-user",
        "--email",
        "user@example.com",
        "--role",
        "organizer",
    ]
    with patch.object(sys, "argv", test_args):
        main()

    mock_session_local.assert_called_once()
    mock_promote_user.assert_called_once_with(
        mock_session_local.return_value, "user@example.com", "organizer"
    )
    mock_session_local.return_value.close.assert_called_once()


@patch("app.cli.list_users")
@patch("app.cli.SessionLocal")
def test_cli_main_list_users(mock_session_local, mock_list_users):
    test_args = ["veditor", "admin", "list-users"]
    with patch.object(sys, "argv", test_args):
        main()

    mock_session_local.assert_called_once()
    mock_list_users.assert_called_once_with(mock_session_local.return_value)
    mock_session_local.return_value.close.assert_called_once()
