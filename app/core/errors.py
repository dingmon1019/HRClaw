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


class AuthenticationError(AppError):
    """Raised when authentication fails."""


class AuthorizationError(AppError):
    """Raised when a user is authenticated but not authorized."""


class CsrfError(AppError):
    """Raised when a CSRF token is missing or invalid."""


class RateLimitError(AppError):
    """Raised when a request exceeds the allowed rate."""
