from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models
from app.auth import hash_api_key
from app.main import app

client = TestClient(app)


def _create_api_client(
    db_session: Session, event_ids: list[int], api_key: str = "valid-key"
) -> models.Client:
    client_row = models.Client(hashed_key=hash_api_key(api_key), event_ids=event_ids)
    db_session.add(client_row)
    db_session.commit()
    db_session.refresh(client_row)
    return client_row


def _create_event(
    db_session: Session, event_id: int, name: str = "Event"
) -> models.Event:
    event = models.Event(id=event_id, name=name)
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    return event


def _create_talk(
    db_session: Session, event_id: int, title: str = "Test Talk"
) -> models.Talk:
    now = datetime.now(UTC)
    talk = models.Talk(
        event_id=event_id,
        title=title,
        start=now,
        end=now,
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)
    return talk


def _create_job(
    db_session: Session, talk_id: int, kind: str = "transcode"
) -> models.Job:
    job = models.Job(talk_id=talk_id, kind=kind, status="pending", log_path=None)
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def test_ops_talks_unauthorized(db_session: Session) -> None:
    # No API key provided, the dependency raises 401.
    response = client.get("/ops/talks")
    assert response.status_code == 401


def test_ops_talks_authorized(db_session: Session) -> None:
    _create_event(db_session, 1)
    _create_api_client(db_session, event_ids=[1])
    talk = _create_talk(db_session, event_id=1)

    response = client.get("/ops/talks", headers={"X-API-Key": "valid-key"})
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == talk.id
    assert data[0]["title"] == "Test Talk"


def test_ops_get_talk_authorized(db_session: Session) -> None:
    _create_event(db_session, 1)
    _create_api_client(db_session, event_ids=[1])
    talk = _create_talk(db_session, event_id=1)
    job = _create_job(db_session, talk_id=talk.id)

    response = client.get(f"/ops/talks/{talk.id}", headers={"X-API-Key": "valid-key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == talk.id
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["id"] == job.id


def test_ops_get_talk_unauthorized_event(db_session: Session) -> None:
    _create_event(db_session, 1)
    # Client only has access to event 2, talk belongs to event 1.
    _create_api_client(db_session, event_ids=[2])
    talk = _create_talk(db_session, event_id=1)

    response = client.get(f"/ops/talks/{talk.id}", headers={"X-API-Key": "valid-key"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Client is not authorized to access this event"


def test_ops_get_job_authorized(db_session: Session) -> None:
    _create_event(db_session, 1)
    _create_api_client(db_session, event_ids=[1])
    talk = _create_talk(db_session, event_id=1)
    job = _create_job(db_session, talk_id=talk.id)

    response = client.get(f"/ops/jobs/{job.id}", headers={"X-API-Key": "valid-key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == job.id
    assert data["kind"] == "transcode"


def test_ops_get_job_unauthorized_event(db_session: Session) -> None:
    _create_event(db_session, 1)
    # Client only has access to event 2, the job's talk belongs to event 1.
    _create_api_client(db_session, event_ids=[2])
    talk = _create_talk(db_session, event_id=1)
    job = _create_job(db_session, talk_id=talk.id)

    response = client.get(f"/ops/jobs/{job.id}", headers={"X-API-Key": "valid-key"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Client is not authorized to access this event"
