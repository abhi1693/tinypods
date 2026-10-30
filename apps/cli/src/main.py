"""Local diagnostic command; application operations use future shared services."""

import argparse
import json

from tinypods_core.settings import ConfigurationError, load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="TinyPods CLI")
    parser.add_argument("command", choices=["doctor"])
    parser.parse_args()
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps({"configuration": "valid", "service": settings.service_name}))
