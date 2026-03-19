class AppError(Exception):
    """Base application error."""


class PolicyViolationError(AppError):
    """Raised when a proposal or execution violates policy."""


class ProviderError(AppError):
    """Raised when a model provider fails."""


class ConnectorError(AppError):
    """Raised when a connector fails."""


class NotFoundError(AppError):
    """Raised when a requested record does not exist."""


class InvalidStateError(AppError):
    """Raised when a state transition is not allowed."""

