"""Shared parsing of ORESTAR's Account Summary pages.

One canonical dollar parser, because there were three copies and all three
carried the same bug: ORESTAR renders negative amounts in ACCOUNTING
PARENTHESES — ($6,441.60) — and every copy matched only a leading minus sign.
The parenthesised amount matched as a plain positive, so a committee $6,441.60
in the red was recorded as $6,441.60 in the black.

That produced a discrepancy of exactly twice the true balance (our correct
calculation minus a sign-flipped reference), which the dashboard then flagged
as a data-quality warning against a figure that was in fact right.
"""

from __future__ import annotations

import re

# label … then an amount in any of:
#     $1,234.00        →  1234.00
#     -$1,234.00       → -1234.00     (leading minus, with or without a space)
#     ($1,234.00)      → -1234.00     (accounting parentheses)
_AMOUNT = r"(\()?\s*(-\s*)?\$\s*([\d,]+\.\d{2})\s*(\))?"


def parse_dollar(html: str, label: str, default: float | None = None) -> float | None:
    """First dollar amount following `label`, signed correctly.

    `label` is treated as a literal, so callers pass "Beginning Balance
    (Previous Year)" rather than a pre-escaped pattern.
    """
    m = re.search(re.escape(label) + r".*?" + _AMOUNT, html or "", re.DOTALL)
    if not m:
        return default
    val = float(m.group(3).replace(",", ""))
    # Parenthesised only counts when BOTH parens are present — a stray "(" from
    # surrounding markup must not silently negate an otherwise positive figure.
    negative = bool(m.group(2) and m.group(2).strip() == "-") or bool(m.group(1) and m.group(4))
    return -val if negative else val

def parse_dollar_between(html: str, start_label: str, end_label: str,
                         label: str, default: float | None = None) -> float | None:
    """Dollar amount for `label`, looked up only BETWEEN two section anchors.

    ORESTAR's account summary labels both in-kind rows simply "In-Kind" — once
    under Contributions, once under Expenditures:

        Contributions
          Cash Contributions            $0.00
          Loans Received (non-exempt)   $0.00
          In-Kind                       $0.00      <- contributions
          Total Contributions           $0.00
        Expenditures
          Cash Expenditures         $1,440.59
          Loan Payments (non-exempt)    $0.00
          In-Kind                       $0.00      <- expenditures
          Total Expenditures        $1,440.59

    A plain search for "In-Kind" finds the contributions row both times, which
    is why asking for "In-Kind Contributions" and "In-Kind Expenditures" — the
    labels the page never uses — returned nothing at all: every one of 45,938
    yearly records had both fields at zero while committees plainly had in-kind.
    Scoping to the enclosing section is what tells the two rows apart.
    """
    a = html.find(start_label) if html else -1
    if a == -1:
        return default
    b = html.find(end_label, a)
    if b == -1:
        return default
    return parse_dollar(html[a:b], label, default)
