"""CLI settings validation and command-scoped logging initialization."""

import logging

from tinypods_core.auth import Principal
from tinypods_core.logging import configure_logging
from tinypods_core.logging.constants import LogEvent
from tinypods_core.settings import Settings, load_settings

logger = logging.getLogger("tinypods.cli")


def command_settings() -> Settings:
    """Validate configuration and initialize logging for the current CLI command.

    Returns:
        Validated deployment settings.
    """
    settings = load_settings()
    configure_logging(settings, service="cli")
    logger.info(LogEvent.COMMAND_STARTED)
    return settings


class OperatorValidator:
    """Trust command-owned identities only inside local operator execution.

    These commands already hold operator database credentials. Never install
    this validator in HTTP authentication or accept a principal from user input.
    """

    async def validate_principal(self, principal: Principal) -> Principal:
        """Return the internally constructed operator identity without impersonating a user."""
        return principal
