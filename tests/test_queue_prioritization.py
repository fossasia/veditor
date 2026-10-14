from datetime import UTC, datetime

from rq import Queue

from app.queue import (
    get_queue_for_stage,
    light_queue,
)
from app.states import advance


def test_get_queue_for_stage():
    # Light stages: standard vs prioritized
    assert get_queue_for_stage("detect", is_priority=False).name == "light"
    assert get_queue_for_stage("detect", is_priority=True).name == "priority_light"
    assert get_queue_for_stage("cut", is_priority=False).name == "light"
    assert get_queue_for_stage("cut", is_priority=True).name == "priority_light"
    assert get_queue_for_stage("preview", is_priority=False).name == "light"
    assert get_queue_for_stage("preview", is_priority=True).name == "priority_light"
    assert get_queue_for_stage("publish", is_priority=False).name == "light"
    assert get_queue_for_stage("publish", is_priority=True).name == "priority_light"

    # Heavy stages: transcode
    assert get_queue_for_stage("transcode", is_priority=False).name == "heavy"
    assert get_queue_for_stage("transcode", is_priority=True).name == "priority_heavy"


def test_advance_clears_priority_rank():
    class DummyTalk:
        def __init__(self, status="cutting", priority_rank=1):
            self.status = status
            self.priority_rank = priority_rank
            self.updated_at = datetime.now(UTC)

    talk = DummyTalk("cutting", priority_rank=2)
    advance(talk, "generating_previews")
    assert talk.status == "generating_previews"
    # Still in progress -> priority preserved
    assert talk.priority_rank == 2

    # Advance to review gate -> priority automatically clears
    advance(talk, "preview")
    assert talk.status == "preview"
    assert talk.priority_rank is None

    # Advance to terminal done -> priority automatically clears
    talk.status = "uploading"
    talk.priority_rank = 3
    advance(talk, "done")
    assert talk.status == "done"
    assert talk.priority_rank is None

    # Advance to terminal failure -> priority automatically clears
    talk.status = "cutting"
    talk.priority_rank = 5
    advance(talk, "broken")
    assert talk.status == "broken"
    assert talk.priority_rank is None


def test_relocate_waiting_talk_job(monkeypatch):
    def dummy_task(talk_id, data):
        return f"{talk_id}:{data}"

    target_talk_id = 88888
    test_src = Queue("test_reloc_src", connection=light_queue.connection)
    test_dst = Queue("test_reloc_dst", connection=light_queue.connection)
    test_src.empty()
    test_dst.empty()

    import app.queue

    monkeypatch.setattr(app.queue, "light_queue", test_src)
    monkeypatch.setattr(app.queue, "priority_light_queue", test_dst)

    # 1. Enqueue to test_src
    job = test_src.enqueue(dummy_task, target_talk_id, "foo")
    assert job.id in test_src.get_job_ids()

    # 2. Relocate to priority (test_dst)
    moved = app.queue.relocate_waiting_talk_job(target_talk_id, to_priority=True)
    assert moved is True
    assert job.id not in test_src.get_job_ids()
    assert job.id in test_dst.get_job_ids()

    # 3. Relocate back from priority to standard (test_src)
    moved_back = app.queue.relocate_waiting_talk_job(target_talk_id, to_priority=False)
    assert moved_back is True
    assert job.id in test_src.get_job_ids()
    assert job.id not in test_dst.get_job_ids()

    test_src.empty()
    test_dst.empty()
