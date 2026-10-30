"""Diagnostics and explicit database administration commands."""

import argparse
import asyncio
import json
from pathlib import Path

from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError
from tinypods_core.db import Database
from tinypods_core.migrations import MigrationManager
from tinypods_core.settings import ConfigurationError, Settings, load_settings


async def check_database(settings: Settings) -> str:
    database = Database(settings)
    try:
        return (await database.installation()).model_dump_json()
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="TinyPods CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="validate configuration")
    doctor.add_argument("--database", action="store_true", help="check the migrated database")
    migrations = commands.add_parser("db", help="manage core migrations explicitly")
    migrations.add_argument("action", choices=["upgrade", "check", "current"])
    migrations.add_argument("--config", type=Path, default=Path("alembic.ini"))
    args = parser.parse_args()
    try:
        settings = load_settings()
        if args.command == "db":
            manager = MigrationManager(settings, args.config)
            if args.action == "upgrade":
                manager.upgrade()
                print(json.dumps({"migration": "upgraded"}))
            elif args.action == "check":
                manager.check()
            else:
                print(json.dumps({"revisions": manager.current()}))
        elif args.database:
            print(asyncio.run(check_database(settings)))
        else:
            print(json.dumps({"configuration": "valid", "service": settings.service_name}))
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    except (SQLAlchemyError, CommandError):
        raise SystemExit(
            "Database operation failed; check connectivity and migration state"
        ) from None
