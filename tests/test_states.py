import pytest

from app.states import TRANSITIONS, InvalidTransitionError, advance


class DummyTalk:
    def __init__(self, status):
        self.status = status


def test_advance_legal_transitions():
    for current_state, next_states in TRANSITIONS.items():
        for next_state in next_states:
            talk = DummyTalk(current_state)
            advanced_talk = advance(talk, next_state)
            assert advanced_talk.status == next_state
            assert talk.status == next_state


def test_advance_illegal_transitions():
    # Test at least one illegal transition per state
    illegal_moves = {
        "waiting_for_files": "cutting",
        "detecting": "cutting",
        "pending_approval": "cutting",  # must go via pending_bounds now
        "pending_bounds": "pending_approval",
        "cutting": "needs_work",
        "generating_previews": "transcoding",
        "preview": "rejected",  # rejection at preview gate resets to pending_bounds; direct rejection is illegal
        "transcoding": "done",
        "uploading": "rejected",
        "needs_work": "preview",
        "done": "waiting_for_files",
        "rejected": "waiting_for_files",
        "broken": "waiting_for_files",
    }

    for current_state, invalid_next in illegal_moves.items():
        talk = DummyTalk(current_state)
        with pytest.raises(InvalidTransitionError) as exc_info:
            advance(talk, invalid_next)

        assert exc_info.value.current_state == current_state
        assert exc_info.value.new_state == invalid_next
        assert talk.status == current_state  # State should not mutate


def test_phase4_happy_path():
    """Full happy path: waiting_for_files → detecting → pending_approval → pending_bounds → cutting → generating_previews → preview."""
    talk = DummyTalk("waiting_for_files")
    for state in (
        "detecting",
        "pending_approval",
        "pending_bounds",
        "cutting",
        "generating_previews",
        "preview",
    ):
        advance(talk, state)
        assert talk.status == state


def test_pending_approval_to_rejected():
    talk = DummyTalk("pending_approval")
    advance(talk, "rejected")
    assert talk.status == "rejected"


def test_rejected_is_terminal():
    talk = DummyTalk("rejected")
    with pytest.raises(InvalidTransitionError):
        advance(talk, "waiting_for_files")


def test_explicit_paths_per_acceptance_criteria():
    # preview → needs_work → cutting (Phase 5+ loop — still wired, just not built yet)
    talk = DummyTalk("preview")
    advance(talk, "needs_work")
    assert talk.status == "needs_work"
    advance(talk, "cutting")
    assert talk.status == "cutting"

    # preview → pending_bounds (review rejection records a reset to pending_bounds)
    talk_reset = DummyTalk("preview")
    advance(talk_reset, "pending_bounds")
    assert talk_reset.status == "pending_bounds"
