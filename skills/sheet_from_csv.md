# Create a Google Spreadsheet from a CSV file

Turn tabular data into a Google Spreadsheet via the `gws` CLI.

## Steps
1. Write the data to a CSV file in the workspace with a quoted heredoc, e.g.
   `execute_shell("cat > data.csv <<'EOF' ... EOF")`. Use the quoted `'EOF'`
   so nothing is expanded.
2. Create a new spreadsheet, or append to an existing one:
   - New: `execute_shell("python3 skills/_sheet_from_csv.py --title \"TITLE\" --csv data.csv")`
   - Existing: `execute_shell("python3 skills/_sheet_from_csv.py --append-to SPREADSHEET_ID --csv data.csv")`
   - Add `--no-coerce` to keep every cell as text.
3. Reply with the spreadsheet URL from the script output (the line starting
   with `https://docs.google.com/spreadsheets/`).

## Rules
- Do not build JSON or escaped quotes in the shell. The helper passes the
  payload to `gws` as argv, so the CSV file is the only input to prepare.
- On an error, report the message as it is; do not retry with different
  quoting.
