import { execFile } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const CLAW_ID = process.env.HIVE_CLAW_ID || '';
const BACKEND_URL = (process.env.BACKEND_URL || process.env.SHIZUHA_PLATFORM_URL || '').replace(/\/+$/, '');
const INTERNAL_TOKEN = process.env.HIVE_INTERNAL_AUTH_TOKEN || '';
const HOME_DIR = process.env.HOME || os.homedir() || '/home/node/.openclaw';
const STATE_PATH = path.join(HOME_DIR, '.shizuha-wa-link-watchdog.json');
const PROBE_INTERVAL_MS = Number(process.env.HIVE_WA_WATCHDOG_INTERVAL_MS || '300000');
const PROBE_INITIAL_DELAY_MS = Number(process.env.HIVE_WA_WATCHDOG_INITIAL_DELAY_MS || '60000');
const NOTIFY_COOLDOWN_MS = Number(process.env.HIVE_WA_WATCHDOG_NOTIFY_COOLDOWN_MS || '1800000');
const FAILURE_STREAK_THRESHOLD = Number(process.env.HIVE_WA_WATCHDOG_FAILURE_STREAK || '2');
const REQUEST_TIMEOUT_MS = Number(process.env.HIVE_NOTIFY_TIMEOUT_MS || '8000');
const DASHBOARD_PATH = CLAW_ID ? `/hive/assistants/${CLAW_ID}/dash` : '/hive/agents';
const WATCHDOG_KEY = '__shizuhaWaLinkWatchdogStarted';

function readState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8')) || {};
  } catch {
    return {};
  }
}

function writeState(state) {
  try {
    fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
    fs.writeFileSync(STATE_PATH, JSON.stringify(state, null, 2) + '\n');
  } catch {
    // Best effort only; cooldown loss should not break the gateway.
  }
}

function stringify(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value || '');
  }
}

function isWhatsAppContext(event) {
  const ctx = event?.context || {};
  const haystack = stringify({ channelId: ctx.channelId, channel: ctx.channel, transport: ctx.transport, metadata: ctx.metadata });
  return /whatsapp|wa\b/i.test(haystack);
}

function isMissingListenerFailure(event) {
  const ctx = event?.context || {};
  return /No active WhatsApp Web listener|not linked|needs_rescan|logged out|disconnected/i.test(stringify(ctx));
}

async function notifyOwner(reason, detail, force = false) {
  if (!BACKEND_URL || !CLAW_ID || !INTERNAL_TOKEN) return;
  const now = Date.now();
  const state = readState();
  const key = `lastNotify:${reason}`;
  if (!force && state[key] && now - Number(state[key]) < NOTIFY_COOLDOWN_MS) return;
  state[key] = now;
  writeState(state);

  const payload = {
    title: 'WhatsApp needs attention',
    body: `Your assistant's WhatsApp link looks disconnected or unable to send (${detail}). Open Hive to re-pair it.`,
    event_type: 'channel_health',
    data: {
      source: 'whatsapp-link-watchdog',
      reason,
      detail: String(detail || '').slice(0, 500),
      dashboard_path: DASHBOARD_PATH,
      deep_link: DASHBOARD_PATH,
    },
  };
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_MS);
  try {
    await fetch(`${BACKEND_URL}/hive/api/v1/claws/${encodeURIComponent(CLAW_ID)}/notify/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Hive-Internal-Token': INTERNAL_TOKEN },
      body: JSON.stringify(payload),
      signal: ctl.signal,
    });
  } catch {
    // Fail-soft: the hook must never break OpenClaw message/gateway handling.
  } finally {
    clearTimeout(timer);
  }
}

function markRecovered() {
  const state = readState();
  if (state.failureStreak || state.lastStatus) {
    state.failureStreak = 0;
    state.lastStatus = 'ok';
    writeState(state);
  }
}

function execOpenClawStatus() {
  return new Promise((resolve) => {
    execFile('openclaw', ['channels', 'status', '--probe', '--json'], { timeout: 30_000 }, (err, stdout, stderr) => {
      resolve({ err, stdout: String(stdout || ''), stderr: String(stderr || '') });
    });
  });
}

function walk(value, visitor) {
  if (Array.isArray(value)) {
    for (const item of value) walk(item, visitor);
    return;
  }
  if (!value || typeof value !== 'object') return;
  visitor(value);
  for (const item of Object.values(value)) walk(item, visitor);
}

function classifyStatus(stdout, stderr, err) {
  const raw = `${stdout}\n${stderr}\n${err?.message || ''}`;
  if (/No active WhatsApp Web listener|not linked|needs_rescan|logged out|disconnected/i.test(raw)) {
    return { ok: false, detail: raw.slice(0, 500) };
  }
  let parsed = null;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    // Non-JSON status output is not proof of failure; only explicit errors above page.
    return err ? { ok: false, detail: raw.slice(0, 500) || 'status probe failed' } : { ok: true, detail: 'status probe completed' };
  }
  let sawWhatsApp = false;
  let bad = '';
  walk(parsed, (obj) => {
    const text = stringify(obj);
    if (!/whatsapp|wa\b/i.test(text)) return;
    sawWhatsApp = true;
    const linked = obj.linked ?? obj.isLinked;
    const connected = obj.connected ?? obj.isConnected;
    const running = obj.running ?? obj.isRunning;
    const status = String(obj.status || obj.state || '').toLowerCase();
    if (linked === false || connected === false || running === false || /unpaired|disconnected|logged.?out|needs.?rescan|error/.test(status)) {
      bad = text.slice(0, 500);
    }
  });
  if (bad) return { ok: false, detail: bad };
  return { ok: true, detail: sawWhatsApp ? 'whatsapp status healthy' : 'no whatsapp status object found' };
}

async function probeOnce() {
  const { err, stdout, stderr } = await execOpenClawStatus();
  const result = classifyStatus(stdout, stderr, err);
  const state = readState();
  if (result.ok) {
    state.failureStreak = 0;
    state.lastStatus = 'ok';
    writeState(state);
    return;
  }
  state.failureStreak = Number(state.failureStreak || 0) + 1;
  state.lastStatus = 'failed';
  state.lastDetail = result.detail;
  writeState(state);
  if (state.failureStreak >= FAILURE_STREAK_THRESHOLD) {
    await notifyOwner('status_probe_failed', result.detail);
  }
}

function startWatchdog() {
  const g = globalThis;
  if (g[WATCHDOG_KEY]) return;
  g[WATCHDOG_KEY] = true;
  const run = () => { void probeOnce(); };
  setTimeout(run, PROBE_INITIAL_DELAY_MS).unref?.();
  setInterval(run, PROBE_INTERVAL_MS).unref?.();
}

export default async function handler(event) {
  try {
    if (event?.type === 'gateway' && event?.action === 'startup') {
      startWatchdog();
      return;
    }
    if (event?.type === 'message' && event?.action === 'sent') {
      const success = event?.context?.success;
      if ((success === false || success === 'false') && (isWhatsAppContext(event) || isMissingListenerFailure(event))) {
        await notifyOwner(
          isMissingListenerFailure(event) ? 'missing_web_listener' : 'send_failed',
          event?.context?.error || event?.context?.lastError || event?.context?.status || 'message:sent reported failure',
          isMissingListenerFailure(event),
        );
      } else if (success === true && isWhatsAppContext(event)) {
        markRecovered();
      }
    }
  } catch (err) {
    console.error('[shizuha-wa-link-watchdog] hook failed (ignored):', err?.message || err);
  }
}
