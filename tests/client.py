#!/usr/bin/env python3
"""Tiny test client: client.py <port> <tool> '<json args>'   (token read from ~/.config/code-gateway/secret)"""
import json, os, sys, urllib.request
from pathlib import Path
port, tool, args = sys.argv[1], sys.argv[2], json.loads(sys.argv[3] if len(sys.argv) > 3 else "{}")
tok = Path(os.environ.get("CODE_GATEWAY_TOKEN_FILE", "~/.config/code-gateway/secret")).expanduser().read_text().strip()
def rpc(method, params=None, i=1):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="POST",
        data=json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}).encode(),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    return json.load(urllib.request.urlopen(req, timeout=330))
if tool == "_tools":
    print(json.dumps([t["name"] for t in rpc("tools/list")["result"]["tools"]]))
else:
    r = rpc("tools/call", {"name": tool, "arguments": args})["result"]
    print(("ISERROR " if r.get("isError") else "") + r["content"][0]["text"])
