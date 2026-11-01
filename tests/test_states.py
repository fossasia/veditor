from datetime import datetime

import pytest

from app.consts import TalkStatus
from app.states import TRANSITIONS, InvalidTransitionError, advance


class DummyTalk:
    status: TalkStatus
    updated_at: datetime

    def __init__(self, status: TalkStatus) -> None:
        self.status = status


def test_advance_legal_transitions() -> None:
    for current_state, next_states in TRANSITIONS.items():
        for next_state in next_states:
            talk = DummyTalk(current_state)
            advanced_talk = advance(talk, next_state)
            assert advanced_talk.status == next_state
            assert talk.status == next_state


def test_advance_updates_talk_updated_at() -> None:
    talk = DummyTalk(TalkStatus.WAITING_FILES)
    assert not hasattr(talk, "updated_at")
    advance(talk, TalkStatus.DETECTING)
    assert hasattr(talk, "updated_at")
    assert talk.updated_at is not None
    assert talk.updated_at.tzinfo is not None

    # Also test transition to terminal 'done' from valid predecessor 'uploading'
    talk.status = TalkStatus.UPLOADING
    prev_time = talk.updated_at
    advance(talk, TalkStatus.DONE)
    assert talk.status == TalkStatus.DONE
    assert talk.updated_at >= prev_time


def test_advance_illegal_transitions() -> None:
    # Test at least one illegal transition per state
    illegal_moves = {
        TalkStatus.WAITING_FILES: TalkStatus.CUTTING,
        TalkStatus.DETECTING: TalkStatus.CUTTING,
        TalkStatus.PENDING_APPROVAL: TalkStatus.CUTTING,  # must go via pending_bounds now
        TalkStatus.PENDING_BOUNDS: TalkStatus.PENDING_APPROVAL,
        TalkStatus.CUTTING: TalkStatus.NEEDS_WORK,
        TalkStatus.GENERATING_PREVIEWS: TalkStatus.TRANSCODING,
        TalkStatus.PREVIEW: TalkStatus.TRANSCODING,  # must go via pending_intro_outro now
        TalkStatus.PENDING_INTRO_OUTRO: TalkStatus.TRANSCODING,  # must go via assembling now
        TalkStatus.ASSEMBLING: TalkStatus.DONE,
        TalkStatus.TRANSCODING: TalkStatus.DONE,
        TalkStatus.UPLOADING: TalkStatus.REJECTED,
        TalkStatus.NEEDS_WORK: TalkStatus.PREVIEW,
        TalkStatus.DONE: TalkStatus.WAITING_FILES,
        TalkStatus.REJECTED: TalkStatus.WAITING_FILES,
        TalkStatus.BROKEN: TalkStatus.WAITING_FILES,
    }

    for current_state, invalid_next in illegal_moves.items():
        talk = DummyTalk(current_state)
        with pytest.raises(InvalidTransitionError) as exc_info:
            advance(talk, invalid_next)

        assert exc_info.value.current_state == current_state
        assert exc_info.value.new_state == invalid_next
        assert talk.status == current_state  # State should not mutate


def test_phase4_happy_path() -> None:
    """Full happy path: waiting_for_files → detecting → pending_approval → pending_bounds → cutting → generating_previews → preview."""
    talk = DummyTalk(TalkStatus.WAITING_FILES)
    for state in (
        TalkStatus.DETECTING,
        TalkStatus.PENDING_APPROVAL,
        TalkStatus.PENDING_BOUNDS,
        TalkStatus.CUTTING,
        TalkStatus.GENERATING_PREVIEWS,
        TalkStatus.PREVIEW,
    ):
        advance(talk, state)
        assert talk.status == state


def test_pending_approval_to_rejected() -> None:
    talk = DummyTalk(TalkStatus.PENDING_APPROVAL)
    advance(talk, TalkStatus.REJECTED)
    assert talk.status == TalkStatus.REJECTED


def test_rejected_is_terminal() -> None:
    talk = DummyTalk(TalkStatus.REJECTED)
    with pytest.raises(InvalidTransitionError):
        advance(talk, TalkStatus.WAITING_FILES)


def test_explicit_paths_per_acceptance_criteria() -> None:
    # preview → needs_work → cutting (Phase 5+ loop — still wired, just not built yet)
    talk = DummyTalk(TalkStatus.PREVIEW)
    advance(talk, TalkStatus.NEEDS_WORK)
    assert talk.status == TalkStatus.NEEDS_WORK
    advance(talk, TalkStatus.CUTTING)
    assert talk.status == TalkStatus.CUTTING

    # preview → pending_bounds (used by needs_work)
    talk_reset = DummyTalk(TalkStatus.PREVIEW)
    advance(talk_reset, TalkStatus.PENDING_BOUNDS)
    assert talk_reset.status == TalkStatus.PENDING_BOUNDS

    # preview → rejected (terminal review rejection)
    talk_rejected = DummyTalk(TalkStatus.PREVIEW)
    advance(talk_rejected, TalkStatus.REJECTED)
    assert talk_rejected.status == TalkStatus.REJECTED

    # preview → pending_intro_outro → assembling → transcoding (Phase 5 intro/outro selection)
    talk_intro_outro = DummyTalk(TalkStatus.PREVIEW)
    advance(talk_intro_outro, TalkStatus.PENDING_INTRO_OUTRO)
    assert talk_intro_outro.status == TalkStatus.PENDING_INTRO_OUTRO
    advance(talk_intro_outro, TalkStatus.ASSEMBLING)
    assert talk_intro_outro.status == TalkStatus.ASSEMBLING
    advance(talk_intro_outro, TalkStatus.TRANSCODING)
    assert talk_intro_outro.status == TalkStatus.TRANSCODING
