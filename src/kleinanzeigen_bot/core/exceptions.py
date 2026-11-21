class BotError(Exception):
    """Operational error; messages must never contain credentials."""


class ChallengeRequired(BotError):
    pass


class IPRangeBlocked(ChallengeRequired):
    """Temporary network-range restriction; requires a durable pause, not retry."""


class AuthError(BotError):
    pass


class ProfileLocked(BotError):
    pass


class SelectorNotFound(BotError):
    def __init__(self, message="Selector not found", *, stage="unknown"):
        self.stage = stage
        self.diagnostic_path = None
        super().__init__(message)


class ListingUnavailable(BotError):
    """A public listing has expired or was removed; this is not a search failure."""


class SendUncertain(BotError):
    pass


class ListingFiltered(BotError):
    def __init__(self, outcome: str, reason: str):
        self.outcome, self.reason = outcome, reason
        super().__init__(reason)


class StateConflict(BotError):
    pass
