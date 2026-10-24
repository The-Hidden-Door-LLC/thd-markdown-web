"""Shared, read-only source boundary for THD desktop tools."""

from .core import SourceError, providers, read, resolve

__all__ = ["SourceError", "providers", "read", "resolve"]
__version__ = "0.3.0"
