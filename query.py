"""Invoice query and summary helpers used by the desktop interface."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import database


@dataclass(frozen=True)
class InvoiceSummary:
    count: int
    total_amount: Decimal


def query_invoices(
    reimburser: str | None = None,
    month: str | None = None,
    category: str | None = None,
    *,
    db_path: str | None = None,
) -> list[dict]:
    return database.query_invoices(
        reimburser=reimburser or None,
        month=month or None,
        category=category or None,
        db_path=db_path,
    )


def summarize_invoices(rows: list[dict]) -> InvoiceSummary:
    return InvoiceSummary(
        count=len(rows),
        total_amount=sum(
            (Decimal(str(row["amount"])) for row in rows),
            start=Decimal("0"),
        ),
    )
