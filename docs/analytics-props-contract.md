# Analytics props PII contract (HIVE-400)

Pulse task: HIVE-400. Parent analytics work: HIVE-393 / HIVE-381.

`POST /api/analytics/events` accepts client-supplied `props`, but those props are a no-PII, allow-listed metadata channel. Emitters must not send raw user text, names, emails, phone numbers, addresses, IPs, tokens, session IDs, cookies, or stable external identifiers.

## Contract

- `org_id` remains server-authorized from the caller's verified memberships; clients only propose it.
- `user_ref` remains server-derived and pseudonymous per `(user, org)`.
- `props` may contain only scalar values (`str`, `int`, `float`, `bool`, `null`) and only the keys allow-listed for the event type in `analytics.ingest.SAFE_PROP_KEYS_BY_EVENT`.
- Unknown keys are rejected with `400`; new instrumentation must extend the allow-list in code and tests before rollout.
- PII-like key names or PII-like string values are rejected before write; the whole batch stays atomic.

## Current allowed props

| Event type | Allowed props |
| --- | --- |
| `page_view` | `route`, `section`, `entrypoint`, `referrer_kind` |
| `feature_use` | `feature`, `action`, `component`, `variant` |
| `drop_off` | `journey`, `step`, `reason_code`, `component` |
| `error_surface` | `error_code`, `component`, `recoverable`, `severity` |
| `journey_step` | `journey`, `step`, `component`, `variant` |
| `journey_complete` | `journey`, `outcome`, `duration_ms`, `steps` |

## Emitter rule of thumb

Send product-shape metadata, not user content. Good: `{route: "dashboard", section: "hero"}`. Bad: `{email: "user@example.com"}`, `{query: "raw search text"}`, `{session_token: "..."}`.
