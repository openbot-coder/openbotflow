"""Custom exceptions for botflow."""


class BotflowError(Exception):
    """Base exception for all botflow errors."""


class NoAvailableModelError(BotflowError):
    """Raised when no model is available in a group (all weighted 0 or all cooling)."""


class AllModelsCooldownError(NoAvailableModelError):
    """Raised when all models in a group are in cooldown.

    Subclass of NoAvailableModelError for backward compatibility —
    any ``except NoAvailableModelError`` will also catch this.
    """


class ProviderError(BotflowError):
    """Raised when a provider call fails."""


class ConfigurationError(BotflowError):
    """Raised when configuration is invalid."""
