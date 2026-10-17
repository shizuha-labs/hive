#!/usr/bin/env node
// notify_owner MCP server (HIVE-201 E2 / HIVE-188 M4) — pinned via NOTIFY_OWNER_VERSION
// in claws/provisioner.py. Seeded into a claw's PVC at
// <data>/extensions/notify-owner/server.mjs by the init-gateway-config
// initContainer and registered in openclaw.json under mcp.servers.notify_owner
// (Option A, aoi 2026-06-25 — provision-time install, no image rebuild).
//
// Exposes a single tool, notify_owner(title, body, data?), that the assistant
// calls to push a proactive notification to the claw's owner. It POSTs the Hive
// E1 endpoint  POST {BACKEND_URL}/hive/api/v1/claws/{HIVE_CLAW_ID}/notify/
// with the per-claw service credential  X-Hive-Internal-Token: {HIVE_INTERNAL_AUTH_TOKEN}
// (the claw container already carries that scoped token in env). Hive resolves
// the owner server-side and injects type=claw_channel; the caller never supplies
// the owner (no IDOR). Fail-soft: 429/5xx/network errors return a normal (non-error)
// tool result so the assistant degrades gracefully instead of crashing the turn.
//
// Dependency-free: MCP stdio transport (newline-delimited JSON-RPC 2.0) + global
// fetch (node>=18; the claw image is node:24). No npm install at runtime.

const PROTOCOL_FALLBACK = '2024-11-05';
const SERVER_NAME = 'notify-owner';
const SERVER_VERSION = '1.0.0';

const BACKEND_URL = (process.env.BACKEND_URL || process.env.SHIZUHA_PLATFORM_URL || '').replace(/\/+$/, '');
const CLAW_ID = process.env.HIVE_CLAW_ID || '';
const INTERNAL_TOKEN = process.env.HIVE_INTERNAL_AUTH_TOKEN || '';
const REQUEST_TIMEOUT_MS = Number(process.env.HIVE_NOTIFY_TIMEOUT_MS || '8000');

const TOOL = {
  name: 'notify_owner',
  description:
    "Send a proactive push notification to THIS claw's owner (their phone/app via " +
    'the Hive claw_channel → FCM). Use for assistant-initiated updates, reminders, ' +
    'or finished long-running work the owner asked to be told about. The owner is ' +
    'resolved server-side; you cannot target anyone else. Rate-limited by Hive ' +
    '(assistant-initiated ~5/hr, 20/day) — if rate-limited it fails soft and returns ' +
    'a notice rather than erroring.',
  inputSchema: {
    type: 'object',
    properties: {
      title: { type: 'string', description: 'Short notification title (<=120 chars).' },
      body: { type: 'string', description: 'Notification body text.' },
      data: {
        type: 'object',
        description: 'Optional structured payload merged under the server-injected type=claw_channel envelope.',
        additionalProperties: true,
      },
    },
    required: ['title', 'body'],
  },
};

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

function result(id, payload) {
  send({ jsonrpc: '2.0', id, result: payload });
}

function rpcError(id, code, message) {
  send({ jsonrpc: '2.0', id, error: { code, message } });
}

function textResult(text) {
  // Always a non-error tool result (fail-soft): the assistant reads the text and
  // continues. We never set isError for transient delivery failures.
  return { content: [{ type: 'text', text }] };
}

async function callNotify(args) {
  const title = String(args?.title ?? '').trim();
  const body = String(args?.body ?? '').trim();
  if (!title || !body) {
    return textResult('notify_owner: both `title` and `body` are required; nothing sent.');
  }
  if (!BACKEND_URL || !CLAW_ID || !INTERNAL_TOKEN) {
    return textResult(
      'notify_owner: not configured in this environment (missing BACKEND_URL / HIVE_CLAW_ID / ' +
        'HIVE_INTERNAL_AUTH_TOKEN); notification not sent.',
    );
  }
  const url = `${BACKEND_URL}/hive/api/v1/claws/${encodeURIComponent(CLAW_ID)}/notify/`;
  const payload = { title, body };
  if (args && typeof args.data === 'object' && args.data !== null) payload.data = args.data;

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Hive-Internal-Token': INTERNAL_TOKEN },
      body: JSON.stringify(payload),
      signal: ctl.signal,
    });
    if (resp.status === 202 || resp.status === 200) {
      return textResult('notify_owner: notification queued for delivery to the owner.');
    }
    if (resp.status === 429) {
      return textResult('notify_owner: rate-limited by Hive (too many notifications recently); not sent. Try again later.');
    }
    if (resp.status === 403) {
      return textResult('notify_owner: rejected by Hive (auth/ownership); not sent.');
    }
    if (resp.status === 404) {
      return textResult('notify_owner: claw not found by Hive; not sent.');
    }
    if (resp.status === 400) {
      return textResult('notify_owner: Hive rejected the payload (400); not sent.');
    }
    // 5xx / 502 (FCM upstream) / anything else → fail soft.
    return textResult(`notify_owner: delivery temporarily unavailable (HTTP ${resp.status}); not sent.`);
  } catch (e) {
    // network error / timeout / abort → fail soft.
    return textResult(`notify_owner: could not reach Hive (${e && e.name === 'AbortError' ? 'timeout' : 'network error'}); not sent.`);
  } finally {
    clearTimeout(timer);
  }
}

async function handle(msg) {
  const { id, method, params } = msg;
  // Notifications (no id) — never reply.
  if (id === undefined || id === null) return;

  switch (method) {
    case 'initialize':
      return result(id, {
        protocolVersion: (params && params.protocolVersion) || PROTOCOL_FALLBACK,
        capabilities: { tools: {} },
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
      });
    case 'tools/list':
      return result(id, { tools: [TOOL] });
    case 'tools/call': {
      const name = params && params.name;
      if (name !== 'notify_owner') {
        return rpcError(id, -32602, `Unknown tool: ${name}`);
      }
      const payload = await callNotify((params && params.arguments) || {});
      return result(id, payload);
    }
    case 'ping':
      return result(id, {});
    default:
      return rpcError(id, -32601, `Method not found: ${method}`);
  }
}

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue; // ignore unparseable lines
    }
    Promise.resolve(handle(msg)).catch((e) => {
      if (msg && msg.id != null) rpcError(msg.id, -32603, `Internal error: ${e?.message || e}`);
    });
  }
});
process.stdin.on('end', () => process.exit(0));
