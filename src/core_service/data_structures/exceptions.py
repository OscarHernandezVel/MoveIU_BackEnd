"""Exceptions raised by the hand-written data structures."""

from __future__ import annotations


class EmptyStructureError(IndexError):
    """Raised when reading or removing from an empty structure."""
