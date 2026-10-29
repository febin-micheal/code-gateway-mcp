#!/usr/bin/env python3
"""Checks the sandbox against a running gateway:  python3 tests/test_sandbox.py <port>
Reads the token from $CODE_GATEWAY_TOKEN_FILE (default ~/.config/code-gateway/secret)."""
import json, os, sys, urllib.request
from pathlib import Path

port = sys.argv[1] if len(sys.argv) > 1 else "8790"
tok = Path(os.environ.get("CODE_GATEWAY_TOKEN_FILE", "~/.config/code-gateway/secret")).expanduser().read_text().strip()


def tool(name, args):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="POST",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).encode(),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json", "Accept": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=120))["result"]
    return r["content"][0]["text"], r.get("isError", False)


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    return ok


results = []
txt, _ = tool("run_code", {"code": "console.log('hi'); return 1+1"})
results.append(check("basic script runs", "hi" in txt and "→ 2" in txt))
txt, _ = tool("run_code", {"code": "try { await fetch('https://example.com'); return 'REACHED' } catch (e) { return 'blocked' }"})
results.append(check("no internet", "blocked" in txt and "REACHED" not in txt))
home = os.path.expanduser("~")
txt, _ = tool("run_code", {"code": "const fs = process.mainModule.require('fs'); const o = {}; for (const p of [" + json.dumps(home) + ", '/etc/passwd']) { try { fs.statSync(p); o[p] = 'VISIBLE' } catch (e) { o[p] = e.code } } return o"})
results.append(check("host files not visible", "VISIBLE" not in txt))
txt, _ = tool("run_code", {"code": "const net = process.mainModule.require('net'); return await new Promise(r => { const s = net.connect(" + port + ", '127.0.0.1'); s.on('connect', () => r('CONNECTED')); s.on('error', e => r(e.code)); })"})
results.append(check("gateway not reachable from inside the sandbox", "CONNECTED" not in txt))
txt, err = tool("run_code", {"code": "await new Promise(r => setTimeout(r, 30000))", "timeout_s": 3})
results.append(check("time limit enforced", err and "time limit" in txt))
txt, _ = tool("run_code", {"code": "try { await api.nope.x({}) } catch (e) { return e.message }"})
results.append(check("unknown connector gives a clear error", "unknown connector" in txt))
sys.exit(0 if all(results) else 1)
