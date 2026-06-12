"""OptionContractSelector for choosing eligible options contracts.

Filters options based on expiry, right, delta, moneyness, and quote quality constraints.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from src.core.enums import OptionRight
from src.core.models import OptionContract, QuoteSnapshot
from src.marketdata.data_quality import validate_quote_freshness, validate_quote_prices

logger = logging.getLogger(__name__)


class OptionContractSelector:
    """Selects eligible option contracts using user-defined criteria and quote quality filters."""

    def __init__(
        self,
        max_age_seconds: float,
        max_spread_pct: float,
        min_bid: float = 0.01,
    ) -> None:
        """Initialize OptionContractSelector.

        Args:
            max_age_seconds: Maximum age of a quote snapshot before it is stale.
            max_spread_pct: Maximum allowed spread percentage: (ask - bid) / mid * 100.
            min_bid: Minimum acceptable bid price.
        """
        self.max_age_seconds = max_age_seconds
        self.max_spread_pct = max_spread_pct
        self.min_bid = min_bid

    def select_contract(
        self,
        contracts: list[OptionContract],
        quotes: dict[str, QuoteSnapshot],
        underlying_price: float,
        right: OptionRight,
        dte_target: int,
        current_time: datetime,
        target_delta: Optional[float] = None,
        max_strike_distance_pct: Optional[float] = None,
    ) -> tuple[Optional[OptionContract], dict[str, list[str]]]:
        """Filter a list of option contracts and return the single best candidate.

        Args:
            contracts: The list of option contracts to evaluate.
            quotes: Dictionary of option contract symbol to its current QuoteSnapshot.
            underlying_price: Current price of the underlying asset.
            right: Target OptionRight (CALL or PUT).
            dte_target: Target Days-to-Expiration (0 for 0DTE, 1 for 1DTE, etc.).
            current_time: Current time in UTC.
            target_delta: Optional target delta (e.g., 0.30). Selection minimizes diff.
            max_strike_distance_pct: Optional maximum strike distance from underlying
                as a decimal percentage (e.g. 0.05 for 5%).

        Returns:
            A tuple of (selected_contract, rejection_reasons)
            where rejection_reasons is a dict of contract symbol to list of reasons.
        """
        eligible_candidates: list[tuple[OptionContract, QuoteSnapshot]] = []
        rejection_reasons: dict[str, list[str]] = {}

        for contract in contracts:
            symbol = contract.symbol
            reasons = []

            # 1. Right Check
            if contract.right != right:
                reasons.append(f"right_mismatch:contract={contract.right.value},target={right.value}")

            # 2. Expiry / DTE Check
            try:
                expiry_date = datetime.strptime(contract.expiry, "%Y%m%d").date()
                dte = (expiry_date - current_time.date()).days
                if dte != dte_target:
                    reasons.append(f"dte_mismatch:dte={dte},target={dte_target}")
            except ValueError:
                reasons.append("invalid_expiry_format")

            # 3. Strike distance / moneyness check
            if max_strike_distance_pct is not None:
                dist = abs(contract.strike - underlying_price) / underlying_price
                if dist > max_strike_distance_pct:
                    reasons.append(f"strike_out_of_bounds:dist={dist:.4f},max={max_strike_distance_pct:.4f}")

            # 4. Retrieve and Validate Quote Quality
            quote = quotes.get(symbol)
            if quote is None:
                reasons.append("quote_missing")
            else:
                # Freshness validation
                fresh, fresh_err = validate_quote_freshness(quote, current_time, self.max_age_seconds)
                if not fresh and fresh_err:
                    reasons.append(fresh_err)

                # Price and spread validation
                valid, price_err = validate_quote_prices(quote, self.max_spread_pct)
                if not valid and price_err:
                    reasons.append(price_err)

                # Min bid check
                if quote.bid is not None and quote.bid < self.min_bid:
                    reasons.append(f"bid_below_threshold:bid={quote.bid},min={self.min_bid}")

                # Target delta presence check (if delta filter is requested)
                if target_delta is not None and quote.delta is None:
                    reasons.append("delta_unavailable")

            # Store rejection reasons or register candidate
            if reasons:
                rejection_reasons[symbol] = reasons
            elif quote is not None:
                eligible_candidates.append((contract, quote))

        if not eligible_candidates:
            return None, rejection_reasons

        # 5. Selection Optimization
        # Sort eligible candidates to find the best match
        if target_delta is not None:
            # Minimize absolute distance between quote delta and target delta (using absolute values)
            def delta_sorter(item: tuple[OptionContract, QuoteSnapshot]) -> float:
                _, q = item
                # delta on quote is not None because we filtered for delta_unavailable
                return abs(abs(q.delta) - abs(target_delta))

            eligible_candidates.sort(key=delta_sorter)
        else:
            # Default: ATM (minimize strike distance to underlying price)
            def strike_sorter(item: tuple[OptionContract, QuoteSnapshot]) -> float:
                c, _ = item
                return abs(c.strike - underlying_price)

            eligible_candidates.sort(key=strike_sorter)

        selected_contract = eligible_candidates[0][0]
        return selected_contract, rejection_reasons
