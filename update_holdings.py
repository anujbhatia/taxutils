#!/usr/bin/env python3
"""
Update holdings CSV with new trades and corporate action adjustments
from an IBKR annual activity statement, then clean up stale entries.

Holdings CSV columns:
  Symbol | Date Acquired | Sellable Qty. | Purchase Date FMV | Commission

IBKR Trades row (Trades,Data,Order,...):
  [5]  Symbol
  [6]  Date/Time
  [7]  Quantity   (positive = buy, negative = sell)
  [8]  T. Price
  [11] Comm/Fee

IBKR Corporate Actions row (Corporate Actions,Data,Stocks,...):
  [5]  Date/Time
  [6]  Description  e.g. "SYM(ISIN) Merged(Acquisition) WITH NEW_ISIN N for M (NEW_SYM, ...)"
  [7]  Quantity     (positive = new shares received, negative = old shares removed)
  [9]  Value

State file (JSON, alongside holdings): tracks applied corporate actions so
re-running the same IBKR statement doesn't double-apply them.
"""

import csv
import json
import re
import argparse
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
import sys

TICKER_MAP = {
    "BRK B": "BRK.B",
}


# ── Date helpers ──────────────────────────────────────────────────────────────

def parse_holding_date(s: str) -> date:
    """Parse the mixed date formats that appear in the holdings file.

    Handles:
      2024-07-25, 11:31:31   (IBKR datetime with time)
      16-SEP-2022            (manual entry, upper-case month)
      18-Mar-2025            (manual entry, mixed-case month)
    """
    s = s.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}', s):
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    return datetime.strptime(s, '%d-%b-%Y').date()


def default_cleanup_cutoff() -> date:
    """Return the start of the previous Indian financial year.

    Indian FY runs 1 Apr – 31 Mar.  When running in FY N–(N+1) we are
    computing tax for FY (N-1)–N, so sells from FY (N-2)–(N-1) and earlier
    are no longer needed.  Cutoff = 1 Apr of (N-1).

    Example: today = 2026-06-28  →  current FY starts 2026-04-01  (N=2026)
             cutoff = 2025-04-01
    """
    today = date.today()
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    return date(fy_start_year - 1, 4, 1)


# ── State file (corporate-action idempotency) ─────────────────────────────────

def state_path(holdings_path: str) -> Path:
    p = Path(holdings_path)
    return p.parent / (p.stem + "_state.json")


def load_state(holdings_path: str) -> dict:
    sp = state_path(holdings_path)
    if sp.exists():
        return json.loads(sp.read_text())
    return {"applied_corporate_actions": []}


def save_state(holdings_path: str, state: dict) -> None:
    state_path(holdings_path).write_text(json.dumps(state, indent=2))


# ── IBKR parsing ──────────────────────────────────────────────────────────────

def parse_ibkr_trades(path: str) -> list[dict]:
    trades = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 12:
                continue
            if row[0] == "Trades" and row[1] == "Data" and row[2] == "Order":
                symbol = TICKER_MAP.get(row[5], row[5])
                trades.append({
                    "symbol": symbol,
                    "datetime": row[6],
                    "quantity": float(row[7]),
                    "price": float(row[8]),
                    "commission": float(row[11]),
                })
    return trades


def parse_ibkr_corporate_actions(path: str) -> list[dict]:
    actions = []
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if (
                len(row) >= 10
                and row[0] == "Corporate Actions"
                and row[1] == "Data"
                and row[2] == "Stocks"
            ):
                actions.append({
                    "datetime": row[5],
                    "description": row[6],
                    "quantity": float(row[7]),
                    "value": float(row[9]),
                })
    return actions


def _symbol_from_description(description: str) -> str:
    m = re.match(r'^([^(]+)\(', description)
    raw = m.group(1).strip() if m else description.split('(')[0].strip()
    return TICKER_MAP.get(raw, raw)


def group_corporate_actions(raw_actions: list[dict]) -> list[dict]:
    """Pair positive (new shares) and negative (old shares) legs of each
    corporate action.  Returns one adjustment record per event with:
      symbol, ratio (new_qty/old_qty), datetime, description.
    """
    groups: dict = defaultdict(list)
    for action in raw_actions:
        symbol = _symbol_from_description(action["description"])
        groups[(action["datetime"], symbol)].append(action)

    adjustments = []
    for (dt, symbol), legs in groups.items():
        pos = sum(r["quantity"] for r in legs if r["quantity"] > 0)
        neg = abs(sum(r["quantity"] for r in legs if r["quantity"] < 0))
        if pos > 0 and neg > 0:
            adjustments.append({
                "symbol": symbol,
                "ratio": pos / neg,
                "datetime": dt,
                "description": legs[0]["description"],
            })
    return adjustments


# ── eTrade PDF parsing ────────────────────────────────────────────────────────

def _parse_etrade_pdf(pdf_path: Path) -> "dict | None":
    try:
        import pdfplumber
    except ImportError:
        raise SystemExit("pdfplumber is required for eTrade PDF parsing. Run: pip3 install pdfplumber")

    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[0].extract_text()

    if not text:
        return None

    # Data row: "MM/DD/YYYY  MM/DD/YYYY  QTY  PRICE  ..."
    m = re.search(
        r'(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)',
        text,
    )
    if not m:
        return None

    trade_date = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
    quantity = float(m.group(2).replace(",", ""))
    price = float(m.group(3).replace(",", ""))

    tx_m = re.search(r'Transaction Type:\s*(Sold|Bought)', text)
    if not tx_m:
        return None
    is_sell = tx_m.group(1) == "Sold"

    sym_m = re.search(r'Symbol\s*/\s*CUSIP\s*/\s*ISIN:\s*([A-Z.]+)\s*/', text)
    if not sym_m:
        return None
    symbol = TICKER_MAP.get(sym_m.group(1), sym_m.group(1))

    commission = 0.0
    comm_m = re.search(r'\bCommission\s+\$([\d,]+(?:\.\d+)?)', text)
    if comm_m:
        commission += float(comm_m.group(1).replace(",", ""))
    supp_m = re.search(r'Transaction Fee\s+\$([\d,]+(?:\.\d+)?)', text)
    if supp_m:
        commission += float(supp_m.group(1).replace(",", ""))

    return {
        "symbol": symbol,
        "datetime": trade_date,
        "quantity": -quantity if is_sell else quantity,
        "price": price,
        "commission": -commission if commission > 0 else 0.0,
        "filename": pdf_path.name,
    }


def parse_etrade_confirmations(folder: str) -> list[dict]:
    """Parse all eTrade PDF trade confirmations in a folder."""
    trades = []
    for pdf_path in sorted(Path(folder).glob("*.pdf")):
        trade = _parse_etrade_pdf(pdf_path)
        if trade:
            trades.append(trade)
        else:
            print(f"  Warning: could not parse {pdf_path.name}", file=sys.stderr)
    return trades


# ── Holdings I/O ──────────────────────────────────────────────────────────────

def parse_holdings(path: str) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError("Holdings file is empty")
    return rows[0], rows[1:]


def write_holdings(path: str, header: list[str], data_rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        writer.writerows(data_rows)


def existing_keys(data_rows: list[list[str]]) -> set[tuple[str, str]]:
    return {(r[0].strip(), r[1].strip()) for r in data_rows if len(r) >= 2}


def existing_etrade_keys(data_rows: list[list[str]]) -> set[tuple[str, str, str]]:
    """(symbol, date, price_str) keys — price disambiguates same-day same-symbol trades."""
    return {(r[0].strip(), r[1].strip(), r[3].strip()) for r in data_rows if len(r) >= 4}


# ── Formatting ────────────────────────────────────────────────────────────────

def _parse_price(s: str) -> float:
    return float(s.strip().lstrip("$").replace(",", "").strip())


def format_price(value: float) -> str:
    s = f"{value:.4f}".rstrip("0").rstrip(".")
    if "." not in s:
        s += ".00"
    elif len(s.split(".")[1]) < 2:
        s += "0"
    return s


def format_qty(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def format_commission(value: float) -> str:
    rounded = round(abs(value), 2)
    if value < 0:
        return f"-${rounded:.2f}"
    if value > 0:
        return f"${rounded:.2f}"
    return "$0.00"


# ── Corporate action adjustment ───────────────────────────────────────────────

def apply_corporate_actions(
    data_rows: list[list[str]],
    adjustments: list[dict],
    applied_keys: set[tuple[str, str]],
) -> tuple[list[dict], list[dict]]:
    """Adjust qty and per-share price for every open (positive-qty) lot of the
    affected symbol.  Dates are preserved — holding period carries over under
    Indian income tax law.  data_rows is mutated in-place.

    Returns (applied_reports, skipped_reports).
    """
    applied, skipped = [], []
    for adj in adjustments:
        key = (adj["symbol"], adj["datetime"])
        if key in applied_keys:
            skipped.append(adj)
            continue

        symbol, ratio = adj["symbol"], adj["ratio"]
        changes = []
        for i, row in enumerate(data_rows):
            if len(row) < 4 or row[0].strip() != symbol:
                continue
            try:
                old_qty = float(row[2])
            except ValueError:
                continue
            if old_qty <= 0:
                continue
            try:
                old_price = _parse_price(row[3])
            except ValueError:
                continue
            new_qty = old_qty * ratio
            new_price = old_price / ratio
            changes.append({
                "date": row[1],
                "old_qty": old_qty, "new_qty": new_qty,
                "old_price": old_price, "new_price": new_price,
            })
            data_rows[i] = (
                [row[0], row[1], format_qty(new_qty), format_price(new_price)]
                + row[4:]
            )
        applied.append({**adj, "changes": changes})
    return applied, skipped


# ── Stale-entry cleanup ───────────────────────────────────────────────────────

def _fifo_cleanup(
    indexed_rows: list[tuple[int, list[str]]], cutoff: date
) -> tuple[set[int], dict[int, float]]:
    """Compute which rows to remove / adjust for one symbol via FIFO.

    Processes ALL sells chronologically (to maintain correct FIFO state),
    but only sells before *cutoff* drive removals/adjustments of buy lots.

    Returns:
      remove_indices  – set of row indices to delete entirely
      adjust_qtys     – {row_index: new_qty} for partially consumed buy lots
    """
    buys: list[list] = []   # [global_idx, date, original_qty]
    sells: list[tuple] = [] # (global_idx, date, abs_qty, is_old)

    for gidx, row in indexed_rows:
        if len(row) < 3:
            continue
        try:
            d = parse_holding_date(row[1])
            qty = float(row[2])
        except ValueError:
            continue
        if qty > 0:
            buys.append([gidx, d, qty])
        elif qty < 0:
            sells.append((gidx, d, abs(qty), d < cutoff))

    if not any(is_old for _, _, _, is_old in sells):
        return set(), {}

    buys.sort(key=lambda x: x[1])
    sells.sort(key=lambda x: x[1])

    consumed_by_old: dict[int, float] = {b[0]: 0.0 for b in buys}
    buy_remaining = [b[2] for b in buys]

    for _sidx, _sdate, sell_qty, is_old in sells:
        left = sell_qty
        for j in range(len(buys)):
            if left <= 0:
                break
            if buy_remaining[j] <= 0:
                continue
            consume = min(buy_remaining[j], left)
            if is_old:
                consumed_by_old[buys[j][0]] += consume
            buy_remaining[j] -= consume
            left -= consume

    remove_indices: set[int] = set()
    adjust_qtys: dict[int, float] = {}

    for sidx, _, _, is_old in sells:
        if is_old:
            remove_indices.add(sidx)

    for buy in buys:
        gidx, _, original_qty = buy
        old_consumed = consumed_by_old[gidx]
        if old_consumed <= 0:
            continue
        new_qty = original_qty - old_consumed
        if new_qty <= 0:
            remove_indices.add(gidx)
        else:
            adjust_qtys[gidx] = new_qty

    return remove_indices, adjust_qtys


def compute_cleanup(
    data_rows: list[list[str]], cutoff: date
) -> tuple[set[int], dict[int, float]]:
    """Run _fifo_cleanup per symbol across the full holdings."""
    by_symbol: dict[str, list] = defaultdict(list)
    for i, row in enumerate(data_rows):
        if row and row[0].strip():
            by_symbol[row[0].strip()].append((i, row))

    all_remove: set[int] = set()
    all_adjust: dict[int, float] = {}
    for rows in by_symbol.values():
        rem, adj = _fifo_cleanup(rows, cutoff)
        all_remove |= rem
        all_adjust.update(adj)
    return all_remove, all_adjust


def apply_cleanup(
    data_rows: list[list[str]], remove: set[int], adjust: dict[int, float]
) -> list[list[str]]:
    result = []
    for i, row in enumerate(data_rows):
        if i in remove:
            continue
        if i in adjust:
            row = list(row)
            row[2] = format_qty(adjust[i])
        result.append(row)
    return result


def sort_holdings(data_rows: list[list[str]]) -> list[list[str]]:
    """Sort rows by symbol (alphabetical) then by acquisition date (oldest first).

    Rows that cannot be date-parsed are placed last within their symbol group.
    """
    _SENTINEL = date.max

    def sort_key(row: list[str]) -> tuple:
        symbol = row[0].strip() if row else ""
        try:
            d = parse_holding_date(row[1]) if len(row) > 1 else _SENTINEL
        except ValueError:
            d = _SENTINEL
        return (symbol, d)

    return sorted(data_rows, key=sort_key)


# ── Trade helpers ─────────────────────────────────────────────────────────────

def trade_to_row(trade: dict) -> list[str]:
    qty = int(trade["quantity"]) if trade["quantity"] == int(trade["quantity"]) else trade["quantity"]
    return [
        trade["symbol"],
        trade["datetime"],
        str(qty),
        format_price(trade["price"]),
        format_commission(trade["commission"]),
    ]


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update holdings CSV from an IBKR activity statement."
    )
    parser.add_argument(
        "--holdings",
        default="/Users/anbhatia/Downloads/global_investment_Holdings.xlsx - Sellable.csv",
    )
    parser.add_argument(
        "--ibkr",
        default="/Users/anbhatia/Downloads/IBKR Activity Stmt FY 25-26 U19268247_20260331_20260331.csv",
    )
    parser.add_argument(
        "--cleanup-before",
        metavar="YYYY-MM-DD",
        help=(
            "Remove sells before this date and their FIFO-matched buy lots. "
            "Default: start of the previous Indian FY (computed from today)."
        ),
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Skip the stale-entry cleanup step.",
    )
    parser.add_argument(
        "--etrade",
        metavar="FOLDER",
        help="Folder containing eTrade PDF trade confirmations to ingest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview all changes without writing to disk.",
    )
    args = parser.parse_args()

    header, data_rows = parse_holdings(args.holdings)
    state = load_state(args.holdings)
    applied_ca_keys = {
        (e["symbol"], e["datetime"])
        for e in state.get("applied_corporate_actions", [])
    }

    ibkr_trades = parse_ibkr_trades(args.ibkr)
    ca_adjustments = group_corporate_actions(parse_ibkr_corporate_actions(args.ibkr))

    etrade_trades: list[dict] = []
    if args.etrade:
        etrade_trades = parse_etrade_confirmations(args.etrade)

    # Work on a copy throughout so --dry-run never touches the file
    working = [list(r) for r in data_rows]

    # ── Step 1: corporate action adjustments ─────────────────────────────────
    ca_applied, ca_skipped = apply_corporate_actions(working, ca_adjustments, applied_ca_keys)

    # ── Step 2: new trades ────────────────────────────────────────────────────
    known = existing_keys(data_rows)
    new_trades = [t for t in ibkr_trades if (t["symbol"], t["datetime"]) not in known]
    skipped    = [t for t in ibkr_trades if (t["symbol"], t["datetime"]) in known]

    working += [trade_to_row(t) for t in new_trades]

    # eTrade: use (symbol, date, price) key to distinguish same-day trades
    known_etrade = existing_etrade_keys(data_rows)
    new_etrade = [
        t for t in etrade_trades
        if (t["symbol"], t["datetime"], format_price(t["price"])) not in known_etrade
    ]
    skipped_etrade = [
        t for t in etrade_trades
        if (t["symbol"], t["datetime"], format_price(t["price"])) in known_etrade
    ]
    working += [trade_to_row(t) for t in new_etrade]

    # ── Step 3: stale-entry cleanup ───────────────────────────────────────────
    remove_indices: set[int] = set()
    adjust_qtys:    dict[int, float] = {}
    cutoff: date | None = None
    cleanup_report: list[dict] = []

    if not args.no_cleanup:
        if args.cleanup_before:
            cutoff = datetime.strptime(args.cleanup_before, "%Y-%m-%d").date()
        else:
            cutoff = default_cleanup_cutoff()

        remove_indices, adjust_qtys = compute_cleanup(working, cutoff)

        by_symbol: dict[str, dict] = {}
        for i in remove_indices:
            row = working[i]
            sym = row[0].strip()
            e = by_symbol.setdefault(sym, {"old_sells": [], "removed_buys": [], "adjusted_buys": []})
            try:
                qty = float(row[2])
            except ValueError:
                qty = 0
            if qty < 0:
                e["old_sells"].append(row)
            else:
                e["removed_buys"].append(row)
        for i, new_qty in adjust_qtys.items():
            row = working[i]
            sym = row[0].strip()
            e = by_symbol.setdefault(sym, {"old_sells": [], "removed_buys": [], "adjusted_buys": []})
            e["adjusted_buys"].append((row, new_qty))
        cleanup_report = [{"symbol": sym, **v} for sym, v in sorted(by_symbol.items())]

    # ── Print report ──────────────────────────────────────────────────────────
    print(f"Holdings file : {args.holdings}")
    print(f"IBKR statement: {args.ibkr}")
    print(f"Existing rows : {len(data_rows)}\n")

    if ca_applied:
        print("── Corporate action adjustments ────────────────────────────────────────")
        for rep in ca_applied:
            print(f"  {rep['symbol']}  ratio={rep['ratio']:.6g}  on {rep['datetime']}")
            for c in rep["changes"]:
                frac = "  ⚠ fractional — verify manually" if c["new_qty"] != int(c["new_qty"]) else ""
                print(
                    f"    {c['date']:35s}"
                    f"  qty {format_qty(c['old_qty']):>6} → {format_qty(c['new_qty']):<6}"
                    f"  price ${c['old_price']:.4f} → ${c['new_price']:.4f}{frac}"
                )
            if not rep["changes"]:
                print(f"    (no open lots found for {rep['symbol']})")
        print()

    if ca_skipped:
        print("── Corporate actions already applied (skipped) ─────────────────────────")
        for rep in ca_skipped:
            print(f"  {rep['symbol']}  ratio={rep['ratio']:.6g}  on {rep['datetime']}")
        print()

    print(f"IBKR trades: {len(ibkr_trades)} total  |  {len(new_trades)} new  |  {len(skipped)} already present")
    if skipped:
        print("\n── Already in holdings (skipped) ──────────────────────────────────────")
        for t in skipped:
            d = "BUY" if t["quantity"] > 0 else "SELL"
            print(f"  {d:4s}  {t['symbol']:8s}  {t['datetime']}  qty={int(t['quantity']):+d}  @ ${t['price']:.4f}")
    if new_trades:
        print("\n── New trades to add ───────────────────────────────────────────────────")
        for t in new_trades:
            d = "BUY" if t["quantity"] > 0 else "SELL"
            qty = int(t["quantity"]) if t["quantity"] == int(t["quantity"]) else t["quantity"]
            print(f"  {d:4s}  {t['symbol']:8s}  {t['datetime']}  qty={qty:+}  @ ${t['price']:.4f}  comm={format_commission(t['commission'])}")
    print()

    if args.etrade:
        print(f"eTrade trades: {len(etrade_trades)} total  |  {len(new_etrade)} new  |  {len(skipped_etrade)} already present")
        if skipped_etrade:
            print("\n── eTrade: Already in holdings (skipped) ──────────────────────────────")
            for t in skipped_etrade:
                d = "BUY" if t["quantity"] > 0 else "SELL"
                print(f"  {d:4s}  {t['symbol']:8s}  {t['datetime']}  qty={int(t['quantity']):+d}  @ ${t['price']:.4f}  [{t['filename']}]")
        if new_etrade:
            print("\n── eTrade: New trades to add ───────────────────────────────────────────")
            for t in new_etrade:
                d = "BUY" if t["quantity"] > 0 else "SELL"
                qty = int(t["quantity"]) if t["quantity"] == int(t["quantity"]) else t["quantity"]
                print(f"  {d:4s}  {t['symbol']:8s}  {t['datetime']}  qty={qty:+}  @ ${t['price']:.4f}  comm={format_commission(t['commission'])}  [{t['filename']}]")
        print()

    if not args.no_cleanup:
        print(f"── Stale entry cleanup  (cutoff: {cutoff}) ─────────────────────────────")
        if cleanup_report:
            for entry in cleanup_report:
                sym = entry["symbol"]
                for row in entry["old_sells"]:
                    print(f"  {sym:8s}  SELL  {row[1]:35s}  qty={row[2]}  → removed")
                for row in entry["removed_buys"]:
                    print(f"  {sym:8s}  BUY   {row[1]:35s}  qty={row[2]}  → removed  (fully consumed by old sell)")
                for row, new_qty in entry["adjusted_buys"]:
                    try:
                        old_qty = float(row[2])
                    except ValueError:
                        old_qty = 0
                    print(
                        f"  {sym:8s}  BUY   {row[1]:35s}"
                        f"  qty {format_qty(old_qty)} → {format_qty(new_qty)}"
                        f"  (partially consumed by old sell)"
                    )
            print(f"\n  {len(remove_indices)} row(s) removed, {len(adjust_qtys)} buy lot(s) adjusted")
        else:
            print("  Nothing to clean up.")
        print()

    # ── Decide whether to write ───────────────────────────────────────────────
    final = sort_holdings(apply_cleanup(working, remove_indices, adjust_qtys))

    # Include a sort-only reorder as a reason to write
    order_changed = [r[0:2] for r in final] != [r[0:2] for r in data_rows]
    nothing_to_do = (
        not ca_applied
        and not new_trades
        and not new_etrade
        and not remove_indices
        and not adjust_qtys
        and not order_changed
    )
    if nothing_to_do:
        print("Nothing to change. Holdings file unchanged.")
        return

    if args.dry_run:
        print("Dry-run mode: no changes written.")
        return

    write_holdings(args.holdings, header, final)

    # Update state with newly applied corporate actions
    for rep in ca_applied:
        state.setdefault("applied_corporate_actions", []).append(
            {"symbol": rep["symbol"], "datetime": rep["datetime"]}
        )
    save_state(args.holdings, state)

    print(f"Written to {args.holdings}")
    if ca_applied:
        ca_lot_count = sum(len(r["changes"]) for r in ca_applied)
        print(f"  {ca_lot_count} lot(s) adjusted for corporate actions")
    if new_trades:
        print(f"  {len(new_trades)} new IBKR trade row(s) added")
    if new_etrade:
        print(f"  {len(new_etrade)} new eTrade row(s) added")
    if remove_indices or adjust_qtys:
        print(f"  {len(remove_indices)} stale row(s) removed, {len(adjust_qtys)} buy lot(s) adjusted")


if __name__ == "__main__":
    main()
