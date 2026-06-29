#!/usr/bin/env python3
"""
Capital Gains Calculator for Indian Income Tax
Computes STCG/LTCG on foreign equity sell transactions using FIFO method.

Indian tax rules applied:
  - Foreign securities: LTCG threshold = 24 months
  - Sales on/after 23-Jul-2024 (Budget 2024): LTCG @ 12.5%, no indexation
  - Sales before 23-Jul-2024:                 LTCG @ 20%, with indexation
  - STCG (< 24 months): taxed at applicable slab rate
"""

import subprocess
import sys
from pathlib import Path

import pandas as pd
import numpy as np
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from collections import deque
import warnings
warnings.filterwarnings("ignore")

CSV_PATH = '/Users/anbhatia/Downloads/global_investment_Holdings.xlsx - Sellable.csv'
OUTPUT_PATH = str(Path(CSV_PATH).parent / 'capital_gains_india_output.csv')

BUDGET_2024_DATE = date(2024, 7, 23)
LTCG_THRESHOLD_MONTHS = 24  # Foreign unlisted/listed-abroad securities


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_price(val):
    """Parse strings like '$1,101.00', '-$5.05', '382.35', '1'."""
    if pd.isna(val) or str(val).strip() == '':
        return 0.0
    s = str(val).strip().replace(',', '').replace('$', '').strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_date(val):
    """Parse multiple date formats present in the CSV."""
    if pd.isna(val):
        return None
    s = str(val).strip()
    for fmt in (
        '%Y-%m-%d, %H:%M:%S',
        '%Y-%m-%d',
        '%d-%b-%y',
        '%d-%b-%Y',
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    print(f"  Warning: Could not parse date '{s}'")
    return None


# ---------------------------------------------------------------------------
# Tax classification
# ---------------------------------------------------------------------------

def months_between(d1, d2):
    r = relativedelta(d2, d1)
    return r.years * 12 + r.months


def classify_gain(holding_months, sale_date):
    """Return (gain_type, tax_rate_pct_or_None, indexation_bool)."""
    if holding_months >= LTCG_THRESHOLD_MONTHS:
        if sale_date >= BUDGET_2024_DATE:
            return 'LTCG', 12.5, False   # Budget 2024: 12.5%, no indexation
        else:
            return 'LTCG', 20.0, True    # Pre-Budget 2024: 20%, with indexation
    else:
        return 'STCG', None, False       # Slab rate applies


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FETCH_SCRIPT = Path(__file__).parent / 'fetch_rbi_rates.py'


def _load_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['parsed_date'] = df['Date Acquired'].apply(parse_date)
    df['qty'] = pd.to_numeric(df['Sellable Qty.'], errors='coerce')
    df['fmv_usd'] = df['Purchase Date FMV'].apply(parse_price)
    df['commission_usd'] = df['Commission'].apply(parse_price)
    df['forex_rate'] = pd.to_numeric(df['USD/INR Rate'], errors='coerce')
    return df


def _ensure_forex_rates(csv_path: str) -> pd.DataFrame:
    """
    Load the CSV. If any rows with a parseable date are missing a forex rate,
    auto-invoke fetch_rbi_rates.py to populate them, then reload.
    """
    df = _load_csv(csv_path)
    has_date = df['parsed_date'].notna()
    missing = df[has_date & df['forex_rate'].isna()]

    if missing.empty:
        return df

    print(f"{len(missing)} row(s) missing USD/INR rate — running fetch_rbi_rates.py ...")
    result = subprocess.run(
        [sys.executable, str(FETCH_SCRIPT), '--holdings', csv_path],
        capture_output=False,
    )
    if result.returncode != 0:
        print("  Warning: fetch_rbi_rates.py exited with an error; some rates may still be missing.")

    # Reload after the fetch
    df = _load_csv(csv_path)
    still_missing = df[has_date & df['forex_rate'].isna()]
    if not still_missing.empty:
        syms = still_missing['Symbol'].unique().tolist()
        print(f"  Warning: {len(still_missing)} row(s) still have no forex rate after fetch: {syms}")
    return df


def main():
    df = _ensure_forex_rates(CSV_PATH)

    buys_df = df[df['qty'] > 0].copy()
    sells_df = df[df['qty'] < 0].copy()
    sells_df['qty'] = sells_df['qty'].abs()

    # Build per-symbol FIFO queues (sorted oldest → newest)
    buy_queues: dict[str, deque] = {}
    for symbol, grp in buys_df.groupby('Symbol'):
        buy_queues[symbol] = deque()
        for _, row in grp.sort_values('parsed_date').iterrows():
            qty = float(row['qty'])
            buy_queues[symbol].append({
                'date': row['parsed_date'],
                'qty': qty,
                'original_qty': qty,          # for proportional commission
                'fmv_usd': float(row['fmv_usd']),
                'commission_usd': abs(float(row['commission_usd'])),
                'forex_rate': row['forex_rate'],  # populated by fetch_rbi_rates.py
            })

    # Process each sell transaction with FIFO matching
    results = []
    for _, sell in sells_df.sort_values(['Symbol', 'parsed_date']).iterrows():
        symbol = str(sell['Symbol'])
        sell_qty_rem = float(sell['qty'])
        sell_date = sell['parsed_date']
        sell_fmv_usd = float(sell['fmv_usd'])
        sell_comm_usd = abs(float(sell['commission_usd']))
        sell_forex = sell['forex_rate']
        original_sell_qty = sell_qty_rem  # for proportional sell-commission

        if pd.isna(sell_forex):
            print(f"  Warning: No forex rate for {symbol} sell on {sell_date} — skipping")
            continue

        if symbol not in buy_queues or not buy_queues[symbol]:
            print(f"  Warning: No buy lots found for {symbol} (sell on {sell_date})")
            continue

        while sell_qty_rem > 1e-6:
            if not buy_queues[symbol]:
                print(f"  Warning: Ran out of buy lots for {symbol} (sell on {sell_date})")
                break

            lot = buy_queues[symbol][0]
            buy_date = lot['date']

            buy_forex = lot['forex_rate']

            matched_qty = min(sell_qty_rem, lot['qty'])

            # --- INR calculations ---
            if not pd.isna(buy_forex):
                buy_price_per_share_inr = lot['fmv_usd'] * buy_forex
                # Apportion commission over original lot size
                buy_comm_inr = (lot['commission_usd'] / lot['original_qty']) * buy_forex * matched_qty
                purchase_cost_inr = buy_price_per_share_inr * matched_qty + buy_comm_inr
            else:
                buy_price_per_share_inr = np.nan
                buy_comm_inr = np.nan
                purchase_cost_inr = np.nan

            gross_sale_inr = sell_fmv_usd * sell_forex * matched_qty
            sell_comm_inr = (sell_comm_usd / original_sell_qty) * sell_forex * matched_qty
            net_sale_inr = gross_sale_inr - sell_comm_inr

            capital_gain_inr = (
                net_sale_inr - purchase_cost_inr
                if not pd.isna(purchase_cost_inr)
                else np.nan
            )

            holding_months = months_between(buy_date, sell_date) if (buy_date and sell_date) else None
            gain_type, tax_rate, indexation = (
                classify_gain(holding_months, sell_date)
                if holding_months is not None
                else ('Unknown', None, False)
            )

            results.append({
                'Symbol': symbol,
                'Purchase Date': buy_date,
                'Sale Date': sell_date,
                'Qty': round(matched_qty, 4),
                'Purchase Price (USD/share)': lot['fmv_usd'],
                'Sale Price (USD/share)': sell_fmv_usd,
                'Purchase USD/INR Rate': round(buy_forex, 4) if not pd.isna(buy_forex) else 'N/A',
                'Sale USD/INR Rate': round(sell_forex, 4),
                'Purchase Cost (INR)': round(purchase_cost_inr, 2) if not pd.isna(purchase_cost_inr) else 'N/A',
                'Sell Commission (INR)': round(sell_comm_inr, 2),
                'Sale Proceeds Gross (INR)': round(gross_sale_inr, 2),
                'Sale Proceeds Net (INR)': round(net_sale_inr, 2),
                'Capital Gain (INR)': round(capital_gain_inr, 2) if not pd.isna(capital_gain_inr) else 'N/A',
                'Holding Period (Months)': holding_months,
                'Gain Type': gain_type,
                'Tax Rate': f'{tax_rate}%' if tax_rate is not None else 'Slab Rate',
                'Indexation Available': 'Yes' if indexation else 'No',
            })

            lot['qty'] -= matched_qty
            sell_qty_rem -= matched_qty
            if lot['qty'] < 1e-6:
                buy_queues[symbol].popleft()

    # Output
    out = pd.DataFrame(results)

    if out.empty:
        out.to_csv(OUTPUT_PATH, index=False)
        print(f"\nNo sell transactions found. Results saved → {OUTPUT_PATH}")
        return

    numeric_gain    = pd.to_numeric(out['Capital Gain (INR)'],         errors='coerce')
    purchase_cost   = pd.to_numeric(out['Purchase Cost (INR)'],        errors='coerce')
    sell_comm       = pd.to_numeric(out['Sell Commission (INR)'],      errors='coerce')
    gross_sale      = pd.to_numeric(out['Sale Proceeds Gross (INR)'],  errors='coerce')
    stcg_mask = out['Gain Type'] == 'STCG'
    ltcg_mask = out['Gain Type'] == 'LTCG'

    # --- ITR field totals ---
    itr_ia   = gross_sale.sum()       # Full value of consideration (gross, unquoted shares)
    itr_ib   = itr_ia                 # FMV = consideration for market transactions
    itr_ii   = 0.0                    # No assets other than unquoted shares
    itr_bi   = purchase_cost.sum()    # Cost of acquisition (without indexation)
    itr_bii  = 0.0                    # Cost of improvement
    itr_biii = sell_comm.sum()        # Expenditure on transfer (sell commissions)
    itr_cg   = itr_ia - itr_bi - itr_bii - itr_biii

    # Build ITR summary rows to append at the bottom of the CSV
    blank = {col: '' for col in out.columns}
    itr_rows = pd.DataFrame([
        {**blank, 'Symbol': '---', 'Purchase Date': 'ITR FIELD', 'Sale Date': 'DESCRIPTION',                                              'Purchase Cost (INR)': 'VALUE (INR)'},
        {**blank, 'Symbol': 'i.a', 'Purchase Date': 'Full value of consideration — unquoted shares (gross)',                               'Purchase Cost (INR)': round(itr_ia,   2)},
        {**blank, 'Symbol': 'i.b', 'Purchase Date': 'Fair market value of unquoted shares (prescribed manner)',                            'Purchase Cost (INR)': round(itr_ib,   2)},
        {**blank, 'Symbol': 'ii',  'Purchase Date': 'Full value of consideration — assets other than unquoted shares',                     'Purchase Cost (INR)': round(itr_ii,   2)},
        {**blank, 'Symbol': 'bi',  'Purchase Date': 'Cost of acquisition without indexation',                                              'Purchase Cost (INR)': round(itr_bi,   2)},
        {**blank, 'Symbol': 'bii', 'Purchase Date': 'Cost of improvement without indexation',                                              'Purchase Cost (INR)': round(itr_bii,  2)},
        {**blank, 'Symbol': 'biii','Purchase Date': 'Expenditure wholly and exclusively in connection with transfer (sell commissions)',    'Purchase Cost (INR)': round(itr_biii, 2)},
        {**blank, 'Symbol': '=',   'Purchase Date': 'Net Capital Gain  (i.a − bi − bii − biii)',                                          'Purchase Cost (INR)': round(itr_cg,   2)},
    ])

    combined = pd.concat([out, itr_rows], ignore_index=True)
    combined.to_csv(OUTPUT_PATH, index=False)

    print(f"\nResults saved → {OUTPUT_PATH}")
    print(f"Total matched lots: {len(out)}\n")

    stcg_total = numeric_gain[stcg_mask].sum()
    ltcg_total = numeric_gain[ltcg_mask].sum()

    print("=" * 55)
    print(f"  STCG ({stcg_mask.sum()} lots):   ₹{stcg_total:>14,.2f}  (slab rate)")
    print(f"  LTCG ({ltcg_mask.sum()} lots):   ₹{ltcg_total:>14,.2f}")
    print(f"  Total Capital Gain:  ₹{numeric_gain.sum():>14,.2f}")
    print("=" * 55)

    print("\nBy symbol:")
    sym_summary = (
        out.groupby(['Symbol', 'Gain Type'])
        .agg(
            Lots=('Qty', 'count'),
            Total_Gain=('Capital Gain (INR)', lambda x: pd.to_numeric(x, errors='coerce').sum()),
        )
        .reset_index()
    )
    for _, r in sym_summary.iterrows():
        print(f"  {r['Symbol']:8s} {r['Gain Type']}  ₹{r['Total_Gain']:>14,.2f}  ({r['Lots']} lots)")

    print("\nITR Schedule CG — values to enter:")
    print(f"  i.a  Full value of consideration (unquoted shares)     ₹{itr_ia:>14,.2f}")
    print(f"  i.b  Fair market value (prescribed manner)             ₹{itr_ib:>14,.2f}")
    print(f"  ii   Consideration — other assets                      ₹{itr_ii:>14,.2f}")
    print(f"  bi   Cost of acquisition (without indexation)          ₹{itr_bi:>14,.2f}")
    print(f"  bii  Cost of improvement (without indexation)          ₹{itr_bii:>14,.2f}")
    print(f"  biii Transfer expenditure (sell commissions)           ₹{itr_biii:>14,.2f}")
    print(f"  ──────────────────────────────────────────────────────────────────")
    print(f"  Net Capital Gain  (i.a − bi − bii − biii)             ₹{itr_cg:>14,.2f}")


if __name__ == '__main__':
    main()
