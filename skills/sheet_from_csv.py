"""Create a Google Spreadsheet from a local CSV file via the gws CLI (no shell quoting needed).

Usage:
  python3 skills/sheet_from_csv.py --title "TITLE" --csv data.csv
  python3 skills/sheet_from_csv.py --append-to SPREADSHEET_ID --csv data.csv
  Options: [--no-coerce] [--gws /full/path/to/gws] [--dry-run]

Prepare the data as a CSV file first (e.g. with a quoted heredoc), then run this
skill. Without --append-to it creates a new spreadsheet titled --title and
appends all CSV rows; with --append-to it appends to an existing spreadsheet.
JSON payloads are passed to gws as argv (never through a shell), so quote
escaping problems cannot happen. Numeric-looking cells are converted to
numbers unless --no-coerce is given. Prints the spreadsheet ID and URL.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from typing import Any, List

GWS_DEFAULT = "gws"
# Rows per +append call; keeps each argv payload well under OS argument limits.
ROWS_PER_CALL = 500


def run(cmd: List[str], dry_run: bool) -> str:
    if dry_run:
        print("[dry-run] " + " ".join(cmd))
        return ""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print("Error: {}".format((result.stderr or result.stdout).strip()), file=sys.stderr)
        sys.exit(1)
    return result.stdout


def coerce(cell: str) -> Any:
    """Convert numeric-looking cells to numbers so Sheets stores them as such."""
    text = cell.strip()
    if not text:
        return ""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return cell


def read_rows(path: str, no_coerce: bool) -> List[List[Any]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        if no_coerce:
            return [list(row) for row in reader]
        return [[coerce(cell) for cell in row] for row in reader]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="path to the CSV file to import")
    parser.add_argument("--title", help="title for a new spreadsheet (required unless --append-to)")
    parser.add_argument("--append-to", help="existing spreadsheet ID to append to instead of creating")
    parser.add_argument("--no-coerce", action="store_true", help="keep all cells as strings")
    parser.add_argument("--gws", default=GWS_DEFAULT)
    parser.add_argument("--dry-run", action="store_true", help="print gws commands without executing")
    args = parser.parse_args()

    if not args.append_to and not args.title:
        parser.error("--title is required when creating a new spreadsheet")

    rows = read_rows(args.csv, args.no_coerce)
    if not rows:
        sys.exit("Error: {} contains no rows".format(args.csv))

    if args.append_to:
        sheet_id = args.append_to
    else:
        out = run([args.gws, "sheets", "spreadsheets", "create",
                   "--json", json.dumps({"properties": {"title": args.title}})], args.dry_run)
        if args.dry_run:
            sheet_id = "DRY_RUN_ID"
        else:
            try:
                sheet_id = json.loads(out)["spreadsheetId"]
            except (ValueError, KeyError):
                sys.exit("Error: could not parse spreadsheetId from create output:\n{}".format(out.strip()))

    for start in range(0, len(rows), ROWS_PER_CALL):
        chunk = rows[start:start + ROWS_PER_CALL]
        run([args.gws, "sheets", "+append", "--spreadsheet", sheet_id,
             "--json-values", json.dumps(chunk)], args.dry_run)

    print("spreadsheetId: {}".format(sheet_id))
    print("https://docs.google.com/spreadsheets/d/{}/edit".format(sheet_id))


if __name__ == "__main__":
    main()
