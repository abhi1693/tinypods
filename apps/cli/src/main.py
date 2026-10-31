"""CLI composition and source-safe process failure handling."""

import logging
from uuid import uuid4

import typer
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError
from tinypods_cli.commands import register_commands
from tinypods_core.logging import failure_fields, log_context, shutdown_logging
from tinypods_core.logging.constants import LogEvent
from tinypods_core.settings import ConfigurationError

logger = logging.getLogger("tinypods.cli")


app = typer.Typer(help="TinyPods CLI", add_completion=False, pretty_exceptions_enable=False)


register_commands(app)


def main() -> None:
    """Run a correlated command with process-safe errors and logging cleanup."""
    with log_context(command_id=str(uuid4())):
        try:
            app()
        except SystemExit as exc:
            if exc.code in (0, None):
                logger.info(LogEvent.COMMAND_COMPLETED)
            raise
        except ConfigurationError as exc:
            raise SystemExit(str(exc)) from None
        except (SQLAlchemyError, CommandError) as exc:
            logger.error(LogEvent.COMMAND_FAILED, extra=failure_fields(exc))
            raise SystemExit(
                "Database operation failed; check connectivity and migration state"
            ) from None
        except Exception as exc:
            logger.error(LogEvent.COMMAND_FAILED, extra=failure_fields(exc))
            raise SystemExit("Command failed; check application logs") from None
        finally:
            shutdown_logging()
