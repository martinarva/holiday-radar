from datetime import date

import pytest

from app.holidays import Holiday


@pytest.fixture
def holiday_autumn() -> Holiday:
    """Estonian autumn break 2026 — the window every example in the tests uses."""
    return Holiday(id="autumn-2026", name="Autumn break 2026",
                   start=date(2026, 10, 26), end=date(2026, 11, 1),
                   duration_min=6, duration_max=11, active=True)
