"""Worker process foundation; durable queue handlers are implemented separately."""

import argparse
import logging
import signal
import threading
from types import FrameType

from tinypods_core.logging import configure_logging, shutdown_logging
from tinypods_core.settings import ConfigurationError, load_settings

logger = logging.getLogger("tinypods.worker")


def main() -> None:
    """Run the signal-aware worker loop or validate configuration and exit."""
    parser = argparse.ArgumentParser(description="TinyPods worker process")
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    args = parser.parse_args()
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    configure_logging(settings, service="worker")
    try:
        if args.check:
            logger.info("Worker configuration valid")
            return
        stopped = threading.Event()

        def stop(signum: int, frame: FrameType | None) -> None:
            """Request worker shutdown without interrupting lifecycle cleanup.

            Args:
                signum: Operating-system signal requesting shutdown.
                frame: Interrupted interpreter frame, when supplied by the signal handler.
            """
            stopped.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        logger.info("Worker process started; queue handlers are not configured")
        while not stopped.wait(settings.worker_heartbeat_interval):
            logger.info("Worker process heartbeat")
        logger.info("Worker process stopped")
    finally:
        shutdown_logging()
