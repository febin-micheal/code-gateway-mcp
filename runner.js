'use strict';
// Runs inside the no-network sandbox. Talks to the gateway over stdin/stdout (one JSON object per line).
const readline = require('readline');
const util = require('util');

let nextId = 1;
const pending = new Map();

function send(obj, cb) { process.stdout.write(JSON.stringify(obj) + '\n', cb); }

const logLine = (...a) => send({ type: 'log', text: util.format(...a) });
console.log = console.info = console.debug = console.warn = console.error = logLine;

function call(connector, tool, args) {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    send({ type: 'call', id, connector, tool, args: args === undefined ? {} : args });
  });
}

function makeApi(names) {
  const api = {};
  const proxy = new Proxy(api, {
    get(t, k) {
      if (typeof k !== 'string' || k === 'then' || k in t) return t[k];
      throw new Error(`unknown connector '${k}'; available: ${Object.keys(t).join(', ')}`);
    },
  });
  for (const name of names) {
    api[name] = new Proxy({}, {
      get(_t, tool) {
        if (typeof tool !== 'string' || tool === 'then') return undefined; // keep `await api.x` from treating it as a promise
        return (input) => call(name, tool, input);
      },
    });
  }
  return proxy;
}

function finish(obj) { send(obj, () => process.exit(0)); }

function fail(e) { finish({ type: 'done', ok: false, error: String((e && e.stack) || e) }); }

process.on('unhandledRejection', fail);
process.on('uncaughtException', fail);

async function run(msg) {
  try {
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const fn = new AsyncFunction('api', 'console', msg.code);
    const v = await fn(makeApi(msg.connectors || []), console);
    let value;
    if (v !== undefined) value = JSON.parse(JSON.stringify(v, (_k, x) => (typeof x === 'bigint' ? x.toString() : x)));
    finish({ type: 'done', ok: true, hasValue: v !== undefined, value });
  } catch (e) { fail(e); }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch (_e) { return; }
  if (msg.type === 'run') run(msg);
  else if (msg.type === 'result') {
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.ok) p.resolve(msg.value); else p.reject(new Error(msg.error || 'call failed'));
  }
});
