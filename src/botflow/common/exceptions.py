"""Custom exceptions for botflow."""


class BotflowError(Exception):
    """Base exception for all botflow errors."""


class NoAvailableModelError(BotflowError):
    """Raised when no model is available in a group (all weighted 0 or all cooling)."""


class AllModelsCooldownError(BotflowError):
    """Raised when all models in a group are in cooldown."""


class ProviderError(BotflowError):
    """Raised when a provider call fails."""


class ConfigurationError(BotflowError):
    """Raised when configuration is invalid."""


class PathTraversalError(BotflowError):
    """Raised when a file path escapes the wiki sandbox."""
