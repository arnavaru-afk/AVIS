"""Security master domain exceptions."""


class EntityResolutionError(Exception):
    """Base exception for security master services."""


class EntityNotFoundError(EntityResolutionError):
    """Raised when an identifier cannot be resolved to a canonical entity."""


class AliasConflictError(EntityResolutionError):
    """Raised when alias lifecycle rules would be violated."""


class UnsupportedAliasSourceError(EntityResolutionError):
    """Raised when an alias source is outside the approved namespace."""
