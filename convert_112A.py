#!/usr/bin/env python3
"""
Convert tradewise long-term equity trades to Schedule 112A CSV format
for ITD e-filing portal upload.

Source: Combined-taxpnl-Indian-Equity-2025_2026-Q1-Q4.xlsx - Long Term and Buyback Tradewise.csv
Target: _template.csv format as per 112A_115AD_CSV_Instructions.pdf
"""

import csv
import re
import sys
from datetime import datetime

CUTOFF_DATE = datetime(2018, 1, 31)  # Grandfathering cutoff

INPUT_FILE = '/Users/anbhatia/Downloads/Combined-taxpnl-Indian-Equity-2025_2026-Q1-Q4.xlsx - Long Term and Buyback Tradewise.csv'
TEMPLATE_FILE = '/Users/anbhatia/Downloads/_template.csv'
OUTPUT_FILE = '/Users/anbhatia/Downloads/112A_LongTerm_output.csv'


def parse_date(s):
    s = s.strip()
    for fmt in ('%Y-%m-%d', '%d-%b-%Y'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def safe_float(val, default=0.0):
    v = str(val).strip() if val is not None else ''
    if v == '':
        return default
    try:
        return float(v)
    except ValueError:
        return default


def clean_name(name):
    """Strip special characters; keep only alphanumeric (ITD requirement)."""
    return re.sub(r'[^A-Za-z0-9]', '', name)


def std_round(x):
    """Standard round-half-up to nearest integer (tax filing convention)."""
    import math
    return int(math.floor(x + 0.5))


def main():
    # Grab exact template header line to preserve it verbatim
    with open(TEMPLATE_FILE, 'r') as f:
        template_header = f.readline().rstrip('\n')

    output_rows = []

    with open(INPUT_FILE, 'r') as f:
        reader = csv.reader(f)
        in_long_term = False
        data_started = False

        for row in reader:
            if len(row) < 2:
                continue

            marker = row[1].strip()

            # Section detection
            if marker == 'Equity - Long Term':
                in_long_term = True
                data_started = False
                continue

            if marker in ('Equity - Buyback', 'Non Equity', 'Mutual Funds',
                          'F&O', 'Currency', 'Commodity'):
                in_long_term = False
                continue

            if not in_long_term:
                continue

            # Column header row inside the section
            if marker == 'Symbol':
                data_started = True
                continue

            if not data_started:
                continue

            # Skip totals / blank rows (no valid ISIN)
            isin = row[2].strip() if len(row) > 2 else ''
            if not marker or not isin.startswith('IN'):
                continue

            symbol = marker
            entry_date_str = row[3].strip() if len(row) > 3 else ''
            exit_date_str  = row[4].strip() if len(row) > 4 else ''

            entry_date = parse_date(entry_date_str)
            if entry_date is None:
                print(f"WARNING: unparseable entry date '{entry_date_str}' for {symbol} — skipped", file=sys.stderr)
                continue

            qty        = safe_float(row[5]  if len(row) > 5  else '')
            buy_value  = safe_float(row[6]  if len(row) > 6  else '')
            sell_value = safe_float(row[7]  if len(row) > 7  else '')
            fmv_per_sh = safe_float(row[10] if len(row) > 10 else '', default=0.0)

            # Charges — sum everything except STT (index 21)
            # Both old-format (brokerage col has total) and new-format (individual cols) work:
            brokerage  = safe_float(row[13] if len(row) > 13 else '')
            etc        = safe_float(row[14] if len(row) > 14 else '')  # Exchange Transaction Charges
            ipft       = safe_float(row[15] if len(row) > 15 else '')
            sebi       = safe_float(row[16] if len(row) > 16 else '')
            cgst       = safe_float(row[17] if len(row) > 17 else '')
            sgst       = safe_float(row[18] if len(row) > 18 else '')
            igst       = safe_float(row[19] if len(row) > 19 else '')
            stamp      = safe_float(row[20] if len(row) > 20 else '')
            # STT (index 21) excluded — not deductible under section 112A

            col12 = round(brokerage + etc + ipft + sebi + cgst + sgst + igst + stamp, 4)

            is_be = entry_date <= CUTOFF_DATE

            if is_be:
                # ── Pre-31-Jan-2018 acquisition (grandfathered) ──────────────────
                col1a = 'BE'
                col2  = isin
                col3  = clean_name(symbol)
                col4  = qty
                col5  = round(sell_value / qty, 4) if qty > 0 else 0.0
                col6  = std_round(col4 * col5)          # = round(sell_value)
                col8  = buy_value                        # total cost of acquisition
                col10 = fmv_per_sh                       # FMV per share on 31-Jan-2018 (0 if unavailable)
                col11 = std_round(col4 * col10)          # total FMV on 31-Jan-2018
                col9  = min(col6, col11)                 # lower of col6 & col11
                col7  = max(col8, col9)                  # higher of col8 & col9 (cost without indexation)
                col13 = std_round(col7 + col12)
                col14 = std_round(col6 - col13)

                output_rows.append([
                    col1a, col2, col3,
                    col4, col5, col6,
                    col7, col8, col9,
                    col10, col11,
                    col12, col13, col14, ''
                ])

            else:
                # ── Post-31-Jan-2018 acquisition ─────────────────────────────────
                # Per ITD instructions: ISIN=INNOTREQUIRD, Name=CONSOLIDATED,
                # qty and sale-price-per-share left blank, totals in col6 onwards.
                col1a = 'AE'
                col2  = 'INNOTREQUIRD'
                col3  = 'CONSOLIDATED'
                col4  = ''           # blank per instructions
                col5  = ''           # blank per instructions
                col6  = std_round(sell_value)
                col8  = buy_value    # total cost
                col9  = ''           # not applicable for AE
                col10 = ''           # blank per instructions
                col11 = ''           # not applicable for AE
                col7  = col8         # max(col8, 0) = col8 since col9 is N/A
                col13 = std_round(col7 + col12)
                col14 = std_round(col6 - col13)

                output_rows.append([
                    col1a, col2, col3,
                    col4, col5, col6,
                    col7, col8, col9,
                    col10, col11,
                    col12, col13, col14, ''
                ])

    # Write output CSV
    with open(OUTPUT_FILE, 'w', newline='') as f:
        f.write(template_header + '\n')
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerows(output_rows)

    # Summary
    be_rows = [r for r in output_rows if r[0] == 'BE']
    ae_rows = [r for r in output_rows if r[0] == 'AE']

    print(f"\nDone — {len(output_rows)} rows written to:\n  {OUTPUT_FILE}")
    print(f"\n  BE (acquired on/before 31-Jan-2018): {len(be_rows)} trades")
    print(f"  AE (acquired after 31-Jan-2018):     {len(ae_rows)} trades")

    print("\n── BE trades ──────────────────────────────────────")
    print(f"{'Symbol':<10} {'Entry Date':<14} {'Qty':>5} {'Sell':>10} {'Buy':>10} {'FMV/sh':>8} {'Col6':>8} {'Col7':>10} {'Col12':>8} {'Col13':>8} {'Col14':>8}")
    for r in output_rows:
        if r[0] == 'BE':
            print(f"{r[2]:<10} {'(pre-2018)  ':<14} {r[3]:>5} {r[5]:>10} {r[7]:>10} {r[9]:>8} {r[5]:>8} {r[6]:>10} {r[11]:>8} {r[12]:>8} {r[13]:>8}")

    print("\n── AE trades (first 10) ───────────────────────────")
    print(f"{'Row':>3} {'Col6 (Sell)':>12} {'Col7=Col8 (Buy)':>16} {'Col12':>8} {'Col13':>8} {'Col14':>8}")
    for i, r in enumerate(output_rows):
        if r[0] == 'AE' and i < 30:
            print(f"{i+1:>3} {r[5]:>12} {r[7]:>16} {r[11]:>8} {r[12]:>8} {r[13]:>8}")


if __name__ == '__main__':
    main()
