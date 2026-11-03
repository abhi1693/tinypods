"""RQ worker process with built-in scheduling and durable Job recovery."""

import argparse
import asyncio
import logging

from redis.exceptions import RedisError
from rq import Worker
from rq.serializers import JSONSerializer
from sqlalchemy.exc import SQLAlchemyError
from tinypods_core.jobs.execution import bootstrap
from tinypods_core.jobs.queue import connection, queue_for, schedule_reconciliation
from tinypods_core.logging import configure_logging, failure_fields, shutdown_logging
from tinypods_core.settings import ConfigurationError, Settings, load_settings

logger = logging.getLogger("tinypods.worker")


def main() -> None:
    """Validate configuration or run one RQ worker, releasing owned resources on exit."""
    parser = argparse.ArgumentParser(description="TinyPods RQ worker")
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--burst", action="store_true", help="process queued work and exit")
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
        run_worker(settings, burst=args.burst)
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    except (RedisError, SQLAlchemyError) as exc:
        logger.error("Worker dependency unavailable", extra=failure_fields(exc))
        raise SystemExit(
            "Worker dependency unavailable; check services and migration state"
        ) from None
    finally:
        shutdown_logging()


def run_worker(settings: Settings, *, burst: bool = False) -> None:
    """Run one RQ worker with durable recovery and its maintained scheduler.

    Startup repairs missed queue submissions and recurrence. RQ owns process
    signals, job children, retries and scheduling. The queue must be private.
    """
    asyncio.run(bootstrap(settings))
    schedule_reconciliation(settings)
    with connection(settings) as client:
        # Blocking dequeue must outlive the heartbeat wait; Redis jobs use bounded
        # clients separately. RQ sets its own connection timeout for worker waits.
        worker = Worker(
            [queue_for(settings, client)],
            connection=client,
            serializer=JSONSerializer,
            log_job_description=False,
        )
        logger.info("RQ worker started")
        worker.work(burst=burst, with_scheduler=True)
        logger.info("RQ worker stopped")
