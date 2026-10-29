#!/usr/bin/env python3
"""code-gateway: one MCP server that turns many MCP servers into a code API.

Instead of showing a model hundreds of tools, it shows two:
  list_api  - the other servers' tools as a TypeScript-style API
  run_code  - runs a JavaScript snippet in a no-network sandbox; the snippet
              calls the other servers through `api.<connector>.<tool>(input)`
              and only its printed output / return value comes back.

Standard library only (Python 3.8+). The sandbox needs `bwrap` (bubblewrap) and `node`.
"""
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import itertools
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror, request as urlrequest

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
RUNNER = HERE / "runner.js"
CONFIG_PATH = Path(os.environ.get("CODE_GATEWAY_CONFIG", "~/.config/code-gateway/config.json")).expanduser()


def xp(p):
    return Path(str(p)).expanduser()


def read_secret(p):
    return xp(p).read_text().strip()


def read_env_value(path, key):
    for line in xp(path).read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("'\"")
    raise KeyError(f"{key} not found in {path}")


CFG = json.loads(CONFIG_PATH.read_text())
LIMITS = {
    "timeout_s": 60,          # default time a script may run
    "max_timeout_s": 300,     # the most a caller may ask for
    "max_calls": 200,         # connector calls allowed per script
    "max_output_chars": 100000,
    "memory_mb": 512,         # node heap limit inside the sandbox
    "call_timeout_s": 60,     # one connector call
    "tools_cache_s": 60,
}
LIMITS.update(CFG.get("limits", {}))
LOG_FILE = xp(CFG.get("log_file", "~/.local/state/code-gateway/calls.log"))
TOKEN_FILE = xp(CFG.get("token_file", "~/.config/code-gateway/secret"))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_lock = threading.Lock()


def log_event(obj):
    obj = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **obj}
    with _log_lock:
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


class BackendError(Exception):
    pass


# --------------------------------------------------------------------------- MCP client

_NOPROXY = urlrequest.build_opener(urlrequest.ProxyHandler({}))


def parse_body(ctype, body, want_id):
    if "text/event-stream" in ctype:
        for block in re.split(r"\r?\n\r?\n", body):
            data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
            if not data.strip():
                continue
            try:
                m = json.loads(data)
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("id") == want_id and ("result" in m or "error" in m):
                return m
        raise BackendError("no response found in event stream")
    m = json.loads(body)
    if isinstance(m, list):
        for x in m:
            if isinstance(x, dict) and x.get("id") == want_id:
                return x
        raise BackendError("no matching response in batch")
    return m


def simplify(result):
    """Turn an MCP tools/call result into something easy to use from a script."""
    if result.get("isError"):
        texts = [p.get("text", "") for p in result.get("content", []) if p.get("type") == "text"]
        raise BackendError("\n".join(texts) or "tool returned an error")
    sc = result.get("structuredContent")
    if sc is not None:
        return sc
    out = []
    for p in result.get("content", []):
        t = p.get("type")
        if t == "text":
            txt = p.get("text", "")
            try:
                out.append(json.loads(txt))
            except ValueError:
                out.append(txt)
        elif t == "image":
            out.append({"_type": "image", "mimeType": p.get("mimeType"), "bytes": len(p.get("data", "")) * 3 // 4})
        elif t == "audio":
            out.append({"_type": "audio", "mimeType": p.get("mimeType"), "bytes": len(p.get("data", "")) * 3 // 4})
        else:
            out.append(p)
    return out[0] if len(out) == 1 else out


class Backend:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.lock = threading.Lock()
        self.ready = False
        self.session = None
        self.proto = None
        self._ids = itertools.count(1)
        self._tools = None
        self._tools_at = 0.0

    def url(self):
        u = self.cfg["url"]
        if "url_secret_file" in self.cfg:
            u = u.replace("{secret}", read_secret(self.cfg["url_secret_file"]))
        return u

    def bearer(self):
        c = self.cfg
        if "bearer_file" in c:
            return read_secret(c["bearer_file"])
        if "bearer_env_file" in c:
            return read_env_value(c["bearer_env_file"], c["bearer_env_key"])
        return None

    def _post(self, payload, timeout):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        tok = self.bearer()
        if tok:
            headers["Authorization"] = "Bearer " + tok
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.proto:
            headers["MCP-Protocol-Version"] = self.proto
        req = urlrequest.Request(self.url(), data=json.dumps(payload).encode(), headers=headers, method="POST")
        with _NOPROXY.open(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.session = sid
            return r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")

    def _reset(self):
        self.ready = False
        self.session = None

    def _init(self, timeout):
        with self.lock:
            if self.ready:
                return
            rid = next(self._ids)
            ct, body = self._post({"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "code-gateway", "version": VERSION}}}, timeout)
            msg = parse_body(ct, body, rid)
            if "error" in msg:
                raise BackendError(f"initialize failed: {msg['error']}")
            self.proto = (msg.get("result") or {}).get("protocolVersion")
            try:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout)
            except urlerror.HTTPError:
                pass
            self.ready = True

    def rpc(self, method, params, timeout=None):
        to = timeout or LIMITS["call_timeout_s"]
        for attempt in (1, 2):
            try:
                self._init(to)
                rid = next(self._ids)
                ct, body = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, to)
                msg = parse_body(ct, body, rid)
            except urlerror.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if attempt == 1 and e.code in (400, 404) and self.session:
                    self._reset()
                    continue
                raise BackendError(f"{self.name}: HTTP {e.code} {detail}")
            except urlerror.URLError as e:
                raise BackendError(f"{self.name}: unreachable ({e.reason})")
            except (OSError, ValueError) as e:
                raise BackendError(f"{self.name}: {e}")
            if "error" in msg:
                if attempt == 1 and "session" in json.dumps(msg["error"]).lower():
                    self._reset()
                    continue
                raise BackendError(f"{self.name}: {msg['error'].get('message', msg['error'])}")
            return msg.get("result") or {}
        raise BackendError(f"{self.name}: failed")

    def tools(self):
        now = time.time()
        if self._tools is not None and now - self._tools_at < LIMITS["tools_cache_s"]:
            return self._tools
        out, cursor = [], None
        while True:
            res = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            out += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self._tools, self._tools_at = out, now
        return out

    def call(self, tool, args):
        return simplify(self.rpc("tools/call", {"name": tool, "arguments": args or {}}))


BACKENDS = {n: Backend(n, c) for n, c in CFG["connectors"].items()}

# --------------------------------------------------------------------------- TypeScript-style API text

IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def jsdoc(text, indent, limit):
    if not text:
        return ""
    t = " ".join(str(text).split()).replace("*/", "* /")
    if len(t) > limit:
        t = t[: limit - 1] + "…"
    return f"{indent}/** {t} */\n"


def ts_type(s, root, depth=0, indent="  "):
    if not isinstance(s, dict) or depth > 6:
        return "unknown"
    if "$ref" in s and isinstance(s["$ref"], str) and s["$ref"].startswith("#/"):
        node = root
        for part in s["$ref"][2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return ts_type(node, root, depth + 1, indent)
    if "enum" in s:
        return " | ".join(json.dumps(v) for v in s["enum"][:30]) or "never"
    if "const" in s:
        return json.dumps(s["const"])
    for key in ("anyOf", "oneOf"):
        if key in s:
            parts = list(dict.fromkeys(ts_type(x, root, depth + 1, indent) for x in s[key]))
            return " | ".join(parts) or "unknown"
    if "allOf" in s:
        return " & ".join("(" + ts_type(x, root, depth + 1, indent) + ")" for x in s["allOf"]) or "unknown"
    t = s.get("type")
    if isinstance(t, list):
        return " | ".join(dict.fromkeys(ts_type({**s, "type": x}, root, depth + 1, indent) for x in t))
    if t == "string":
        return "string"
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "null":
        return "null"
    if t == "array":
        inner = ts_type(s.get("items", {}), root, depth + 1, indent)
        return f"({inner})[]" if re.search(r"[ |&]", inner) else f"{inner}[]"
    if t == "object" or "properties" in s:
        props = s.get("properties") or {}
        req = set(s.get("required") or [])
        lines = []
        for k, v in props.items():
            key = k if IDENT.match(k) else json.dumps(k)
            doc = jsdoc(v.get("description") if isinstance(v, dict) else None, indent + "  ", 200)
            lines.append(f"{doc}{indent}  {key}{'' if k in req else '?'}: {ts_type(v, root, depth + 1, indent + '  ')};")
        ap = s.get("additionalProperties")
        if isinstance(ap, dict):
            lines.append(f"{indent}  [key: string]: {ts_type(ap, root, depth + 1, indent + '  ')};")
        elif ap is True and not props:
            return "Record<string, unknown>"
        if not lines:
            return "Record<string, unknown>"
        return "{\n" + "\n".join(lines) + f"\n{indent}}}"
    return "unknown"


def ts_tool(tool, indent="    "):
    name = tool["name"]
    key = name if IDENT.match(name) else json.dumps(name)
    inp = tool.get("inputSchema") or {}
    opt = "?" if not inp.get("required") else ""
    return jsdoc(tool.get("description"), indent, 600) + f"{indent}{key}(input{opt}: {ts_type(inp, inp, 0, indent)}): Promise<unknown>;"


def first_sentence(text, limit=110):
    t = " ".join((text or "").split())
    m = re.search(r"(?<=[.!?])\s", t)
    if m:
        t = t[: m.start()]
    return t if len(t) <= limit else t[: limit - 1] + "…"


def tools_of(name):
    try:
        return BACKENDS[name].tools(), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def list_api(args):
    wanted_conn = args.get("connectors") or []
    wanted_tools = args.get("tools") or []
    unknown = [c for c in wanted_conn if c not in BACKENDS]
    if unknown:
        raise BackendError(f"unknown connector(s) {unknown}; available: {sorted(BACKENDS)}")
    if not wanted_conn and not wanted_tools:
        out = ["Connectors and tools. Inside run_code call them as `await api.<connector>.<tool>(input)`.",
               "For full input types call list_api again with `connectors: [\"name\"]` or `tools: [\"name.tool\"]`.", ""]
        for name in BACKENDS:
            ts, err = tools_of(name)
            if err:
                out.append(f"## {name} — UNREACHABLE: {err}")
                continue
            out.append(f"## {name} ({len(ts)} tools)")
            out += [f"- {t['name']} — {first_sentence(t.get('description'))}" for t in ts]
            out.append("")
        return "\n".join(out)
    by_conn = {}
    for c in wanted_conn:
        ts, err = tools_of(c)
        by_conn.setdefault(c, []).extend(ts or [])
        if err:
            by_conn[c] = err
    for full in wanted_tools:
        c, _, tn = full.partition(".")
        if c not in BACKENDS:
            raise BackendError(f"unknown connector in '{full}'")
        ts, err = tools_of(c)
        if err:
            by_conn[c] = err
            continue
        if isinstance(by_conn.get(c), list) and c in wanted_conn:
            continue
        by_conn.setdefault(c, []).extend(t for t in ts if t["name"] == tn)
    lines = ["// Write plain JavaScript in run_code. Call: await api.<connector>.<tool>(input)",
             "// Types below are documentation only.", "declare const api: {"]
    for c, ts in by_conn.items():
        if isinstance(ts, str):
            lines.append(f"  // {c}: UNREACHABLE: {ts}")
            continue
        lines.append(f"  {c}: {{")
        lines += [ts_tool(t) for t in ts]
        lines.append("  };")
    lines.append("};")
    return "\n".join(lines)


# --------------------------------------------------------------------------- sandbox

def sandbox_cmd(node):
    cmd = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
           "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/tmp",
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr"]
    for d in ("/bin", "/sbin", "/lib", "/lib64", "/lib32"):
        if os.path.islink(d):
            cmd += ["--symlink", os.readlink(d), d]
        elif os.path.isdir(d):
            cmd += ["--ro-bind", d, d]
    if os.path.exists("/etc/ld.so.cache"):
        cmd += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    real = os.path.realpath(node)
    if not real.startswith("/usr/"):
        prefix = str(Path(real).parent.parent)
        cmd += ["--ro-bind", prefix, prefix]
    cmd += ["--ro-bind", str(RUNNER), "/sandbox/runner.js", "--chdir", "/tmp",
            real, f"--max-old-space-size={int(LIMITS['memory_mb'])}", "/sandbox/runner.js"]
    return cmd


def run_code(args):
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        raise BackendError("`code` is required")
    timeout_s = min(int(args.get("timeout_s") or LIMITS["timeout_s"]), int(LIMITS["max_timeout_s"]))
    node = shutil.which("node")
    if not node or not shutil.which("bwrap"):
        raise BackendError("sandbox unavailable: node and bwrap must be installed")
    run_id = uuid.uuid4().hex[:8]
    t0 = time.time()
    proc = subprocess.Popen(sandbox_cmd(node), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, start_new_session=True)
    stdin_lock, state_lock = threading.Lock(), threading.Lock()
    logs, stderr_buf = [], []
    st = {"calls": 0, "done": None, "timed_out": False}

    def send(obj):
        with stdin_lock:
            try:
                proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def on_timeout():
        st["timed_out"] = True
        try:
            proc.kill()
        except OSError:
            pass

    timer = threading.Timer(timeout_s, on_timeout)
    timer.start()
    threading.Thread(target=lambda: stderr_buf.append(proc.stderr.read(4000)), daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=8)

    def handle_call(msg):
        cid, conn, tool, a = msg.get("id"), msg.get("connector"), msg.get("tool"), msg.get("args") or {}
        t = time.time()
        ok, err = True, None
        try:
            with state_lock:
                st["calls"] += 1
                over = st["calls"] > int(LIMITS["max_calls"])
            if over:
                raise BackendError(f"call limit reached ({LIMITS['max_calls']} per script)")
            b = BACKENDS.get(conn)
            if not b:
                raise BackendError(f"unknown connector '{conn}'; available: {sorted(BACKENDS)}")
            send({"type": "result", "id": cid, "ok": True, "value": b.call(tool, a)})
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)
            send({"type": "result", "id": cid, "ok": False, "error": err})
        log_event({"event": "call", "run": run_id, "connector": conn, "tool": tool, "ok": ok,
                   "ms": int((time.time() - t) * 1000), "args": json.dumps(a, ensure_ascii=False)[:200],
                   **({"error": err[:200]} if err else {})})

    send({"type": "run", "code": code, "connectors": list(BACKENDS)})
    for line in proc.stdout:
        line = line.rstrip("\n")
        try:
            msg = json.loads(line)
        except ValueError:
            logs.append(line)
            continue
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "log":
            logs.append(msg.get("text", ""))
        elif kind == "call":
            pool.submit(handle_call, msg)
        elif kind == "done":
            st["done"] = msg
        else:
            logs.append(line)
    timer.cancel()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    pool.shutdown(wait=True)

    parts = list(logs)
    is_error = False
    done = st["done"]
    if st["timed_out"]:
        parts.append(f"Error: script stopped after {timeout_s}s (time limit).")
        is_error = True
    elif done is None:
        parts.append("Error: sandbox exited without a result. " + " ".join(stderr_buf)[:500])
        is_error = True
    elif not done.get("ok"):
        parts.append("Error: " + str(done.get("error", "unknown error")))
        is_error = True
    elif done.get("hasValue"):
        v = done.get("value")
        parts.append("→ " + (v if isinstance(v, str) else json.dumps(v, indent=2, ensure_ascii=False)))
    ms = int((time.time() - t0) * 1000)
    text = "\n".join(parts)
    cap = int(LIMITS["max_output_chars"])
    if len(text) > cap:
        text = text[:cap] + f"\n[output cut: {len(text) - cap} more characters]"
    text += f"\n[{st['calls']} connector call(s), {ms} ms]"
    log_event({"event": "run", "run": run_id, "ok": not is_error, "calls": st["calls"], "ms": ms,
               "code_head": code[:200]})
    return text, is_error


# --------------------------------------------------------------------------- MCP server (stateless, JSON replies)

RUN_DESC = ("Run JavaScript in a sandbox with no network and no files. The code is the body of an async function: "
            "top-level `await` and `return` work. Call the connectors with `await api.<connector>.<tool>(input)`; "
            "each call returns the parsed result (JSON when the tool returned JSON, otherwise text). "
            "Do loops, filtering and joining INSIDE the script and print or return only what you need: "
            "only console.log output and the returned value come back to you. "
            "A failed call throws an Error. Call list_api first to see connectors, tools and input shapes. "
            f"Limits: {LIMITS['timeout_s']}s by default, {LIMITS['max_calls']} calls per script.")
LIST_DESC = ("List the connectors and tools available inside run_code. With no arguments it returns a short index "
             "(connector, tool, one line each). Pass `connectors` or `tools` (\"connector.tool\") to get the full "
             "TypeScript-style input types for just those.")
TOOLS = [
    {"name": "list_api", "description": LIST_DESC, "inputSchema": {"type": "object", "properties": {
        "connectors": {"type": "array", "items": {"type": "string"}, "description": "Connector names to show in full."},
        "tools": {"type": "array", "items": {"type": "string"}, "description": "Specific tools as \"connector.tool\"."}}}},
    {"name": "run_code", "description": RUN_DESC, "inputSchema": {"type": "object", "properties": {
        "code": {"type": "string", "description": "JavaScript (async function body)."},
        "timeout_s": {"type": "integer", "description": f"Optional time limit in seconds (max {LIMITS['max_timeout_s']})."}},
        "required": ["code"]}},
]


def handle_rpc(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None  # notification
    try:
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion") or "2025-03-26", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "code-gateway", "version": VERSION}}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            name, a = params.get("name"), params.get("arguments") or {}
            try:
                if name == "list_api":
                    text, err = list_api(a), False
                elif name == "run_code":
                    text, err = run_code(a)
                else:
                    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
            except BackendError as e:
                text, err = f"Error: {e}", True
            res = {"content": [{"type": "text", "text": text}], "isError": err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


class Handler(BaseHTTPRequestHandler):
    server_version = "code-gateway"

    def log_message(self, *a):
        pass

    def _reply(self, code, obj=None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        h = self.headers.get("Authorization", "")
        try:
            tok = read_secret(TOKEN_FILE)
        except OSError:
            return False
        return h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip().encode(), tok.encode())

    def do_POST(self):
        if not self._authed():
            return self._reply(401, {"error": "unauthorized"})
        if self.path.split("?")[0] != "/mcp":
            return self._reply(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4_000_000:
            return self._reply(413, {"error": "too large"})
        try:
            msg = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._reply(400, {"error": "bad json"})
        if isinstance(msg, list):
            out = [r for r in (handle_rpc(m) for m in msg) if r is not None]
            return self._reply(200, out) if out else self._reply(202)
        r = handle_rpc(msg)
        return self._reply(202) if r is None else self._reply(200, r)

    def do_GET(self):
        self._reply(401 if not self._authed() else 405, {"error": "unauthorized" if not self._authed() else "method not allowed"})

    do_DELETE = do_GET


def main():
    host, _, port = CFG.get("listen", "127.0.0.1:8790").rpartition(":")
    srv = ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    srv.daemon_threads = True
    print(f"code-gateway {VERSION} on {host}:{port}, connectors: {', '.join(BACKENDS)}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
