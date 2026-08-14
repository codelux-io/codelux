"""Typed errors exposed by Codelux foundation services."""


class CodeluxError(Exception):
    """Base class for expected Codelux failures."""


class ValidationError(CodeluxError):
    """Raised when persisted or user-provided data violates a contract."""


class LockUnavailableError(CodeluxError):
    """Raised when another Codelux operation holds the global lock."""


class UnsafePathError(CodeluxError):
    """Raised when a path escapes its root or traverses a symbolic link."""


class RecoveryRequiredError(CodeluxError):
    """Raised when a transaction cannot restore every modified file."""
