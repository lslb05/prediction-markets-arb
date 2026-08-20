"""The shape every venue adapter must produce, independent of any venue."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Status = Literal["open", "closed", "inactive"]


@dataclass(frozen=True, slots=True)
class Market:
    """One tradeable binary contract, normalised across venues.

    `external_id` is the venue's own identifier — a Kalshi ticker, a
    Polymarket condition id — and is what makes ingestion idempotent
    together with the platform.

    `execution_data` carries whatever the trader needs to actually route an
    order there and nothing else: Polymarket's two CLOB token ids, Kalshi's
    ticker. It stays a free-form mapping because the two venues genuinely
    address a contract differently, and forcing a common column would mean
    inventing a field that is null half the time.
    """

    external_id: str
    title: str
    specific_outcome: str | None
    category: str | None
    start_date: datetime | None
    end_date: datetime | None
    status: Status
    execution_data: dict[str, str] = field(default_factory=dict)
