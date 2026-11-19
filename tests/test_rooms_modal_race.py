import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
import uvicorn
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    Request,
    Route,
    sync_playwright,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app import models
from app.consts import AuthRole
from app.db import SessionLocal
from app.main import app
from app.security import create_session_token, hash_password


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope='module')
def live_server() -> Iterator[str]:
    server_port = get_free_port()
    config = uvicorn.Config(
        app, host='127.0.0.1', port=server_port, log_level='warning'
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Poll server readiness instead of fixed sleep
    deadline = time.time() + 10.0
    server_ready = False
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f'http://127.0.0.1:{server_port}/login', timeout=0.5
            ):
                server_ready = True
                break
        except urllib.error.URLError, OSError:
            time.sleep(0.05)
    if not server_ready:
        raise RuntimeError('Live test server failed to start within timeout')

    yield f'http://127.0.0.1:{server_port}'
    server.should_exit = True
    thread.join(timeout=2)


@pytest.fixture(scope='module')
def test_data() -> Iterator[dict[str, object]]:
    db = SessionLocal()
    user = db.scalar(
        select(models.User).where(models.User.email == 'race_test_org@example.com')
    )
    if not user:
        user = models.User(
            email='race_test_org@example.com',
            hashed_password=hash_password('password123'),
            role=AuthRole.ORGANIZER,
            is_active=True,
            is_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    # Event A
    event_a = models.Event(name='Race Event A', created_by_user_id=user.id)
    db.add(event_a)
    db.flush()
    room_a1 = models.Room(
        event_id=event_a.id, name='Room A1', description='Alpha Room 1'
    )
    room_a2 = models.Room(
        event_id=event_a.id, name='Room A2', description='Alpha Room 2'
    )
    db.add_all([room_a1, room_a2])

    # Event B
    event_b = models.Event(name='Race Event B', created_by_user_id=user.id)
    db.add(event_b)
    db.flush()
    room_b1 = models.Room(
        event_id=event_b.id, name='Room B1', description='Beta Room 1'
    )
    room_b2 = models.Room(
        event_id=event_b.id, name='Room B2', description='Beta Room 2'
    )
    db.add_all([room_b1, room_b2])
    db.commit()

    token = create_session_token(user.id, AuthRole.ORGANIZER)

    data: dict[str, object] = {
        'user_id': user.id,
        'token': token,
        'event_a_id': event_a.id,
        'event_b_id': event_b.id,
        'event_a_name': event_a.name,
        'event_b_name': event_b.name,
    }
    try:
        yield data
    finally:
        # Robust teardown
        try:
            db_cleanup = SessionLocal()
            for ev_id in (event_a.id, event_b.id):
                ev = db_cleanup.get(models.Event, ev_id)
                if ev:
                    db_cleanup.delete(ev)
            db_cleanup.commit()
            db_cleanup.close()
        except SQLAlchemyError:
            db_cleanup.rollback()
            db_cleanup.close()
        db.close()


def test_stale_room_response_does_not_overwrite_active_event_rooms(
    live_server: str, test_data: dict[str, object]
) -> None:
    """Verifies that an in-flight request aborted on modal close does not overwrite newly opened event."""
    event_a_id = int(str(test_data['event_a_id']))
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            context = browser.new_context(viewport={'width': 1280, 'height': 900})
            try:
                context.add_cookies(
                    [
                        {
                            'name': 'veditor_session',
                            'value': token,
                            'domain': '127.0.0.1',
                            'path': '/',
                            'httpOnly': True,
                        }
                    ]
                )
                page = context.new_page()

                held_routes_a: list[Route] = []
                event_b_completed: list[bool] = []

                def handle_rooms_route(route: Route, request: Request) -> None:
                    url = request.url
                    method = request.method
                    if f'/events/{event_a_id}/rooms' in url and method == 'GET':
                        held_routes_a.append(route)
                        return
                    elif f'/events/{event_b_id}/rooms' in url and method == 'GET':
                        route.continue_()
                        event_b_completed.append(True)
                        return
                    else:
                        route.continue_()

                page.route('**/events/**/rooms**', handle_rooms_route)

                page.goto(f'{live_server}/studio/events')
                page.wait_for_selector('.btn-manage-rooms')

                btn_rooms_a = page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_a_id}']"
                )
                btn_rooms_b = page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                )

                # 1. Open Event A modal and hold request in-flight
                btn_rooms_a.click()
                for _ in range(50):
                    if len(held_routes_a) > 0:
                        break
                    page.wait_for_timeout(100)
                assert len(held_routes_a) > 0, (
                    'Event A rooms request was not intercepted'
                )

                # 2. Close modal
                close_btn = page.locator(
                    '#modal-manage-rooms button[data-modal-close]'
                ).first
                close_btn.click()
                page.wait_for_timeout(300)

                # 3. Open Event B modal and allow its request to complete
                btn_rooms_b.click()
                for _ in range(50):
                    if len(event_b_completed) > 0:
                        break
                    page.wait_for_timeout(100)
                page.wait_for_selector('#rooms-list .room-list-row')
                page.wait_for_timeout(300)

                rooms_text_initial = page.locator('#rooms-list').inner_text()
                assert 'Room B1' in rooms_text_initial
                assert 'Room B2' in rooms_text_initial

                # 4. Release delayed Event A response (may be aborted by browser)
                delayed = held_routes_a.pop(0)
                try:
                    delayed.continue_()
                except PlaywrightError:
                    pass
                page.wait_for_timeout(1000)

                # 5. Verify Event B rooms are NOT overwritten by stale Event A response
                rooms_text_after = page.locator('#rooms-list').inner_text()
                assert 'Room B1' in rooms_text_after
                assert 'Room B2' in rooms_text_after
                assert 'Room A1' not in rooms_text_after
                assert 'Room A2' not in rooms_text_after

                # 6. Verify Delete operation works in the correct event context
                row_b1 = page.locator('#rooms-list .room-list-row').first
                row_b1.locator('button.btn-danger').click()
                page.wait_for_timeout(400)
                confirm_btn = page.locator(
                    ".v-modal-confirm button.btn-danger, button:has-text('Delete')"
                ).last
                confirm_btn.click()
                page.wait_for_timeout(1000)

                rooms_after_del = page.locator('#rooms-list').inner_text()
                assert 'Room B1' not in rooms_after_del
                assert 'Room B2' in rooms_after_del
            finally:
                context.close()
        finally:
            browser.close()


def test_generation_counter_rejects_delayed_response_ignoring_abort(
    live_server: str, test_data: dict[str, object]
) -> None:
    """Verifies generation counter defense-in-depth: Even if a late HTTP 200 arrives,

    it is ignored because roomsLoadGeneration/roomsEventId has shifted.
    """
    event_a_id = int(str(test_data['event_a_id']))
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            context = browser.new_context(viewport={'width': 1280, 'height': 900})
            try:
                context.add_cookies(
                    [
                        {
                            'name': 'veditor_session',
                            'value': token,
                            'domain': '127.0.0.1',
                            'path': '/',
                            'httpOnly': True,
                        }
                    ]
                )
                page = context.new_page()

                # Override window.fetch to strip signal so fetch never aborts locally
                page.add_init_script("""
                    const origFetch = window.fetch;
                    window.fetch = function(url, options = {}) {
                        const copy = { ...options };
                        delete copy.signal;
                        return origFetch(url, copy);
                    };
                """)

                held_routes_a: list[Route] = []
                event_b_completed: list[bool] = []

                def handle_rooms_route(route: Route, request: Request) -> None:
                    url = request.url
                    method = request.method
                    if f'/events/{event_a_id}/rooms' in url and method == 'GET':
                        held_routes_a.append(route)
                        return
                    elif f'/events/{event_b_id}/rooms' in url and method == 'GET':
                        route.continue_()
                        event_b_completed.append(True)
                        return
                    else:
                        route.continue_()

                page.route('**/events/**/rooms**', handle_rooms_route)

                page.goto(f'{live_server}/studio/events')
                page.wait_for_selector('.btn-manage-rooms')

                btn_rooms_a = page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_a_id}']"
                )
                btn_rooms_b = page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                )

                # Open Event A and hold request
                btn_rooms_a.click()
                for _ in range(50):
                    if len(held_routes_a) > 0:
                        break
                    page.wait_for_timeout(100)
                assert len(held_routes_a) > 0

                # Close modal
                close_btn = page.locator(
                    '#modal-manage-rooms button[data-modal-close]'
                ).first
                close_btn.click()
                page.wait_for_timeout(300)

                # Open Event B and allow it to complete
                btn_rooms_b.click()
                for _ in range(50):
                    if len(event_b_completed) > 0:
                        break
                    page.wait_for_timeout(100)
                page.wait_for_selector('#rooms-list .room-list-row')
                page.wait_for_timeout(300)

                # Now fulfill Event A request with real Event A room data (HTTP 200)
                delayed = held_routes_a.pop(0)
                stale_payload = json.dumps(
                    [
                        {
                            'id': 99991,
                            'event_id': event_a_id,
                            'name': 'STALE_ROOM_ALPHA_1',
                            'talks_count': 0,
                        },
                        {
                            'id': 99992,
                            'event_id': event_a_id,
                            'name': 'STALE_ROOM_ALPHA_2',
                            'talks_count': 0,
                        },
                    ]
                )
                delayed.fulfill(
                    status=200,
                    content_type='application/json',
                    body=stale_payload,
                )
                page.wait_for_timeout(1000)

                # Verify generation counter rejected stale payload from overwriting Event B
                rooms_text = page.locator('#rooms-list').inner_text()
                assert 'STALE_ROOM_ALPHA_1' not in rooms_text
                assert 'STALE_ROOM_ALPHA_2' not in rooms_text
                assert 'Room B' in rooms_text
            finally:
                context.close()
        finally:
            browser.close()


def test_edit_room_context_safety_and_mutation(
    live_server: str, test_data: dict[str, object]
) -> None:
    """Verifies that editing a room correctly updates the room in the active event context."""
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            context = browser.new_context(viewport={'width': 1280, 'height': 900})
            try:
                context.add_cookies(
                    [
                        {
                            'name': 'veditor_session',
                            'value': token,
                            'domain': '127.0.0.1',
                            'path': '/',
                            'httpOnly': True,
                        }
                    ]
                )
                page = context.new_page()
                page.goto(f'{live_server}/studio/events')
                page.wait_for_selector('.btn-manage-rooms')

                btn_rooms_b = page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                )
                btn_rooms_b.click()
                page.wait_for_selector('#rooms-list .room-list-row')

                first_row = page.locator('#rooms-list .room-list-row').first
                first_row.locator("button:has-text('Edit')").click()
                page.wait_for_timeout(300)

                name_input = page.locator('#room-name-input')
                current_val = name_input.input_value()
                new_val = f'{current_val} Renamed'
                name_input.fill(new_val)

                page.locator("#create-room-form button[type='submit']").click()
                page.wait_for_timeout(1000)

                rooms_after = page.locator('#rooms-list').inner_text()
                assert new_val in rooms_after
            finally:
                context.close()
        finally:
            browser.close()


def test_delete_confirmation_aborted_when_session_changes(
    live_server: str, test_data: dict[str, object]
) -> None:
    """Verifies Finding 1: If Delete confirmation dialog is open and modal is closed/reopened,

    the old Delete action cannot execute.
    """
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            context = browser.new_context(viewport={'width': 1280, 'height': 900})
            try:
                context.add_cookies(
                    [
                        {
                            'name': 'veditor_session',
                            'value': token,
                            'domain': '127.0.0.1',
                            'path': '/',
                            'httpOnly': True,
                        }
                    ]
                )
                page = context.new_page()

                delete_requests: list[str] = []

                def on_request(req: Request) -> None:
                    if req.method == 'DELETE':
                        delete_requests.append(req.url)

                page.on('request', on_request)

                page.goto(f'{live_server}/studio/events')
                page.wait_for_selector('.btn-manage-rooms')

                btn_rooms_b = page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                )
                btn_rooms_b.click()
                page.wait_for_selector('#rooms-list .room-list-row')

                # Click Delete on row - opens confirmation dialog
                row = page.locator('#rooms-list .room-list-row').first
                row.locator('button.btn-danger').click()
                confirm_btn = page.locator(
                    '#v-dynamic-modal button[data-modal-confirm]'
                )
                confirm_btn.wait_for()

                # While confirmation dialog is pending, the modal session changes
                # (e.g. roomsModal receives close event or another room load occurred)
                page.evaluate(
                    "document.getElementById('modal-manage-rooms').dispatchEvent(new Event('close'))"
                )
                page.wait_for_timeout(200)

                # Now the user confirms the dialog
                confirm_btn.click()
                page.wait_for_timeout(800)

                # Verify no DELETE request was dispatched because session generation changed
                assert len(delete_requests) == 0, (
                    f'Expected 0 delete requests, got {delete_requests}'
                )
            finally:
                context.close()
        finally:
            browser.close()
