"""Price providers.

Stage A (screening): travelpayouts, ryanair — wide, cached/calendar-based.
Stage B (verify): google_flights — narrow, exact.
All fail soft with ProviderError; the radar keeps running on what works.
"""
from app.providers.base import Observation, ProviderError, VerifiedOffer  # noqa: F401
