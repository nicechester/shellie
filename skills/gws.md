# Google Workspace CLI (`gws`): Gmail, Drive, Sheets, Docs, Calendar, Slides

CLI binary: `gws` — ensure the Node bin path is in PATH (see `.env`)
Docs: https://github.com/googleworkspace/cli

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
gws drive files list --params '{"q":"name contains '\''report'\''","pageSize":10}'
gws drive files get  --params '{"fileId":"FILE_ID"}'
```

---

## Sheets

Read a range:
```
gws sheets +read --spreadsheet <ID> --range <RANGE>   # e.g. 'Sheet1!A1:D10'
```

Append rows:
```
gws sheets +append --spreadsheet <ID> --values 'Alice,100,true'
gws sheets +append --spreadsheet <ID> --json-values '[[\"a\",\"b\"],[\"c\",\"d\"]]'
```

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
