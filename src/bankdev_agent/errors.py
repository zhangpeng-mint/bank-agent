class BankDevError(Exception):
    """Base exception for expected BankDev Agent failures."""


class ConfigurationError(BankDevError):
    """Raised when configuration is missing, malformed, or unsafe."""


class ScopeViolation(BankDevError):
    """Raised when a requested path escapes or violates a repository scope."""


class GitInspectionError(BankDevError):
    """Raised when a repository cannot be inspected safely with Git."""


class GoldenVerificationError(BankDevError):
    """Raised when a golden case cannot be loaded or interpreted."""

