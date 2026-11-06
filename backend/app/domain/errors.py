"""Domain errors raised by services. They know nothing about HTTP; app/api/errors.py maps them."""


class DomainError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    """The referenced entity does not exist."""


class ConflictError(DomainError):
    """The request contradicts existing state (e.g. a run id reused for another robot)."""


class InvalidRequestError(DomainError):
    """Well-formed input that is invalid given stored state (e.g. ended_at before started_at)."""


class ForbiddenError(DomainError):
    """The caller is authenticated but may not act on this entity (e.g. another robot's run)."""
