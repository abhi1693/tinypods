"""Public API application factory and installed process dispatch."""

from tinypods_api.main import create_app
from tinypods_api.main import main as run_api

__all__ = ["create_app", "run_api"]
