"""Re-usable data quality validation functions for options market data.

Used by both RiskEngine and OptionContractSelector.
"""

from datetime import datetime
from typing import Optional
from src.core.models import QuoteSnapshot


def validate_quote_freshness(
    quote: Optional[QuoteSnapshot],
    now: datetime,
    max_age_seconds: float,
) -> tuple[bool, Optional[str]]:
    """Validate quote timestamp freshness.

    Args:
        quote: The QuoteSnapshot to validate.
        now: Current time in UTC.
        max_age_seconds: Maximum allowed age of the quote in seconds.

    Returns:
        (is_valid, error_reason)
    """
    if quote is None:
        return False, "quote_missing"

    quote_ts = quote.timestamp
    # Support both tz-aware and naive datetimes safely
    if quote_ts.tzinfo is None:
        now_naive = now.replace(tzinfo=None)
        age = (now_naive - quote_ts).total_seconds()
    else:
        now_aware = now if now.tzinfo is not None else now.astimezone(quote_ts.tzinfo)
        age = (now_aware - quote_ts).total_seconds()

    if age > max_age_seconds:
        return False, f"quote_stale:age={age:.1f}s,max={max_age_seconds}s"

    return True, None


def validate_quote_prices(
    quote: Optional[QuoteSnapshot],
    max_spread_pct: float,
) -> tuple[bool, Optional[str]]:
    """Validate bid/ask spread and bid presence.

    Args:
        quote: The QuoteSnapshot to validate.
        max_spread_pct: Maximum allowed spread percentage: (ask - bid) / mid * 100.

    Returns:
        (is_valid, error_reason)
    """
    if quote is None:
        return False, "quote_missing"

    if quote.bid is None or quote.ask is None:
        return False, "quote_incomplete:missing_bid_or_ask"

    if quote.ask <= 0:
        return False, "quote_invalid:ask_is_zero_or_negative"

    mid = (quote.bid + quote.ask) / 2.0
    if mid <= 0:
        return False, "quote_invalid:mid_is_zero_or_negative"

    spread_pct = ((quote.ask - quote.bid) / mid) * 100.0
    if spread_pct > max_spread_pct:
        return False, f"spread_too_wide:spread={spread_pct:.1f}%,max={max_spread_pct}%"

    return True, None
