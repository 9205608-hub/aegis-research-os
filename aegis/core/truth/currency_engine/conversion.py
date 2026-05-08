"""Currency Engine — deterministic cross-currency conversion.

All cross-currency computations must go through this engine.
Never compare metrics in different currencies without explicit conversion.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FXRate:
    """A point-in-time foreign exchange rate."""

    base_currency: str  # e.g. "USD"
    quote_currency: str  # e.g. "CNY"
    rate: float  # 1 base = rate quote
    rate_date: date
    source: str


@dataclass(frozen=True)
class ConversionResult:
    """Result of a currency conversion with full audit trail."""

    original_value: float
    original_currency: str
    converted_value: float
    target_currency: str
    fx_rate_used: FXRate
    conversion_path: list[str]  # e.g. ["CNY", "USD"] or ["CNY", "USD", "EUR"]


class CurrencyConversionError(Exception):
    """Raised when currency conversion fails."""


class CurrencyEngine:
    """Deterministic currency conversion engine.

    Rules:
    - All conversions must use explicit, dated FX rates.
    - Cross-currency comparisons without conversion are blocked.
    - Conversion path must be recorded for audit.
    """

    def __init__(self) -> None:
        self._rates: dict[tuple[str, str, date], FXRate] = {}

    def load_rate(self, rate: FXRate) -> None:
        """Load an FX rate into the engine."""
        key = (rate.base_currency, rate.quote_currency, rate.rate_date)
        self._rates[key] = rate
        # Also store the inverse
        inverse_key = (rate.quote_currency, rate.base_currency, rate.rate_date)
        self._rates[inverse_key] = FXRate(
            base_currency=rate.quote_currency,
            quote_currency=rate.base_currency,
            rate=1.0 / rate.rate if rate.rate != 0 else 0,
            rate_date=rate.rate_date,
            source=rate.source,
        )

    def convert(
        self,
        value: float,
        from_currency: str,
        to_currency: str,
        rate_date: date,
    ) -> ConversionResult:
        """Convert a value from one currency to another.

        Args:
            value: The amount to convert.
            from_currency: Source currency code (e.g. "CNY").
            to_currency: Target currency code (e.g. "USD").
            rate_date: Date of the FX rate to use.

        Returns:
            ConversionResult with full audit trail.

        Raises:
            CurrencyConversionError: If no rate is available.
        """
        if from_currency == to_currency:
            return ConversionResult(
                original_value=value,
                original_currency=from_currency,
                converted_value=value,
                target_currency=to_currency,
                fx_rate_used=FXRate(
                    base_currency=from_currency,
                    quote_currency=to_currency,
                    rate=1.0,
                    rate_date=rate_date,
                    source="identity",
                ),
                conversion_path=[from_currency],
            )

        # Direct rate
        direct_key = (from_currency, to_currency, rate_date)
        if direct_key in self._rates:
            rate = self._rates[direct_key]
            return ConversionResult(
                original_value=value,
                original_currency=from_currency,
                converted_value=value * rate.rate,
                target_currency=to_currency,
                fx_rate_used=rate,
                conversion_path=[from_currency, to_currency],
            )

        # Try USD cross
        for cross in ["USD", "EUR"]:
            key1 = (from_currency, cross, rate_date)
            key2 = (cross, to_currency, rate_date)
            if key1 in self._rates and key2 in self._rates:
                rate1 = self._rates[key1]
                rate2 = self._rates[key2]
                cross_rate = rate1.rate * rate2.rate
                return ConversionResult(
                    original_value=value,
                    original_currency=from_currency,
                    converted_value=value * cross_rate,
                    target_currency=to_currency,
                    fx_rate_used=FXRate(
                        base_currency=from_currency,
                        quote_currency=to_currency,
                        rate=cross_rate,
                        rate_date=rate_date,
                        source=f"cross_via_{cross}",
                    ),
                    conversion_path=[from_currency, cross, to_currency],
                )

        raise CurrencyConversionError(
            f"No FX rate available for {from_currency}->{to_currency} "
            f"on {rate_date}. Load rates before conversion."
        )

    def get_rate(self, base: str, quote: str, rate_date: date) -> FXRate | None:
        """Look up a specific FX rate."""
        return self._rates.get((base, quote, rate_date))
