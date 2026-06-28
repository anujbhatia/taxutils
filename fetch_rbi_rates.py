#!/usr/bin/env python3
"""
Populate USD/INR RBI reference rates in the holdings CSV for sale rows only.

Only rows with a negative Sellable Qty. (i.e. sell transactions) get a rate.
Per Indian income tax rules the rate used is the RBI reference rate on the
last day of the month immediately preceding the month of transfer.

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
      24-MAR-2023            (manual entry, upper-case month abbrev)
      18-Mar-2025            (manual entry, mixed-case month abbrev)
    """
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    return datetime.strptime(s, "%d-%b-%Y").date()


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

    # ── Identify sell rows and their rate-lookup dates ────────────────────────
    # Per Indian income tax rules: rate = last day of the month preceding transfer.
    sell_rows: list[tuple[int, date, date]] = []  # (row_idx, sale_date, rate_date)

    for i, row in enumerate(data_rows):
        try:
            qty = float(row[qty_idx])
        except (ValueError, IndexError):
            continue
        if qty >= 0:
            continue  # buy row — clear any stale rate and skip
        try:
            sale_date = parse_date(row[date_idx])
        except ValueError:
            continue
        rate_date = last_day_of_preceding_month(sale_date)
        sell_rows.append((i, sale_date, rate_date))

    sell_indices = {i for i, _, _ in sell_rows}

    # Clear any stale rate values on buy rows
    for i, row in enumerate(data_rows):
        if i not in sell_indices and row[rate_idx].strip():
            row[rate_idx] = ""

    # Skip rows that already have a rate (unless --force)
    needed_rate_dates = [
        rate_date
        for i, _sale, rate_date in sell_rows
        if args.force or not data_rows[i][rate_idx].strip()
    ]

    sell_count   = len(sell_rows)
    already_done = sum(
        1 for i, _, _ in sell_rows if data_rows[i][rate_idx].strip()
    )

    print(f"Holdings file  : {args.holdings}")
    print(f"Total rows     : {len(data_rows)}  ({sell_count} sell row(s))")
    print(f"Rates needed   : {len(set(needed_rate_dates))} unique date(s) across "
          f"{len(needed_rate_dates)} row(s)  ({already_done} already set)")
    print()

    # ── Fetch missing rates ───────────────────────────────────────────────────
    cache = {} if args.no_cache else load_cache()
    cache = fill_cache(list(set(needed_rate_dates)), cache)
    print()

    if not args.dry_run:
        save_cache(cache)

    # ── Apply rates to sell rows ──────────────────────────────────────────────
    updated = missing = 0

    for i, sale_date, rate_date in sell_rows:
        row = data_rows[i]
        already_set = row[rate_idx].strip()
        if already_set and not args.force:
            continue

        rate, actual_date = resolve_rate(rate_date, cache)

        if rate is None:
            print(f"  WARNING  {row[0]:8s}  sale {sale_date}  "
                  f"rate-date {rate_date}  — no rate found within 7-day lookback")
            missing += 1
            continue

        note = (f"  [used {actual_date}, {(rate_date - actual_date).days}d back]"
                if actual_date != rate_date else "")
        print(f"  {row[0]:8s}  sold {sale_date}  "
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
