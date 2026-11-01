from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .consts import TalkStatus

if TYPE_CHECKING:
    from .models import Talk


class InvalidTransitionError(Exception):
    def __init__(self, current_state: str, new_state: str):
        self.current_state = current_state
        self.new_state = new_state
        super().__init__(f"Invalid transition from {current_state} to {new_state}")


TRANSITIONS: dict[TalkStatus, list[TalkStatus]] = {
    TalkStatus.WAITING_FILES: [TalkStatus.DETECTING, TalkStatus.BROKEN],
    TalkStatus.DETECTING: [TalkStatus.PENDING_APPROVAL, TalkStatus.BROKEN],
    TalkStatus.PENDING_APPROVAL: [
        TalkStatus.PENDING_INTRO_OUTRO,
        TalkStatus.PENDING_BOUNDS,
        TalkStatus.REJECTED,
        TalkStatus.BROKEN,
    ],
    TalkStatus.PENDING_INTRO_OUTRO: [
        TalkStatus.PENDING_BOUNDS,
        TalkStatus.ASSEMBLING,
        TalkStatus.BROKEN,
    ],
    TalkStatus.PENDING_BOUNDS: [TalkStatus.CUTTING, TalkStatus.BROKEN],
    TalkStatus.CUTTING: [TalkStatus.GENERATING_PREVIEWS, TalkStatus.BROKEN],
    TalkStatus.GENERATING_PREVIEWS: [TalkStatus.PREVIEW, TalkStatus.BROKEN],
    TalkStatus.PREVIEW: [
        TalkStatus.ASSEMBLING,
        TalkStatus.PENDING_INTRO_OUTRO,
        TalkStatus.NEEDS_WORK,
        TalkStatus.PENDING_BOUNDS,
        TalkStatus.REJECTED,
        TalkStatus.BROKEN,
    ],
    TalkStatus.ASSEMBLING: [TalkStatus.TRANSCODING, TalkStatus.BROKEN],
    TalkStatus.TRANSCODING: [TalkStatus.UPLOADING, TalkStatus.BROKEN],
    TalkStatus.UPLOADING: [TalkStatus.DONE, TalkStatus.BROKEN],
    TalkStatus.NEEDS_WORK: [TalkStatus.CUTTING, TalkStatus.BROKEN],
    TalkStatus.DONE: [],
    TalkStatus.REJECTED: [],
    TalkStatus.BROKEN: [],
}


def advance(talk: Talk, new_state: str) -> Talk:
    current_state = talk.status

    try:
        current = TalkStatus(current_state)
        new = TalkStatus(new_state)
    except ValueError as exc:
        raise InvalidTransitionError(current_state, new_state) from exc

    if current not in TRANSITIONS:
        raise InvalidTransitionError(current_state, new_state)

    if new not in TRANSITIONS[current]:
        raise InvalidTransitionError(current_state, new_state)

    talk.status = new_state
    talk.updated_at = datetime.now(UTC)
    return talk
