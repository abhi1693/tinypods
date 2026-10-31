"""CLI settings validation and command-scoped logging initialization."""

import logging

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
