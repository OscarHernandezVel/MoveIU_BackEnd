"""End-to-end fixtures (the helpers live in ``system_harness``)."""

from __future__ import annotations

import pytest

from system_harness import System, build_system


@pytest.fixture
def system() -> System:
    return build_system()
