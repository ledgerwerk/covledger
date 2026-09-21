"""CovLedger public package surface."""

from __future__ import annotations

try:
    from ._version import __version__
except ModuleNotFoundError:  # source tree before setuptools-scm has generated the file
    try:
        from importlib.metadata import version

        __version__ = version("covledger")
    except Exception:  # pragma: no cover - defensive source-tree fallback
        __version__ = "0.0.0"

__all__ = ["__version__"]
