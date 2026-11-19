import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http import HTTPMethod, HTTPStatus

import pytest
import uvicorn
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    Request,
    Route,
    expect,
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


@pytest.fixture(scope='function')
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
        'room_a1_id': room_a1.id,
        'room_a2_id': room_a2.id,
        'room_b1_id': room_b1.id,
        'room_b2_id': room_b2.id,
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
    """1. Verifies that an in-flight request aborted on modal close does not overwrite newly opened event."""
    event_a_id = int(str(test_data['event_a_id']))
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                def handle_rooms_route(route: Route, request: Request) -> None:
                    url = request.url
                    method = request.method
                    if (
                        f'/events/{event_a_id}/rooms' in url
                        and method == HTTPMethod.GET.value
                    ):
                        held_routes_a.append(route)
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

                # 3. Open Event B modal and allow its request to complete
                btn_rooms_b.click()
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')
                expect(page.locator('#rooms-list')).to_contain_text('Room B2')

                # 4. Release delayed Event A response (may be aborted by browser)
                delayed = held_routes_a.pop(0)
                try:
                    delayed.continue_()
                except PlaywrightError:
                    pass

                # 5. Verify Event B rooms are NOT overwritten by stale Event A response
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')
                expect(page.locator('#rooms-list')).to_contain_text('Room B2')
                expect(page.locator('#rooms-list')).not_to_contain_text('Room A1')
                expect(page.locator('#rooms-list')).not_to_contain_text('Room A2')
            finally:
                context.close()
        finally:
            browser.close()


def test_generation_counter_rejects_delayed_response_ignoring_abort(
    live_server: str, test_data: dict[str, object]
) -> None:
    """2. Verifies generation counter defense-in-depth: Even if a late HTTP 200 arrives,

    it is ignored because roomsRequestGeneration/roomsModalSessionId has shifted.
    """
    event_a_id = int(str(test_data['event_a_id']))
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                def handle_rooms_route(route: Route, request: Request) -> None:
                    url = request.url
                    method = request.method
                    if (
                        f'/events/{event_a_id}/rooms' in url
                        and method == HTTPMethod.GET.value
                    ):
                        held_routes_a.append(route)
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

                # Open Event B and allow it to complete
                btn_rooms_b.click()
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')

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
                    status=HTTPStatus.OK.value,
                    content_type='application/json',
                    body=stale_payload,
                )

                # Verify generation counter rejected stale payload from overwriting Event B
                expect(page.locator('#rooms-list')).not_to_contain_text(
                    'STALE_ROOM_ALPHA_1'
                )
                expect(page.locator('#rooms-list')).not_to_contain_text(
                    'STALE_ROOM_ALPHA_2'
                )
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')
            finally:
                context.close()
        finally:
            browser.close()


def test_old_delete_completion_after_switching_to_another_event(
    live_server: str, test_data: dict[str, object]
) -> None:
    """3. Verifies that an in-flight DELETE for Event A completing after switching to Event B

    does not reload Event A or disrupt Event B's active rooms list.
    """
    event_a_id = int(str(test_data['event_a_id']))
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                held_delete: list[Route] = []

                def handle_routes(route: Route, request: Request) -> None:
                    if (
                        f'/events/{event_a_id}/rooms/' in request.url
                        and request.method == HTTPMethod.DELETE.value
                    ):
                        held_delete.append(route)
                    else:
                        route.continue_()

                page.route('**/events/**/rooms/**', handle_routes)
                page.goto(f'{live_server}/studio/events')

                # Open Event A
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_a_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room A1')

                # Click Delete on Room A1
                first_row = page.locator('#rooms-list .room-list-row').first
                first_row.locator('button.btn-danger').click()

                # Confirm delete dialog
                confirm_btn = page.locator(
                    '#v-dynamic-modal button[data-modal-confirm]'
                )
                confirm_btn.wait_for()
                confirm_btn.click()

                # Wait until DELETE is intercepted and held
                for _ in range(50):
                    if len(held_delete) > 0:
                        break
                    page.wait_for_timeout(100)
                assert len(held_delete) > 0

                # While DELETE is in flight, switch to Event B
                page.locator(
                    '#modal-manage-rooms button[data-modal-close]'
                ).first.click()
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')

                # Now allow the held DELETE for Event A to complete
                del_route = held_delete.pop(0)
                try:
                    del_route.continue_()
                except PlaywrightError:
                    pass

                # Verify Event B remains cleanly loaded and was not overwritten or disturbed by Event A
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')
                expect(page.locator('#rooms-list')).not_to_contain_text('Room A')
            finally:
                context.close()
        finally:
            browser.close()


def test_old_patch_completion_after_switching_to_another_event(
    live_server: str, test_data: dict[str, object]
) -> None:
    """4. Verifies that an in-flight PATCH for Event A completing after switching to Event B

    does not reset Event B's form draft or reload Event A.
    """
    event_a_id = int(str(test_data['event_a_id']))
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                held_patch: list[Route] = []

                def handle_routes(route: Route, request: Request) -> None:
                    if (
                        f'/events/{event_a_id}/rooms/' in request.url
                        and request.method == HTTPMethod.PATCH.value
                    ):
                        held_patch.append(route)
                    else:
                        route.continue_()

                page.route('**/events/**/rooms/**', handle_routes)
                page.goto(f'{live_server}/studio/events')

                # Open Event A
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_a_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room A1')

                # Edit Room A1
                first_row = page.locator('#rooms-list .room-list-row').first
                first_row.locator("button:has-text('Edit')").click()
                page.locator('#room-name-input').fill('Room A1 Modified')

                # Submit form -> triggers PATCH
                page.locator("#create-room-form button[type='submit']").click()

                # Wait for PATCH to be intercepted
                for _ in range(50):
                    if len(held_patch) > 0:
                        break
                    page.wait_for_timeout(100)
                assert len(held_patch) > 0

                # While PATCH is in flight, switch to Event B
                page.locator(
                    '#modal-manage-rooms button[data-modal-close]'
                ).first.click()
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')

                # Type a draft in Event B form
                page.locator('#room-name-input').fill('Unsaved Draft For Event B')

                # Now fulfill the held Event A PATCH request
                patch_route = held_patch.pop(0)
                try:
                    patch_route.continue_()
                except PlaywrightError:
                    pass

                # Verify Event B's draft is NOT erased and Event B's rooms are untouched
                expect(page.locator('#room-name-input')).to_have_value(
                    'Unsaved Draft For Event B'
                )
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')
                expect(page.locator('#rooms-list')).not_to_contain_text('Room A')
            finally:
                context.close()
        finally:
            browser.close()


def test_delayed_close_event_after_reopening_modal(
    live_server: str, test_data: dict[str, object]
) -> None:
    """5. Verifies that a delayed 'close' event from an older modal session

    does not wipe or abort a newly opened session.
    """
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                # Open Event B modal
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')

                # Dispatch a delayed 'close' event simulating older session id
                page.evaluate("""() => {
                    const m = document.getElementById('modal-manage-rooms');
                    const realSession = m.dataset.sessionId;
                    m.dataset.sessionId = '999999';
                    m.dispatchEvent(new Event('close'));
                    m.dataset.sessionId = realSession;
                }""")

                # Verify Event B's room list is still intact and not emptied
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')
                expect(page.locator('#rooms-list')).to_contain_text('Room B2')
            finally:
                context.close()
        finally:
            browser.close()


def test_edit_remains_valid_after_same_session_list_refresh(
    live_server: str, test_data: dict[str, object]
) -> None:
    """6. Verifies that a same-session room list refresh does NOT invalidate an active edit form."""
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                # Open Event B modal
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room B1')
                expect(page.locator('#rooms-list')).to_contain_text('Room B2')

                # Click Edit on Room B1
                row_b1 = page.locator('#rooms-list .room-list-row', has_text='Room B1')
                row_b1.locator("button:has-text('Edit')").click()
                page.locator('#room-name-input').fill('Room B1 Refreshed Edit')

                # In the same session, delete Room B2
                row_b2 = page.locator('#rooms-list .room-list-row', has_text='Room B2')
                row_b2.locator('button.btn-danger').click()
                confirm_btn = page.locator(
                    '#v-dynamic-modal button[data-modal-confirm]'
                )
                confirm_btn.wait_for()
                confirm_btn.click()

                # Wait for Room B2 to be removed and room list refreshed
                expect(page.locator('#rooms-list')).not_to_contain_text('Room B2')

                # Submit the active edit form for Room B1 - must succeed because modal session is unchanged
                page.locator("#create-room-form button[type='submit']").click()

                # Verify Room B1 was successfully updated
                expect(page.locator('#rooms-list')).to_contain_text(
                    'Room B1 Refreshed Edit'
                )
            finally:
                context.close()
        finally:
            browser.close()


def test_edit_is_invalidated_after_closing_or_switching_sessions(
    live_server: str, test_data: dict[str, object]
) -> None:
    """7. Verifies that closing or switching modal sessions invalidates an active edit draft."""
    event_a_id = int(str(test_data['event_a_id']))
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                # Open Event A modal and start editing Room A1
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_a_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room A1')
                page.locator('#rooms-list .room-list-row').first.locator(
                    "button:has-text('Edit')"
                ).click()
                expect(
                    page.locator('#create-room-form button[type="submit"]')
                ).to_have_text('Save Room')

                # Close modal
                page.locator(
                    '#modal-manage-rooms button[data-modal-close]'
                ).first.click()

                # Open Event B modal - form must be clean 'Add Room' mode
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                ).click()
                expect(
                    page.locator('#create-room-form button[type="submit"]')
                ).to_have_text('Add Room')
                expect(page.locator('#cancel-room-edit')).to_have_attribute(
                    'hidden', ''
                )
                expect(page.locator('#room-name-input')).to_have_value('')
            finally:
                context.close()
        finally:
            browser.close()


def test_request_timeout_and_recovery(
    live_server: str, test_data: dict[str, object]
) -> None:
    """8. Verifies request timeout on stalled room list and recovery upon retry."""
    event_a_id = int(str(test_data['event_a_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                # Shorten timeout for test speed
                page.add_init_script('window.ROOMS_REQUEST_TIMEOUT_MS = 600;')

                stall_requests = True

                def handle_routes(route: Route, request: Request) -> None:
                    if (
                        stall_requests
                        and f'/events/{event_a_id}/rooms' in request.url
                        and request.method == HTTPMethod.GET.value
                    ):
                        # stall without replying
                        return
                    else:
                        route.continue_()

                page.route('**/events/**/rooms**', handle_routes)
                page.goto(f'{live_server}/studio/events')

                # Open Event A modal - request stalls
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_a_id}']"
                ).click()

                # Expect timeout error message
                expect(page.locator('#rooms-list')).to_contain_text(
                    'Request timed out. Please try again.'
                )

                # Restore normal network and retry by closing and reopening
                stall_requests = False
                page.locator(
                    '#modal-manage-rooms button[data-modal-close]'
                ).first.click()
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_a_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room A1')
            finally:
                context.close()
        finally:
            browser.close()


def test_existing_delete_and_edit_functionality(
    live_server: str, test_data: dict[str, object]
) -> None:
    """9. Verifies existing Delete and Edit operations function normally within the active session."""
    event_b_id = int(str(test_data['event_b_id']))
    token = str(test_data['token'])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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

                # Open Event B
                page.locator(
                    f"button.btn-manage-rooms[data-event-id='{event_b_id}']"
                ).click()
                expect(page.locator('#rooms-list')).to_contain_text('Room B2')

                # Edit Room B2
                row_b2 = page.locator('#rooms-list .room-list-row', has_text='Room B2')
                row_b2.locator("button:has-text('Edit')").click()
                page.locator('#room-name-input').fill('Room B2 Renamed OK')
                page.locator("#create-room-form button[type='submit']").click()
                expect(page.locator('#rooms-list')).to_contain_text(
                    'Room B2 Renamed OK'
                )

                # Delete the edited room
                row_renamed = page.locator(
                    '#rooms-list .room-list-row', has_text='Room B2 Renamed OK'
                )
                row_renamed.locator('button.btn-danger').click()
                confirm_btn = page.locator(
                    '#v-dynamic-modal button[data-modal-confirm]'
                )
                confirm_btn.wait_for()
                confirm_btn.click()

                # Verify room is gone
                expect(page.locator('#rooms-list')).not_to_contain_text(
                    'Room B2 Renamed OK'
                )
            finally:
                context.close()
        finally:
            browser.close()
