"""Shared TinyPods package and installed project version."""

from importlib.metadata import version

__version__ = version("tinypods-core")

__all__ = ["__version__"]
