# Google Workspace CLI (`gws`): Gmail, Drive, Sheets, Docs, Calendar, Slides

CLI binary: `gws` — ensure the Node bin path is in PATH (see `.env`)
Docs: https://github.com/googleworkspace/cli

---

## Quoting rules — read this BEFORE any `--params` / `--json-values` call

1. **Never backslash-escape quotes in a shell command.** `\'` and `\"` are not
   JSON-style escapes in the shell; they break parsing (`unmatched "` errors).
2. Wrap JSON payloads in **single quotes** and write the JSON inside them
   plainly: `--params '{"q":"plain text"}'`.
3. If the JSON string itself must contain a **single quote** (e.g. Drive
   queries like `'FOLDER_ID' in parents`), do not try to shell-escape it —
   use the JSON unicode escape `'` instead:
   ```
   gws drive files list --params '{"q":"'FOLDER_ID' in parents"}'
   ```
4. For any **large or nested payload** (sheet values, doc content), do not
   inline JSON at all. Write it to a file in the workspace first, then
   substitute it:
   ```
   cat > payload.json <<'EOF'
   {"values": [["Category", "Amount"], ["Bills", 19350.27]]}
   EOF
   gws sheets +append --spreadsheet <ID> --json-values "$(cat payload.json)"
   ```
   (The `<<'EOF'` quoted heredoc passes the JSON through byte-for-byte — no
   escaping needed inside it.)
5. To create a spreadsheet from tabular data, prefer the deterministic skill
   `skills/sheet_from_csv.py` (see Sheets below) — it invokes `gws` without a
   shell, so quoting problems cannot happen.

---

## Gmail

Triage unread inbox:
```
gws gmail +triage [--max N] [--query 'GMAIL_SEARCH'] [--format table|json]
```

Read a message (MSG_ID from +triage or messages list):
```
gws gmail +read --id <MSG_ID> [--headers] [--format text|json]
```

Send:
```
gws gmail +send --to <EMAIL[,...]> --subject <SUBJECT> --body <TEXT> \
  [--cc <EMAIL>] [--bcc <EMAIL>] [--from <ALIAS>] [--html] [-a <FILE>] [--draft]
```

Reply / reply-all (threading automatic):
```
gws gmail +reply     --id <MSG_ID> --body <TEXT> [--html] [-a <FILE>]
gws gmail +reply-all --id <MSG_ID> --body <TEXT> [--html] [-a <FILE>]
```

Forward:
```
gws gmail +forward --id <MSG_ID> --to <EMAIL[,...]> --body <TEXT>
```

Raw API (search, trash, labels, etc.):
```
gws gmail users messages list  --params '{"userId":"me","q":"QUERY"}'
gws gmail users messages trash --params '{"userId":"me","id":"MSG_ID"}'
gws gmail users labels list    --params '{"userId":"me"}'
```

---

## Drive

Upload a file:
```
gws drive +upload <FILE> [--name <NAME>] [--parent <FOLDER_ID>]
```

List / get files:
```
gws drive files list --params '{"q":"name contains 'report'","pageSize":10}'
gws drive files list --params '{"q":"'FOLDER_ID' in parents"}'
gws drive files get  --params '{"fileId":"FILE_ID"}'
```

---

## Sheets

Create a spreadsheet from a CSV file (preferred — no shell quoting involved):
```
python3 skills/sheet_from_csv.py --title "TITLE" --csv data.csv
```
Save your data as a CSV file first (via a quoted heredoc or a script), then run
the skill; it prints the new spreadsheet's ID and URL. Use `--append-to <ID>`
to add rows to an existing spreadsheet instead of creating one.

Create an empty spreadsheet:
```
gws sheets spreadsheets create --json '{"properties":{"title":"TITLE"}}'
```

Read a range:
```
gws sheets +read --spreadsheet <ID> --range <RANGE>   # e.g. 'Sheet1!A1:D10'
```

Append rows:
```
gws sheets +append --spreadsheet <ID> --values 'Alice,100,true'
gws sheets +append --spreadsheet <ID> --json-values '[["a","b"],["c","d"]]'
```
For more than a few rows, put the JSON in a file and use
`--json-values "$(cat payload.json)"` (see Quoting rules above), or use
`skills/sheet_from_csv.py`.

---

## Docs

Append text to a document:
```
gws docs +write --document <DOC_ID> --text <TEXT>
```

Read a document:
```
gws docs documents get --params '{"documentId":"DOC_ID"}'
```

---

## Calendar

Show agenda:
```
gws calendar +agenda [--today] [--tomorrow] [--week] [--days N] \
  [--calendar <NAME|ID>] [--timezone <IANA_TZ>]
```

Create an event:
```
gws calendar +insert --summary <TITLE> --start <RFC3339> --end <RFC3339> \
  [--location <TEXT>] [--description <TEXT>] [--attendee <EMAIL>] [--meet]
```

Raw API:
```
gws calendar events list --params '{"calendarId":"primary","timeMin":"2026-01-01T00:00:00Z"}'
```

---

## Slides

To create a presentation with content, use `skills/slides_create.py` instead.

Get a presentation:
```
gws slides presentations get --params '{"presentationId":"PRES_ID"}'
```

---

## Tips
- IDs (file, doc, sheet, etc.) are in the URL: `https://docs.google.com/...d/<ID>/...`
- Use `--format json | jq` for scripting.
- Use `--dry-run` to validate without executing.
- Use `gws schema <service.resource.method>` to inspect any API's full parameter schema.
