from enum import StrEnum


class AuthRole(StrEnum):
    """
    Canonical authenticated role taxonomy shared by every credential source.
    """

    USER = "user"
    ORGANIZER = "organizer"
    SPEAKER = "speaker"
    ADMIN = "admin"

    def rank(self) -> int:
        if self == self.ADMIN:
            return 3
        if self == self.ORGANIZER:
            return 2
        if self == self.SPEAKER:
            return 1
        return 0


# Note: May be redundant
ROLE_HIERARCHY: dict[str, int] = {
    "user": 0,
    "speaker": 1,
    "organizer": 2,
    "admin": 3,
}


class SSOScopeType(StrEnum):
    """
    Scope shapes an SSO token can be minted for.

    Centralized here so the literal pair ``"event"`` / ``"talk"`` is never
    repeated as raw strings across the codebase, and so :func:`decode_sso_token`
    can validate once and hand callers a typed member.
    """

    EVENT = "event"
    TALK = "talk"
