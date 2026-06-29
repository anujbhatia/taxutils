#!/usr/bin/env python3
"""
Populate USD/INR RBI reference rates in the holdings CSV for ALL rows
(both buy/acquisition rows and sell/transfer rows).

Per Indian income tax rules the rate used is the RBI reference rate on the
last day of the month immediately preceding the month of the transaction.

  e.g. acquisition on 2014-01-24  →  rate date = 2013-12-31
       transfer on    2025-05-02  →  rate date = 2025-04-30

Fetches from https://rbi.org.in/scripts/ReferenceRateArchive.aspx.
Weekends and public holidays fall back to the nearest prior trading day
(up to 7 days).

Usage:
    python3 fetch_rbi_rates.py
    python3 fetch_rbi_rates.py --holdings /path/to/file.csv
    python3 fetch_rbi_rates.py --dry-run      # show changes without writing
    python3 fetch_rbi_rates.py --force        # overwrite already-populated rates
    python3 fetch_rbi_rates.py --no-cache     # ignore on-disk rate cache

Requires:
    python3 -m pip install requests beautifulsoup4
"""

import csv
import json
import re
import sys
import time
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependencies — run:  python3 -m pip install requests beautifulsoup4")

# ── Config ────────────────────────────────────────────────────────────────────

HOLDINGS_CSV = (
    "/Users/anbhatia/Downloads/global_investment_Holdings.xlsx - Sellable.csv"
)
RBI_URL   = "https://rbi.org.in/scripts/ReferenceRateArchive.aspx"
RATE_COL  = "USD/INR Rate"
CACHE_PATH = Path(__file__).parent / ".rbi_rate_cache.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://rbi.org.in/",
}


# ── Date parsing ──────────────────────────────────────────────────────────────

def parse_date(s: str) -> date:
    """Parse the mixed date formats used in the holdings CSV.

    Handles:
      2024-07-25, 11:31:31   (IBKR datetime with time)
      24-Mar-23              (manual entry, 2-digit year)
      24-MAR-2023            (manual entry, 4-digit year, upper-case)
      18-Mar-2025            (manual entry, 4-digit year, mixed-case)
    """
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    for fmt in ("%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: '{s}'")


# ── On-disk rate cache ────────────────────────────────────────────────────────

def load_cache() -> dict[str, float]:
    """Load cached rates. Keys are ISO date strings (YYYY-MM-DD)."""
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(cache: dict[str, float]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


# ── RBI scraping ──────────────────────────────────────────────────────────────

def _get_viewstate(session: requests.Session) -> dict[str, str]:
    """GET the RBI page and return the hidden ASP.NET form fields."""
    resp = session.get(RBI_URL, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return {
        inp["name"]: inp.get("value", "")
        for inp in soup.find_all("input", {"type": "hidden"})
        if inp.get("name")
    }


def _parse_rate_table(html: str) -> dict[date, float]:
    """
    Extract date → USD/INR pairs from the RBI response HTML.

    The result table has <td> rows (no <th>). The first row is the header
    ['Date', 'USD (INR / 1 USD)']; subsequent rows are data.
    Dates are in DD/MM/YYYY format.
    """
    soup = BeautifulSoup(html, "html.parser")
    rates: dict[date, float] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first_cells = [td.get_text(strip=True) for td in rows[0].find_all(["td", "th"])]
        if "Date" not in first_cells:
            continue

        # Locate column indices dynamically in case column order changes
        date_idx = first_cells.index("Date")
        usd_idx = next(
            (i for i, h in enumerate(first_cells) if re.search(r"USD", h, re.I)),
            None,
        )
        if usd_idx is None:
            continue

        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) <= max(date_idx, usd_idx):
                continue
            date_str = cells[date_idx]
            usd_str  = cells[usd_idx].replace(",", "")
            if not date_str or not usd_str:
                continue
            try:
                d = datetime.strptime(date_str, "%d/%m/%Y").date()
                rates[d] = float(usd_str)
            except ValueError:
                continue

        break  # stop after the first matching table

    return rates


def fetch_rates_for_range(
    session: requests.Session,
    from_date: date,
    to_date: date,
) -> dict[date, float]:
    """POST to RBI and return {date: usd_inr} for the requested range."""
    hidden = _get_viewstate(session)
    payload: dict[str, str] = {
        **hidden,
        "txtFromDate": from_date.strftime("%d/%m/%Y"),
        "txtToDate":   to_date.strftime("%d/%m/%Y"),
        "chkUSD":      "on",
        "btnSubmit":   " GO ",
    }
    resp = session.post(RBI_URL, data=payload, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    return _parse_rate_table(resp.text)


def fill_cache(
    needed_dates: list[date],
    cache: dict[str, float],
) -> dict[str, float]:
    """
    Fetch rates for any dates not already in cache, batching by calendar year
    (one HTTP request per year covers all dates in that year).
    Updates and returns cache.
    """
    uncached = sorted({d for d in needed_dates if d.isoformat() not in cache})
    if not uncached:
        return cache

    session = requests.Session()
    years = sorted({d.year for d in uncached})

    for year in years:
        year_dates = [d for d in uncached if d.year == year]
        from_date  = min(year_dates)
        to_date    = max(year_dates)

        print(
            f"  Fetching RBI rates  {from_date}  →  {to_date} ...",
            end="  ",
            flush=True,
        )
        try:
            new_rates = fetch_rates_for_range(session, from_date, to_date)
            for d, r in new_rates.items():
                cache[d.isoformat()] = r
            print(f"{len(new_rates)} rate(s).")
        except Exception as exc:
            print(f"FAILED — {exc}")

        time.sleep(1)  # avoid hammering the server

    return cache


# ── Rate-date computation ─────────────────────────────────────────────────────

def last_day_of_preceding_month(transfer_date: date) -> date:
    """Return the last calendar day of the month before transfer_date's month.

    Per Indian income tax rules, the exchange rate for computing capital gains
    on a foreign asset is the rate on the last day of the month immediately
    preceding the month in which the asset is transferred.

    e.g. transfer on 2026-02-13  →  rate date = 2026-01-31
         transfer on 2026-03-04  →  rate date = 2026-02-28
    """
    return transfer_date.replace(day=1) - timedelta(days=1)


# ── Holiday/weekend fallback ──────────────────────────────────────────────────

def resolve_rate(
    target: date,
    cache: dict[str, float],
    max_lookback: int = 7,
) -> tuple[Optional[float], Optional[date]]:
    """
    Return (rate, actual_date) for target, walking back up to max_lookback
    days for weekends and public holidays.
    """
    for offset in range(max_lookback + 1):
        d = target - timedelta(days=offset)
        if d.isoformat() in cache:
            return cache[d.isoformat()], d
    return None, None


# ── CSV helpers ───────────────────────────────────────────────────────────────

def read_csv(path: str) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError("CSV is empty")
    return rows[0], rows[1:]


def write_csv(path: str, header: list[str], data: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(data)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate USD/INR RBI reference rates in the holdings CSV."
    )
    parser.add_argument("--holdings", default=HOLDINGS_CSV, help="Path to holdings CSV")
    parser.add_argument("--dry-run",  action="store_true", help="Print changes, don't write")
    parser.add_argument("--force",    action="store_true", help="Overwrite existing rate values")
    parser.add_argument("--no-cache", action="store_true", help="Ignore on-disk rate cache")
    args = parser.parse_args()

    # ── Load CSV ──────────────────────────────────────────────────────────────
    header, data_rows = read_csv(args.holdings)

    if RATE_COL not in header:
        header = header + [RATE_COL]
    rate_idx = header.index(RATE_COL)
    date_idx = header.index("Date Acquired") if "Date Acquired" in header else 1
    qty_idx  = header.index("Sellable Qty.") if "Sellable Qty." in header else 2

    # Pad every row to the new column width
    for row in data_rows:
        while len(row) < len(header):
            row.append("")

    # ── Identify all rows and their rate-lookup dates ─────────────────────────
    # Rate date = last calendar day of the month immediately preceding the
    # transaction date (applies to both acquisitions and transfers).
    all_rows: list[tuple[int, str, date, date]] = []  # (row_idx, kind, txn_date, rate_date)

    for i, row in enumerate(data_rows):
        try:
            qty = float(row[qty_idx])
        except (ValueError, IndexError):
            continue
        try:
            txn_date = parse_date(row[date_idx])
        except ValueError:
            continue
        kind = "sell" if qty < 0 else "buy"
        rate_date = last_day_of_preceding_month(txn_date)
        all_rows.append((i, kind, txn_date, rate_date))

    # Skip rows that already have a rate (unless --force)
    needed_rate_dates = [
        rate_date
        for i, _kind, _txn, rate_date in all_rows
        if args.force or not data_rows[i][rate_idx].strip()
    ]

    buy_count  = sum(1 for _, k, _, _ in all_rows if k == "buy")
    sell_count = sum(1 for _, k, _, _ in all_rows if k == "sell")
    already_done = sum(1 for i, _, _, _ in all_rows if data_rows[i][rate_idx].strip())

    print(f"Holdings file  : {args.holdings}")
    print(f"Total rows     : {len(data_rows)}  ({buy_count} buy,  {sell_count} sell)")
    print(f"Rates needed   : {len(set(needed_rate_dates))} unique date(s) across "
          f"{len(needed_rate_dates)} row(s)  ({already_done} already set)")
    print()

    # ── Fetch missing rates ───────────────────────────────────────────────────
    cache = {} if args.no_cache else load_cache()
    cache = fill_cache(list(set(needed_rate_dates)), cache)
    print()

    if not args.dry_run:
        save_cache(cache)

    # ── Apply rates to all rows ───────────────────────────────────────────────
    updated = missing = 0

    for i, kind, txn_date, rate_date in all_rows:
        row = data_rows[i]
        if row[rate_idx].strip() and not args.force:
            continue

        rate, actual_date = resolve_rate(rate_date, cache)

        if rate is None:
            print(f"  WARNING  {row[0]:8s}  {kind} {txn_date}  "
                  f"rate-date {rate_date}  — no rate found within 7-day lookback")
            missing += 1
            continue

        note = (f"  [used {actual_date}, {(rate_date - actual_date).days}d back]"
                if actual_date != rate_date else "")
        print(f"  {row[0]:8s}  {kind:4s} {txn_date}  "
              f"rate-date {rate_date}  →  ₹{rate:.4f}{note}")
        row[rate_idx] = f"{rate:.4f}"
        updated += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    print(
        f"\n  {updated} updated"
        + (f"  |  {missing} missing" if missing else "")
    )

    if args.dry_run:
        print("\nDry-run — nothing written.")
        return

    write_csv(args.holdings, header, data_rows)
    print(f"Written: {args.holdings}")


if __name__ == "__main__":
    main()
