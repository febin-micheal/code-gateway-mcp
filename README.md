# code-gateway-mcp

One MCP server that puts **many MCP servers behind two tools**, so an AI chat can work with them by writing code.

Instead of showing the model hundreds of tool definitions, it sees:

| Tool | What it does |
|---|---|
| `list_api` | Lists your connectors and tools. With no arguments: a short index. With `connectors` or `tools`: full TypeScript-style input types for just those. |
| `run_code` | Runs a JavaScript snippet in a sandbox with **no network and no files**. The snippet calls your connectors with `await api.<connector>.<tool>(input)`. Only what it prints or returns comes back to the model. |

The idea comes from Cloudflare's post [Code Mode: the better way to use MCP](https://blog.cloudflare.com/code-mode/).
This is an independent, small implementation (Python standard library plus Node). It uses no Cloudflare code and is not
affiliated with Cloudflare or Anthropic.

## Why

- **Less context.** A server with 60+ tools costs a lot of tokens just to list. Here the model reads a short index and asks for full types only for the tools it needs.
- **Chains stay out of the model.** "Search, filter, then fetch each hit" is one script. The middle results never pass through the model.
- **Secrets stay out of the generated code.** Tokens live in the gateway. The script runs in a sandbox that cannot read them and cannot reach the network.
- **Your existing servers are not changed.** The gateway is just another MCP client of each of them.

## How it works

```
chat (MCP client)  --bearer-->  code-gateway  --MCP-->  your connector A
                                   |  ^                  your connector B
                       pipe only   v  |                  ...
                              [ bubblewrap sandbox: node, no network, no host files ]
```

1. `run_code` starts `node runner.js` inside [bubblewrap](https://github.com/containers/bubblewrap) with `--unshare-all` (no network, new PID/user/IPC namespaces), a read-only `/usr`, an empty `/tmp`, and a clean environment.
2. The script talks to the gateway only through stdin/stdout. When it calls `api.x.y(...)`, the gateway makes the real call, with the right token, and sends the result back.
3. Each script has a time limit, a call limit and an output cap. Every call is logged.

## Requirements

- Linux with `bwrap` (bubblewrap) and user namespaces enabled
- Python 3.8+ (standard library only) and Node.js 18+
- Your MCP servers must speak streamable HTTP (JSON or event-stream replies both work)

## Setup

```bash
git clone <this repo> ~/code-gateway-mcp
mkdir -p ~/.config/code-gateway && chmod 700 ~/.config/code-gateway
cp ~/code-gateway-mcp/config.example.json ~/.config/code-gateway/config.json   # edit: list your connectors
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.config/code-gateway/secret
chmod 600 ~/.config/code-gateway/config.json ~/.config/code-gateway/secret
python3 ~/code-gateway-mcp/gateway.py
```

Run it as a service with `systemd/code-mcp.service` (`systemctl --user enable --now code-mcp`).

Add it to your chat app as a custom MCP connector: URL `http(s)://<host>/mcp`, header `Authorization: Bearer <the secret>`.
Anything without that header gets `401`. The gateway listens on loopback by default; put your own TLS tunnel or reverse proxy in front if you need it reachable.

## Configuration

```json
{
  "listen": "127.0.0.1:8790",
  "token_file": "~/.config/code-gateway/secret",
  "connectors": {
    "tickets": { "url": "http://127.0.0.1:9002/mcp", "bearer_file": "~/.config/tickets-mcp/token" },
    "chat":    { "url": "http://127.0.0.1:9003/mcp", "bearer_env_file": "~/.config/chat-mcp/env", "bearer_env_key": "CHAT_MCP_TOKEN" },
    "wiki":    { "url": "http://127.0.0.1:9004/{secret}/mcp", "url_secret_file": "~/.config/wiki-mcp/secret" }
  }
}
```

Per connector: `url`, and optionally `bearer_file` (file holding the token), `bearer_env_file` + `bearer_env_key` (a `KEY=value` file), or `url_secret_file` (replaces `{secret}` in the URL). Tokens are read from disk on every call, so rotating one needs no restart.
Limits (`timeout_s`, `max_timeout_s`, `max_calls`, `max_output_chars`, `memory_mb`, `call_timeout_s`) are in `config.example.json`.

Set `CODE_GATEWAY_CONFIG` to use a config file somewhere else.

## Writing scripts

The code is the body of an async function: top-level `await` and `return` work, and `console.log` output is returned.
Write plain JavaScript. The types shown by `list_api` are documentation only.

```js
const hits = await api.tickets.search({ query: "login bug" });
const open = hits.filter(h => h.status !== "done").slice(0, 5);
const details = await Promise.all(open.map(h => api.tickets.get({ id: h.id })));
return details.map(d => ({ id: d.id, title: d.title }));
```

Tool results are parsed for you: JSON text becomes an object, other text stays a string, images become `{_type: "image", mimeType, bytes}`. A failing call throws an `Error`.

## Things to know

- **Approvals collapse.** Your chat app asks to approve `run_code` as a whole, not each inner call. If one of your connectors can do something destructive (send, delete, run shell commands), either leave it out of the config or only add it when you are happy for scripts to use it. Every inner call is written to the log file (connector, tool, first 200 characters of the arguments, result, time).
- **The sandbox is the safety layer.** Check it with `python3 tests/test_sandbox.py <port>`: it confirms there is no internet, no host files, no way back to the gateway, a working time limit, and clear errors.
- **No internet from scripts by design.** If a script needs a web page, give it a connector that fetches pages.
- Linux only. It was written for a small home setup, not hardened for hostile multi-user hosting.

## License

MIT
