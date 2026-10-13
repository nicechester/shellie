# MCP server setup

Shellie can call tools on remote MCP (Model Context Protocol) servers. This guide
explains the setup types and walks through a Google Drive example.

## Overview

- Each remote tool is exposed to the LLM as `mcp_<server>_<tool>`. Its description
  starts with `[MCP:<server>]`, so replies can be traced to their source.
- Supported transport: **Streamable HTTP** only. Each call is a JSON POST to the
  server URL.
- Not supported: stdio servers, and SSE responses (`text/event-stream`). Those are
  skipped with a warning in the log.
- Tool lists are cached for 300 seconds. Changes to a server config apply on the
  next LLM call.

Servers are configured in the `MCP_SERVERS` setting, a JSON array:

```json
[
  {
    "name": "svc",
    "url": "https://mcp.example.com/",
    "headers": {"Authorization": "Bearer <token>"}
  }
]
```

`name` is 1–32 characters: letters, digits, `-` and `_`. Names must be unique.

## Setup types

| Type | What you set | Where it is added | Secret stored |
| --- | --- | --- | --- |
| No auth | `name`, `url` | Chat (proposal) or web settings | None |
| Static header | `name`, `url`, `headers` (e.g. `Authorization: Bearer …`) | Web settings only | Header value |
| OAuth, public client | `name`, `url`, `oauth` without `client_secret` | Chat (proposal), then `/mcp auth` | Refresh token, in the token file |
| OAuth, client secret | `name`, `url`, `oauth` with `client_secret` | Web settings only, then `/mcp auth` | Client secret, in settings |

The chat path never accepts headers or a client secret. Those stay in the web
settings page, which only listens on the local machine.

## Where each setup is done

**From Telegram chat**

- Ask in plain words, e.g. `add MCP server test at https://example.com/mcp`.
  Shellie replies with a 🔌 proposal and ✅ Confirm / ✖ Cancel buttons. Nothing
  changes until you tap Confirm. Proposals expire after 10 minutes.
- For an OAuth server, Confirm also sends the approval link.
- `/mcp` shows the login state of OAuth servers.
- `/mcp auth <server>` sends an approval link, valid for 15 minutes.
- `/mcp finish <server> <URL>` completes an OAuth login from the URL you pasted back.
- `/mcp logout <server>` forgets a stored login.

**From the local web settings page**

The settings page listens only on `127.0.0.1` and has no login. Reach it from
another machine with an SSH tunnel:

```
ssh -L 8321:127.0.0.1:8321 <pi-user>@<pi-host>
```

Then open `http://127.0.0.1:8321` on your laptop and edit `MCP_SERVERS`. Keep the
local port at 8321. The server rejects any other `Host` header.

**Directly on the Pi**

`MCP_SERVERS` can also go in the `.env` file in the repo folder, as one line with
no quotes around the JSON. The file is read at startup, so restart Shellie after
editing it. If `MCP_SERVERS` is later saved from the web page, that copy in
`settings.json` takes precedence.

## OAuth login flow

Shellie runs headless, so the redirect can't land inside the app. Login is
copy-paste based:

1. `/mcp auth <server>` sends an approval link.
2. Open the link on your own device and approve access.
3. The browser then fails to load the `redirect_uri` page. That's expected.
4. Copy the full address from the address bar and send `/mcp finish <server> <URL>`.
5. Check with `/mcp`. It should show `connected`.

Shellie requests offline access, so Google issues a refresh token and the access
token renews without another login. A login made before this was added has no
refresh token: run `/mcp logout <server>`, then `/mcp auth <server>` again.

The `redirect_uri` must match a value registered with the provider exactly.
`client_secret` must be entered in the web settings, not in chat.

`authorization_url` and `token_url` must be `https://`. Plain `http://` is
accepted only for `localhost` and loopback addresses.

## Example: Google Drive MCP server

Google's Drive MCP server is in Developer Preview. Your Google account may need
access to the preview program before calls succeed.

Server URL: `https://drivemcp.googleapis.com/mcp/v1`

### 1. Enable the APIs

In the Google Cloud project, enable both:

- `drive.googleapis.com` (Google Drive API)
- `drivemcp.googleapis.com` (Google Drive MCP API)

### 2. Configure the OAuth consent screen

In **Google Auth Platform → Branding**, set the app name to `Drive MCP Server`.

Under **Audience**, choose **Internal** if you're on a Workspace organization.
Otherwise choose **External** and add your Google account as a test user.

Under **Data Access**, add these scopes and save:

- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/drive.file`

`drive.readonly` lets the tools read your Drive. `drive.file` only covers files the
app created or that you opened with it, so `copy_file` and `create_file` may fail
on other files.

### 3. Create an OAuth client

In **Google Auth Platform → Clients**, click **Create client**, then:

- Type: **Web application**
- Authorized redirect URI: `http://localhost:8787/callback`

Click **Create**. The dialog shows the **Client ID** and **Client secret**. The
secret can be viewed again from the client's page later.

### 4. Add the server

Add this entry to `MCP_SERVERS` (web settings, or `.env` on the Pi), replacing the
two placeholders:

```json
[{"name":"gdrive","url":"https://drivemcp.googleapis.com/mcp/v1","oauth":{"authorization_url":"https://accounts.google.com/o/oauth2/v2/auth","token_url":"https://oauth2.googleapis.com/token","redirect_uri":"http://localhost:8787/callback","client_id":"YOUR_CLIENT_ID.apps.googleusercontent.com","client_secret":"YOUR_CLIENT_SECRET","scope":"https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file"}}]
```

Restart Shellie if you edited `.env`.

### 5. Log in and test

In Telegram:

1. `/mcp auth gdrive`, then open the link and approve.
2. Copy the address from the failed redirect page and send `/mcp finish gdrive <URL>`.
3. `/mcp` should show `gdrive: connected`.
4. Ask: `list my 5 most recent Drive files`.

Tools exposed by this server: `copy_file`, `create_file`, `download_file_content`,
`get_file_metadata`, `get_file_permissions`, `list_recent_files`,
`read_file_content`, `search_files`.

Text the tools read is sent to the LLM as context. Be careful with documents that
contain instructions you didn't write.

## Troubleshooting

- **Server missing from `/mcp`**: check the JSON in `MCP_SERVERS` and the log with
  `/systemlog 50`. Invalid entries are ignored at startup.
- **`initialization failed` or `tools/list failed` in the log**: the URL, headers,
  or token are wrong, or the server doesn't speak Streamable HTTP.
- **`SSE responses are not supported`**: the server uses SSE. Shellie can't use it.
- **`redirect_uri` mismatch from the provider**: the value in the config doesn't
  exactly match the one registered in the provider's console.
- **`State does not match`**: the pasted address came from an older approval link.
  Run `/mcp auth <server>` again and use the newest link.
- **Login expires after an hour**: the login predates offline access. Run
  `/mcp logout <server>`, then `/mcp auth <server>`.
- **403 from an OpenAI-compatible host**: some hosts block Python's default
  User-Agent. Shellie now sends `Shellie/<version>`. Restart after updating.

## Sources

- Google Drive MCP server reference: https://developers.google.com/workspace/drive/api/reference/mcp
- Configure the Drive MCP server: https://developers.google.com/workspace/drive/api/guides/configure-mcp-server
