# DJ Treta MCP SSE Server

Remote MCP server exposing DJ Treta's state + controls. Lets Manish
(and other Beings) talk to the live DJ, see what she's playing, change
her mood, skip tracks, and — once Phase 7 lands — co-DJ alongside her.

## Endpoint

```
https://mcp.dj.treta.life/sse
```

Requires `Authorization: Bearer $DJTRETA_MCP_TOKEN`.

Health check (no auth): `https://mcp.dj.treta.life/health`

## Tools

| Tool                | Kind  | Purpose                                        |
|---------------------|-------|------------------------------------------------|
| `dj_status`         | read  | Compact now-playing snapshot                   |
| `dj_playlist`       | read  | Ranked upcoming candidates                     |
| `dj_session_state`  | read  | Full session.json (verbose, debug)             |
| `dj_talk`           | write | Conversational intent to Being agent           |
| `dj_set_mood`       | write | Change mood + trigger replan                   |
| `dj_skip`           | write | Skip current track (fast / smooth)             |
| `dj_request_track`  | write | Queue a specific artist/title                  |
| `dj_take_deck`      | hook  | Reserve a deck for co-being control (Phase 7)  |
| `dj_release_deck`   | hook  | Release a previously-taken deck                |
| `dj_load_on_deck`   | hook  | Load a track on a reserved deck                |

## Architecture

```
Client (Claude Desktop, Himani, Serra)
    │  Bearer token over TLS
    ▼
nginx  mcp.dj.treta.life:443  →  127.0.0.1:8765
    │
    ▼
dj-treta-mcp.service  (FastMCP SSE + Starlette auth middleware)
    │
    ├─ read  → /mnt/data/dj-treta/.beings/session.json  (atomic reads)
    │         + /tmp/dj-treta-state.json
    │
    └─ write → /tmp/dj-treta-command.json  (agent heartbeat consumes)
              Narrow signals (user_skip, library_need) flow through
              command-file handlers on the daemon side — never direct
              writes to session.json.
```

**Key invariant**: session.json is owned by the agent's Session singleton.
The MCP server reads it but never writes it directly. All mutations go
through the command file, the same way the TUI talks to the daemon.

## Client config — Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dj-treta": {
      "url": "https://mcp.dj.treta.life/sse",
      "transport": "sse",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN_HERE"
      }
    }
  }
}
```

## Client config — Himani / Claude Code (`~/.claude.json`)

```json
{
  "mcpServers": {
    "dj-treta": {
      "type": "sse",
      "url": "https://mcp.dj.treta.life/sse",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN_HERE"
      }
    }
  }
}
```

## Operations

```bash
# Service control
sudo systemctl status dj-treta-mcp
sudo systemctl restart dj-treta-mcp    # music keeps playing — agent untouched

# Logs
tail -f /mnt/data/logs/dj-treta-mcp.log

# Rotate token
python -c 'import secrets; print(secrets.token_urlsafe(32))' \
  | sudo tee -a /mnt/data/dj-treta/.env.new
# edit .env, restart service, update all clients
```

## Music-never-stop guarantee

The MCP server is a separate systemd unit from the agent. Restarting
`dj-treta-mcp` never touches `dj-treta-agent`, `dj-treta-mixxx`, or
`dj-treta-stream`. Audio continues uninterrupted.
