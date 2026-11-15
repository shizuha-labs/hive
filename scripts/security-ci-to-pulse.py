#!/usr/bin/env python3
"""Normalize Forgejo CI SAST/SCA output and optionally file Pulse security findings.

Inputs are JSON files emitted by Semgrep, Bandit, OSV-Scanner, and Trivy. The
script is intentionally standalone (stdlib only) so Forgejo workflows can vendor
it into any repo and file/update durable Pulse `security-finding` work instead
of leaving scan output in chat or transient CI logs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SECURITY_CI_SOURCE = "security-ci"
SEV_RANK = {"info": 0, "low": 1, "medium": 2, "moderate": 2, "high": 3, "critical": 4}
PRIORITY = {"info": "low", "low": "low", "medium": "normal", "moderate": "normal", "high": "high", "critical": "urgent"}
# Pulse `Item.SEVERITY_CHOICES` only accepts info/warning/error/critical — our
# internal scanner vocabulary (low/medium/high) is NOT valid there, so POSTing
# `severity: "high"` was rejected with HTTP 400 "high is not a valid choice",
# which broke ALL finding filing (the security-ci gate failure). Map the internal
# rank name to the Pulse enum on the wire; the granular value survives in labels.
PULSE_SEVERITY = {"info": "info", "low": "info", "medium": "warning", "moderate": "warning", "high": "error", "critical": "critical"}
SECURITY_WIKI_SPACE_KEY = "SEC"
SECURITY_WIKI_INDEX_PAGE_ID = "9f584f5b-46a6-4274-bbe3-1e3684e8beb6"
WIKI_PROJECTION_START = "<!-- security-ci:projection:start -->"
WIKI_PROJECTION_END = "<!-- security-ci:projection:end -->"


@dataclass(frozen=True)
class Finding:
    tool: str
    rule: str
    severity: str
    title: str
    path: str
    line: int | None
    detail: str
    cwe: str | None = None
    url: str | None = None
    # PLAT-2893: for dependency-CVE tools (osv/trivy), the vulnerable package +
    # its ecosystem. None for code SAST (semgrep/bandit). Used to collapse the
    # N-advisories-for-one-package flood into a single per-package task.
    package: str | None = None
    ecosystem: str | None = None

    @property
    def source_id(self) -> str:
        raw = "|".join([self.tool, self.rule, self.path, str(self.line or 0), self.title])
        return "security-ci:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class WikiLedgerResult:
    ok: bool
    page_url: str = ""
    page_id: str = ""
    error: str = ""
    action: str = ""


def load_json(path: str | None) -> Any:
    if not path:
        return None
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def norm_sev(value: Any) -> str:
    s = str(value or "medium").strip().lower()
    # Tool-specific aliases.
    if s == "error":
        return "high"
    if s == "warning":
        return "medium"
    if s == "note":
        return "low"
    if s == "moderate":
        return "medium"
    return s if s in SEV_RANK else "medium"


def parse_cvss_severity(severities: Any) -> str:
    """Map OSV severity blocks to a coarse Pulse severity."""
    best = "medium"
    for s in severities or []:
        if not isinstance(s, dict):
            continue
        raw = str(s.get("score") or "")
        # OSV v2 may use a numeric score or a CVSS vector. Prefer numeric when present.
        try:
            score = float(raw)
        except ValueError:
            score = None
        if score is not None:
            if score >= 9.0:
                cand = "critical"
            elif score >= 7.0:
                cand = "high"
            elif score >= 4.0:
                cand = "medium"
            else:
                cand = "low"
        elif "CVSS" in str(s.get("type", "")):
            cand = "high"
        else:
            cand = "medium"
        if SEV_RANK[cand] > SEV_RANK[best]:
            best = cand
    return best


def parse_semgrep(data: Any) -> Iterable[Finding]:
    for r in (data or {}).get("results", []) or []:
        extra = r.get("extra") or {}
        meta = extra.get("metadata") or {}
        sev = norm_sev(extra.get("severity") or meta.get("impact"))
        start = r.get("start") or {}
        refs = meta.get("references") if isinstance(meta.get("references"), list) else []
        yield Finding(
            tool="semgrep",
            rule=str(r.get("check_id") or "semgrep"),
            severity=sev,
            title=str(extra.get("message") or r.get("check_id") or "Semgrep finding"),
            path=str(r.get("path") or ""),
            line=start.get("line"),
            detail=str(extra.get("message") or ""),
            cwe=",".join(meta.get("cwe", [])) if isinstance(meta.get("cwe"), list) else meta.get("cwe"),
            url=meta.get("source") or (refs[0] if refs else None),
        )


def parse_bandit(data: Any) -> Iterable[Finding]:
    for r in (data or {}).get("results", []) or []:
        yield Finding(
            tool="bandit",
            rule=str(r.get("test_id") or r.get("test_name") or "bandit"),
            severity=norm_sev(r.get("issue_severity")),
            title=str(r.get("issue_text") or r.get("test_name") or "Bandit finding"),
            path=str(r.get("filename") or ""),
            line=r.get("line_number"),
            detail=str(r.get("issue_text") or ""),
            cwe=str((r.get("issue_cwe") or {}).get("id") or "") or None,
            url=(r.get("issue_cwe") or {}).get("link"),
        )


def parse_osv(data: Any) -> Iterable[Finding]:
    def vulns_from_package(pkg: dict[str, Any]):
        for v in pkg.get("vulnerabilities", []) or []:
            yield pkg, v

    for result in (data or {}).get("results", []) or []:
        source = result.get("source") or {}
        src_path = source.get("path") or source.get("name") or "dependency-lock"
        for pkg in result.get("packages", []) or []:
            for pkg, v in vulns_from_package(pkg):
                pkg_info = pkg.get("package") or {}
                vuln_id = v.get("id") or "OSV"
                name = pkg_info.get("name") or "dependency"
                yield Finding(
                    tool="osv",
                    rule=str(vuln_id),
                    severity=norm_sev(parse_cvss_severity(v.get("severity"))),
                    title=f"{name}: {vuln_id}",
                    path=str(src_path),
                    line=None,
                    detail=str(v.get("summary") or v.get("details") or "OSV dependency vulnerability"),
                    url=(v.get("references") or [{}])[0].get("url") if isinstance(v.get("references"), list) and v.get("references") else None,
                    package=str(name),
                    ecosystem=str(pkg_info.get("ecosystem") or "").strip() or None,
                )


def parse_trivy(data: Any) -> Iterable[Finding]:
    for result in (data or {}).get("Results", []) or []:
        target = result.get("Target") or "filesystem"
        for v in result.get("Vulnerabilities", []) or []:
            vid = v.get("VulnerabilityID") or "TRIVY"
            pkg = v.get("PkgName") or "package"
            yield Finding(
                tool="trivy",
                rule=str(vid),
                severity=norm_sev(v.get("Severity")),
                title=f"{pkg}: {vid}",
                path=str(target),
                line=None,
                detail=str(v.get("Title") or v.get("Description") or "Trivy vulnerability"),
                url=v.get("PrimaryURL"),
                package=str(pkg),
                ecosystem=str(result.get("Class") or result.get("Type") or "").strip() or None,
            )


def load_allowlist(path: str | None) -> list[dict[str, Any]]:
    data = load_json(path) if path else None
    if not data:
        return []
    if isinstance(data, dict):
        return list(data.get("allowlist") or data.get("suppressions") or [])
    return list(data) if isinstance(data, list) else []


# PLAT-4772 / Hiro contract: suppressions are fail-closed. A row may suppress a
# finding only when it carries an exact identity, a *suppressing* disposition,
# a rationale, typed Security approval provenance, and a non-expired expiry.
# Unknown/malformed/expired/unauthorized rows never match and are reported
# loudly. PLAT-4772: bare tool+rule is class-wide and rejected; deferred/open
# are ledger states, not suppressions; Security identity is exact-match only.
#
# Suppressing dispositions (machine allowlist). Ledger may still track
# deferred/open findings, but those must NOT hide active scan results.
VALID_SUPPRESSION_DISPOSITIONS = frozenset({
    "accepted-false-positive",
    "false-positive",
    "remediated",
    "risk-accepted",
    "wont-fix",
})
# Canonical typed Security identities (exact token or email local-part).
# Substring / regex matching is forbidden — "not-security" must never pass.
_CANONICAL_SECURITY_IDENTITIES = frozenset({
    "security",
    "sec",
    "security-lead",
    "sec-lead",
    "security-team",
})


def _norm_disposition(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _is_security_approver(value: Any) -> bool:
    """True only for canonical typed Security identities.

    Accepts exact tokens (``security``, ``security-lead``, …) or emails whose
    local-part (plus-tag stripped) is exactly one of those tokens. Rejects
    substring spoofs such as ``not-security`` / ``unsecurity@…``.
    """
    text = str(value or "").strip().lower()
    if not text:
        return False
    if text in _CANONICAL_SECURITY_IDENTITIES:
        return True
    if "@" not in text:
        return False
    local, _, domain = text.partition("@")
    local = local.strip()
    domain = domain.strip()
    if not local or not domain:
        return False
    if "+" in local:
        local = local.split("+", 1)[0]
    return local in _CANONICAL_SECURITY_IDENTITIES


def _parse_expiry(value: Any) -> datetime | None:
    """Parse an allowlist expiry. Returns None when missing/unparseable."""
    raw = str(value or "").strip()
    if not raw:
        return None
    # Date-only → end of that UTC day (inclusive).
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return day.replace(hour=23, minute=59, second=59)
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_allowlist_entry(entry: Any, *, now: datetime | None = None) -> str | None:
    """Return a rejection reason, or None when the entry is structurally valid.

    Valid entries still have to *match* a finding before they suppress anything.
    This gate only decides whether the row is eligible to suppress at all.

    Exact identity (PLAT-4772 / Hiro Architecture): require a stable
    ``source_id`` **or** a demonstrably path-scoped fingerprint
    (``tool`` + ``rule`` + non-empty ``path_contains``). Bare ``tool``+``rule``
    is class-wide and must never suppress.
    """
    if not isinstance(entry, dict):
        return "entry is not an object"
    source_id = str(entry.get("source_id") or "").strip()
    tool = str(entry.get("tool") or "").strip()
    rule = str(entry.get("rule") or "").strip()
    path_contains = str(entry.get("path_contains") or "").strip()
    # Exact fingerprint: source_id alone, OR tool+rule+path_contains together.
    # path_contains alone / bare tool+rule / empty {} never suppress.
    if source_id:
        pass  # stable source_id is the preferred exact key
    elif tool and rule and path_contains:
        pass  # path-scoped fingerprint (not class-wide)
    elif tool and rule:
        return (
            "missing exact identity (bare tool+rule is class-wide; "
            "require source_id or tool+rule+path_contains)"
        )
    else:
        return (
            "missing exact identity "
            "(require source_id or tool+rule+path_contains)"
        )
    reason = str(entry.get("reason") or entry.get("rationale") or "").strip()
    if not reason:
        return "missing reason/rationale"
    owner = str(entry.get("owner") or "").strip()
    if not owner:
        return "missing owner"
    disposition = _norm_disposition(entry.get("disposition"))
    if not disposition:
        return "missing disposition"
    if disposition not in VALID_SUPPRESSION_DISPOSITIONS:
        # deferred/open are ledger states — they must not hide active findings.
        return f"invalid disposition {disposition!r} (not a suppressing disposition)"
    approved_by = entry.get("approved_by") or entry.get("security_approver") or entry.get("approver")
    if not (_is_security_approver(approved_by) or _is_security_approver(owner)):
        return "missing Security approval provenance (owner/approved_by must be Security)"
    expiry_raw = entry.get("expires") or entry.get("expires_at") or entry.get("expiry")
    if expiry_raw is None or str(expiry_raw).strip() == "":
        return "missing expiry"
    expiry = _parse_expiry(expiry_raw)
    if expiry is None:
        return f"unparseable expiry {expiry_raw!r}"
    current = now or datetime.now(timezone.utc)
    if current > expiry:
        return f"expired at {expiry.isoformat()}"
    return None


def allowlisted(
    f: Finding,
    entries: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    reject_sink: list[str] | None = None,
) -> str | None:
    """Return a suppression reason when a *valid* entry matches ``f``.

    Malformed / expired / non-Security-approved rows never suppress. Each
    rejection is printed to stderr and optionally appended to ``reject_sink``.
    """
    for idx, e in enumerate(entries):
        rejection = validate_allowlist_entry(e, now=now)
        if rejection is not None:
            msg = f"allowlist entry[{idx}] rejected (fail-closed, no suppression): {rejection}"
            print(f"ERROR: {msg}", file=sys.stderr)
            if reject_sink is not None:
                reject_sink.append(msg)
            continue
        # Fail-closed fence (PLAT-6754 B101 disposition): a non-dict entry is
        # malformed input, not a fence violation — skip it the same way the
        # rejected-allowlist neighbors do. (A bare assert vanishes under -O and
        # crashes the relay otherwise.)
        if not isinstance(e, dict):
            continue
        if e.get("source_id") and str(e.get("source_id")) != f.source_id:
            continue
        if e.get("tool") and str(e.get("tool")) != f.tool:
            continue
        if e.get("rule") and str(e.get("rule")) != f.rule:
            continue
        if e.get("path_contains") and str(e.get("path_contains")) not in f.path:
            continue
        reason = str(e.get("reason") or e.get("rationale") or "").strip()
        owner = str(e.get("owner") or "").strip()
        disposition = _norm_disposition(e.get("disposition"))
        return f"{reason} (owner: {owner}; disposition: {disposition})"
    return None


def normalize_api_base(raw: str) -> str:
    base = (raw or "").rstrip("/")
    if not base:
        return ""
    if base.endswith("/api") or base.endswith("/pulse/api") or base.endswith("/shizuha-pulse/api"):
        return base
    if base.endswith("/pulse") or base.endswith("/shizuha-pulse"):
        return f"{base}/api"
    return f"{base}/api"


def pulse_request(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    # PLAT-2004 (bandit B310): restrict to http(s) so a misconfigured PULSE_API_URL
    # can't turn this into a file:// / custom-scheme open. Standalone CI script — no
    # Django import path — so the guard is inline rather than tasks.net_utils.
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(f"refusing non-HTTP(S) Pulse API URL scheme {scheme!r}")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    # PLAT-4120: retry transient transport failures (network error / timeout /
    # HTTP 5xx / 429) with backoff, so a momentary Pulse-API hiccup does not red the
    # whole (non-blocking) security-ci run and false-page the lead via ORIG-11. A 4xx
    # other than 429 is a genuine contract error -> raised immediately, never retried.
    # This wraps only the transport; CRITICAL-finding blocking is unaffected.
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - scheme validated above
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # Surface the server's reason — a bare "HTTP Error 400" hides which field
            # the Pulse API rejected, making contract drift undebuggable from CI logs.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
            except Exception:
                pass
            if (exc.code == 429 or exc.code >= 500) and attempt < attempts:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            # Network-level failure (DNS / connection refused / timeout) — transient.
            if attempt < attempts:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{method} {url} -> {exc.reason}") from exc


def normalize_wiki_api_base(raw: str) -> str:
    base = (raw or "").rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/api") else f"{base}/api"


def wiki_request(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None = None,
    *,
    organization_id: int = 1,
) -> Any:
    """Call Wiki with the same fail-readable transport contract as Pulse."""
    data = json.dumps(body).encode() if body is not None else None
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(f"refusing non-HTTP(S) Wiki API URL scheme {scheme!r}")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Organization-ID": str(organization_id),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - scheme validated above
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail or exc.reason}") from exc


def _wiki_content_json(content: str) -> dict[str, Any]:
    """Build a readable TipTap document without adding non-stdlib dependencies."""
    nodes: list[dict[str, Any]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            nodes.append({"type": "heading", "attrs": {"level": 1},
                          "content": [{"type": "text", "text": stripped[2:]}]})
        elif stripped.startswith("## "):
            nodes.append({"type": "heading", "attrs": {"level": 2},
                          "content": [{"type": "text", "text": stripped[3:]}]})
        else:
            node: dict[str, Any] = {"type": "paragraph"}
            if line:
                node["content"] = [{"type": "text", "text": line}]
            nodes.append(node)
    return {"type": "doc", "content": nodes}


def _wiki_projection(findings: list[Finding], args: argparse.Namespace) -> str:
    def cell(value: Any, limit: int = 160) -> str:
        return str(value or "").replace("|", "/").replace("\n", " ")[:limit]

    lines = [
        WIKI_PROJECTION_START,
        "## Current scan projection (managed by security-ci)",
        "",
        f"Repository: `{args.repo}`  ",
        f"Scan: `{args.ref}` / `{args.sha or 'N/A'}`  ",
        f"Run: {args.run_url or 'N/A'}  ",
        f"Active findings: **{len(findings)}**",
        "",
        "This section is derived from the scanner. The checked-in ",
        "`.security-ci-allowlist.json` is the machine-consumed suppression authority.",
        "",
        "| Severity | Tool | Rule | Location | Source ID |",
        "|---|---|---|---|---|",
    ]
    # The Wiki projection is the durable disposition surface, so it must remain
    # lossless even during a large scanner flood. Let the Wiki write fail loud
    # (and surface durable-write-failed in Pulse) if its API cannot accept the
    # complete page rather than silently publishing only a prefix.
    for finding in sorted(findings, key=lambda f: -SEV_RANK.get(f.severity, 0)):
        location = cell((finding.path or "") + (f":{finding.line}" if finding.line else ""), 240)
        lines.append(
            f"| {cell(finding.severity)} | `{cell(finding.tool)}` | `{cell(finding.rule)}` | "
            f"`{location}` | `{cell(finding.source_id)}` |"
        )
    if not findings:
        lines.append("| — | — | — | No active findings at filing threshold | — |")
    lines.append(WIKI_PROJECTION_END)
    return "\n".join(lines)


def _replace_wiki_projection(existing: str, projection: str, repo: str) -> str:
    if (WIKI_PROJECTION_START in existing) != (WIKI_PROJECTION_END in existing):
        raise RuntimeError("Wiki projection markers are incomplete; refusing a destructive rewrite")
    if WIKI_PROJECTION_START in existing and WIKI_PROJECTION_END in existing:
        before, tail = existing.split(WIKI_PROJECTION_START, 1)
        _, after = tail.split(WIKI_PROJECTION_END, 1)
        return before.rstrip() + "\n\n" + projection + after
    if existing.strip():
        return existing.rstrip() + "\n\n" + projection
    return f"# Findings Ledger — `{repo}`\n\n" + projection


def wiki_upsert_ledger(
    api_base: str,
    token: str,
    findings: list[Finding],
    args: argparse.Namespace,
) -> WikiLedgerResult:
    """Upsert the derived per-repo projection before Pulse may point at it."""
    if not api_base or not token:
        return WikiLedgerResult(ok=False, error="Wiki API URL/token not configured", action="config-missing")

    organization_id = int(getattr(args, "wiki_organization_id", 1) or 1)
    index_page_id = str(getattr(args, "wiki_index_page_id", "") or SECURITY_WIKI_INDEX_PAGE_ID)
    try:
        space = wiki_request(
            "GET", f"{api_base}/spaces/{SECURITY_WIKI_SPACE_KEY}/", token,
            organization_id=organization_id,
        )
        space_id = str(space.get("id") or "")
        if not space_id:
            raise RuntimeError("Security space response did not contain an id")

        repo_slug = args.repo.split("/")[-1]
        title = f"Findings Ledger — {repo_slug}"
        query = urllib.parse.urlencode({
            "space": space_id,
            "search": title,
            "page_size": 100,
        })
        listed = wiki_request(
            "GET", f"{api_base}/pages/?{query}", token,
            organization_id=organization_id,
        )
        rows = listed.get("results", []) if isinstance(listed, dict) else listed
        exact_matches = [row for row in rows or [] if row.get("title") == title]
        if len(exact_matches) > 1:
            raise RuntimeError(f"Wiki contains multiple exact pages titled {title!r}")
        match = exact_matches[0] if exact_matches else None
        projection = _wiki_projection(findings, args)

        if match:
            page_id = str(match.get("id") or "")
            if not page_id:
                raise RuntimeError("Wiki page search match did not contain an id")
            detail = wiki_request(
                "GET", f"{api_base}/pages/{page_id}/", token,
                organization_id=organization_id,
            )
            content = _replace_wiki_projection(str(detail.get("content_text") or ""), projection, args.repo)
            if content != str(detail.get("content_text") or ""):
                wiki_request(
                    "PATCH", f"{api_base}/pages/{page_id}/", token,
                    {
                        "content_text": content,
                        "content_json": _wiki_content_json(content),
                        "expected_version": int(detail.get("version") or 1),
                        "status": "published",
                    },
                    organization_id=organization_id,
                )
                action = "updated"
            else:
                action = "unchanged"
        else:
            content = _replace_wiki_projection("", projection, args.repo)
            created = wiki_request(
                "POST", f"{api_base}/pages/", token,
                {
                    "space": space_id,
                    "parent": index_page_id,
                    "title": title,
                    "content_text": content,
                    "content_json": _wiki_content_json(content),
                    "status": "published",
                },
                organization_id=organization_id,
            )
            page_id = str(created.get("id") or "")
            if not page_id:
                raise RuntimeError("Wiki page create response did not contain an id")
            action = "created"
        return WikiLedgerResult(
            ok=True,
            page_url=f"https://wiki.shizuha.com/{page_id}",
            page_id=page_id,
            action=action,
        )
    except Exception as exc:
        return WikiLedgerResult(ok=False, error=str(exc)[:500], action="write-failed")


def pulse_find_existing_all(api_base: str, token: str, source_id: str) -> list[dict[str, Any]]:
    """Return EVERY live Pulse item sharing this stable ``source_id`` (PLAT-8903).

    Same server-side constraint as :func:`pulse_find_existing` (index-backed
    exact match on ``(source, source_id)``, ``include_archived=true`` so a
    repeated run does not refile a duplicate of an archived row), but returns
    the full match list instead of silently electing one row. The ledger
    upsert needs the full set to detect the two-live-rollups ambiguity instead
    of silently writing one generation (PLAT-8903: the oldest-first election
    upserted PLAT-4699 — created 2026-07-16 — over the wiki-documented current
    rollup PLAT-6986 and wrote a fix-branch's stale 12-row posture into it).
    """
    q = urllib.parse.urlencode({
        "source": SECURITY_CI_SOURCE,
        "source_id": source_id,
        # Also match auto-archived done-category copies — otherwise a repeated
        # run would refile a duplicate of an archived finding.
        "include_archived": "true",
    })
    data = pulse_request("GET", f"{api_base}/items/?{q}", token)
    rows = data.get("results") if isinstance(data, dict) else data
    matched = []
    for row in rows or []:
        # When the payload exposes source_id, require an exact match; when it does
        # not (the list serializer), the server-side source_id constraint already
        # guarantees the row is this identity — trust it.
        rid = row.get("source_id")
        if rid in (None, source_id) and (row.get("item_key") or row.get("id")):
            matched.append(row)
    if matched:
        matched.sort(key=lambda row: str(row.get("created_at") or row.get("id") or ""))
        return matched
    # Fallback for Pulse builds that ignore the source_id query param.
    q2 = urllib.parse.urlencode({
        "source": SECURITY_CI_SOURCE,
        "search": source_id,
        "include_archived": "true",
    })
    data = pulse_request("GET", f"{api_base}/items/?{q2}", token)
    rows = data.get("results") if isinstance(data, dict) else data
    fallback = []
    for row in rows or []:
        rid = row.get("source_id")
        if rid in (None, source_id) and (row.get("item_key") or row.get("id")):
            fallback.append(row)
    fallback.sort(key=lambda row: str(row.get("created_at") or row.get("id") or ""))
    return fallback


def pulse_find_existing(api_base: str, token: str, source_id: str) -> dict[str, Any] | None:
    """Return the existing Pulse item for this finding's stable ``source_id``, if any.

    PLAT-2688: constrain the query server-side with ``source_id=`` (index-backed
    exact match on ``(source, source_id)``). ItemListSerializer does not expose
    ``source_id``/``description``, so the match must not depend on per-row fields.

    Do NOT AND ``search=`` with ``source_id=``. Ledger titles are
    ``[security-ci] Findings ledger — <repo>`` and the description does not
    contain the token ``security-ci:<repo>:ledger``. AND-combining those filters
    returned zero rows on every weekly scan and minted a new ledger (live
    2026-08-29: six open cortex ledgers sharing one source_id). Look up by
    source_id first; fall back to search only when that filter returns nothing
    (old Pulse builds without the PLAT-2688 query param).

    PLAT-8903 root cause (documented): this single-row election sorted matches
    by ``created_at`` ASCENDING and returned the OLDEST row — for the drive
    ledger that elected the stale generation PLAT-4699 (2026-07-16) over the
    wiki-documented current rollup PLAT-6986, and a fix-branch scan then wrote
    its stale posture into it. The ledger path no longer uses this election:
    :func:`pulse_upsert_ledger` consumes :func:`pulse_find_existing_all` and
    fails loud on multi-generation ambiguity. The per-finding path keeps the
    historical first-row behavior (unchanged scope).
    """
    return (pulse_find_existing_all(api_base, token, source_id) or [None])[0]


def pulse_comment(api_base: str, token: str, item_ref: str, content: str) -> None:
    pulse_request("POST", f"{api_base}/comments/", token, {"item": item_ref, "content": content})


def finding_comment(finding: Finding, args: argparse.Namespace) -> str:
    location = f"{finding.path}:{finding.line}" if finding.line else finding.path
    return textwrap.dedent(f"""
    Security CI observed this finding again.

    - Tool: `{finding.tool}`
    - Rule/Vulnerability: `{finding.rule}`
    - Severity: `{finding.severity}`
    - Repository: `{args.repo}`
    - Ref/SHA: `{args.ref}` / `{args.sha}`
    - Location: `{location}`
    - Source ID: `{finding.source_id}`
    - Run: {args.run_url or 'N/A'}
    - Reference: {finding.url or 'N/A'}
    """).strip()


def origin_observation(args: argparse.Namespace, *, check: str = "security-ci") -> dict[str, str]:
    """Build the standalone CI writer's immutable Pulse provenance payload."""
    values = {
        "provider": "origin-forgejo",
        "repo": str(args.repo or "").strip(),
        "ref": str(args.ref or "").strip(),
        "workflow": os.environ.get("GITHUB_WORKFLOW") or "security-ci.yml",
        "check": check,
        "run_id": os.environ.get("GITHUB_RUN_ID") or "",
        "run_number": os.environ.get("GITHUB_RUN_NUMBER") or "",
        "run_url": str(args.run_url or "").strip(),
    }
    sha = str(args.sha or "").strip().lower()
    if 7 <= len(sha) <= 64 and all(c in "0123456789abcdef" for c in sha):
        values["commit_sha"] = sha
    return {key: value for key, value in values.items() if value}


def pulse_create_or_update_finding(api_base: str, token: str, finding: Finding, args: argparse.Namespace) -> str:
    existing = pulse_find_existing(api_base, token, finding.source_id)
    if existing:
        ref = str(existing.get("item_key") or existing.get("id"))
        # If the finding was already terminally dispositioned (remediated,
        # rejected as a false positive, duplicate, etc.) leave it alone — a
        # closed disposition is durable and re-commenting on every CI run is just
        # noise. ItemListSerializer exposes status_category, so this needs no
        # extra fetch. Non-terminal matches get a lightweight "observed again"
        # comment instead of a duplicate task.
        terminal_statuses = {"done", "closed", "completed", "cancelled", "canceled", "deferred", "rejected", "duplicate", "wont_fix", "failed", "expired"}
        if existing.get("status_category") == "done" or str(existing.get("status") or "").strip() in terminal_statuses:
            return f"skipped-terminal:{ref}"
        # HIVE-694 (operator 2026-07-12): do NOT re-comment "observed again" on
        # every CI run. With ~20 open findings per repo and CI firing per push,
        # this flooded the Pulse activity feed and the Home dashboard live
        # theater (the "source flood anomaly: security-ci" the board itself
        # flagged). An OPEN finding already means "still present" — a
        # per-run breadcrumb adds nothing. Only comment when the finding
        # MATERIALLY changed (severity shifted); the security workflow's
        # Verify stage re-runs the tooling anyway.
        prev_severity = str(existing.get("severity") or "").strip().lower()
        new_severity = str(PULSE_SEVERITY.get(finding.severity, "warning")).strip().lower()
        if prev_severity and prev_severity != new_severity:
            pulse_comment(api_base, token, ref,
                          f"Severity changed: `{prev_severity}` → `{new_severity}`.\n\n"
                          + finding_comment(finding, args))
            return f"updated:{ref}"
        return f"skipped-unchanged:{ref}"
    location = f"{finding.path}:{finding.line}" if finding.line else finding.path
    body = {
        "mode": "task",
        "title": f"[security-ci][{finding.tool}] {finding.title}"[:240],
        "description": textwrap.dedent(f"""
        Automated security finding from Forgejo CI.

        - Tool: `{finding.tool}`
        - Rule/Vulnerability: `{finding.rule}`
        - Severity: `{finding.severity}`
        - Repository: `{args.repo}`
        - Ref/SHA: `{args.ref}` / `{args.sha}`
        - Location: `{location}`
        - Source ID: `{finding.source_id}`
        - Run: {args.run_url or 'N/A'}
        - Reference: {finding.url or 'N/A'}
        - CWE: `{finding.cwe or 'N/A'}`

        Detail:
        {finding.detail[:4000]}

        Triage: confirm exploitability. Suppress only with an explicit allowlist entry
        (`.security-ci-allowlist.json`) that includes owner, reason, and the stable
        `source_id`/rule/path match. Close via the `security-finding` workflow after
        remediation or accepted false-positive disposition.
        """).strip(),
        "priority": PRIORITY.get(finding.severity, "normal"),
        "severity": PULSE_SEVERITY.get(finding.severity, "warning"),
        "workflow_name": "security-finding",
        "assignment_group": "security",
        "source": SECURITY_CI_SOURCE,
        "source_id": finding.source_id,
        "source_url": args.run_url or finding.url,
        "metadata": {"origin_observations": [
            origin_observation(args, check=f"security-ci:{finding.tool}")
        ]},
        # Pulse `/items/` `labels` is a list of strings, NOT a mapping — a dict here
        # is rejected with HTTP 400 (the security-ci filing failure). Encode the
        # metadata as flat `key:value` label strings.
        "labels": ["security-ci", f"tool:{finding.tool}", f"repo:{args.repo}", f"severity:{finding.severity}"],
        # 2026-07-03 flood post-mortem: the items API auto-assigned the CREATING
        # token's identity (codex@ service user) despite assignment_group being
        # set — 646 findings piled onto one agent's queue in a day. Findings are
        # TEAM work: explicitly null the assignee and repair post-create if the
        # server still stamps one.
        "assignee_id": None,
    }
    if args.project_id:
        body["project"] = int(args.project_id)
    created = pulse_request("POST", f"{api_base}/items/", token, body)
    ref = created.get("item_key") or created.get("id")
    try:
        if created.get("assignee") or created.get("assignee_id"):
            pulse_request("PATCH", f"{api_base}/items/{created.get('id')}/", token, {"assignee_id": None})
    except Exception as exc:
        print(f"WARN: could not clear auto-assigned assignee on {ref}: {exc}", file=sys.stderr)
    return f"created:{ref}"


def pulse_upsert_ledger(
    api_base: str,
    token: str,
    findings: list[Finding],
    args: argparse.Namespace,
    wiki_ledger: WikiLedgerResult | None = None,
) -> str:
    """HIVE-694 (operator 2026-07-12): ONE rolling ledger item per repo instead
    of a Pulse task per finding.

    Per-finding filing put 1,500+ security-finding tasks on the board; the
    operator's directive is wiki-for-bulk, Pulse-for-pointers. The ledger item
    holds the CURRENT findings table (top 50) + a pointer to the repo's wiki
    ledger page, is updated IN PLACE only when the finding set changes
    (content hash kept in a label), and never accumulates comments. The
    security team triages from the ledger; individual remediation work items
    are created by humans/agents only for findings they actually pick up.
    Set SECURITY_CI_PER_FINDING=1 to restore the legacy per-finding filing.
    """
    import hashlib
    # PLAT-8903 (branch posture): a fix-branch scan is BEHIND main — its
    # finding set is not the repo's stable posture. Writing it into the stable
    # per-repo rollup advertised stale posture (live: run 5830 on
    # `fix/security-ci-origin-checkout` wrote a 12-row stale set into the
    # drive rollup). Branch telemetry stays branch-scoped: the CI run summary
    # (write_summary) carries it; the stable rollup is only ever written by
    # scans of the repo's default branch.
    # PLAT-8903 follow-up: GITHUB_REF arrives fully qualified on scheduled and
    # push events ("refs/heads/master") and the workflow does not pass --ref,
    # so args.ref falls back to that env default. Compare the SHORT branch
    # name: the unqualified comparison classified every scheduled run as a
    # branch scan and silently skipped the stable rollup forever (the ledger
    # went stale with no error). Unknown/empty refs still fail closed to the
    # branch-skip path.
    normalized_ref = (
        str(getattr(args, "ref", "") or "")
        .strip()
        .lower()
        .rstrip("/")
        .removeprefix("refs/heads/")
        .removeprefix("refs/")
    )
    if normalized_ref not in ("main", "master"):
        print(
            f"ledger: ref {args.ref!r} is not the default branch — stable rollup "
            f"for {args.repo} NOT updated (branch posture stays branch-scoped, PLAT-8903)",
            file=sys.stderr,
        )
        return f"ledger-skipped-branch:{args.ref}"
    source_id = f"security-ci:{args.repo}:ledger"
    wiki_ledger = wiki_ledger or WikiLedgerResult(
        ok=False, error="Wiki ledger upsert was not attempted", action="not-attempted"
    )
    durable_state = "ok" if wiki_ledger.ok else "failed"
    digest = hashlib.sha256(
        ("\n".join(sorted(f"{f.source_id}|{f.severity}" for f in findings))
         + f"\nwiki:{durable_state}:{wiki_ledger.page_id}").encode()
    ).hexdigest()[:12]
    hash_label = f"ledger-hash:{digest}"
    sev_counts: dict[str, int] = {}
    for f in findings:
        sev_counts[f.severity] = sev_counts.get(f.severity, 0) + 1
    counts_md = ", ".join(
        f"{k}: {v}" for k, v in sorted(sev_counts.items(), key=lambda kv: -SEV_RANK.get(kv[0], 0))
    ) or "none"
    top = sorted(findings, key=lambda f: -SEV_RANK.get(f.severity, 0))[:50]
    rows = "\n".join(
        "| {sev} | `{tool}` | `{rule}` | `{loc}` | `{sid}` |".format(
            sev=f.severity, tool=f.tool, rule=(f.rule or "")[:60].replace("|", "/"),
            loc=((f.path or "") + (f":{f.line}" if f.line else ""))[:80].replace("|", "/"),
            sid=(f.source_id or "")[:48],
        ) for f in top
    )
    wiki_slug = args.repo.split("/")[-1]
    if wiki_ledger.ok:
        durable_line = (
            f"- Full ledger, history and remediation notes: "
            f"[Findings Ledger — {wiki_slug}]({wiki_ledger.page_url})"
        )
    else:
        failure_detail = " ".join(str(wiki_ledger.error or "unknown error").split())
        failure_detail = failure_detail.replace("`", "'")[:240]
        durable_line = (
            "- Durable ledger projection: **FAILED** (`durable-write-failed`). "
            f"No wiki pointer was emitted; Security owns recovery. "
            f"Action: `{wiki_ledger.action or 'unknown'}`; error: `{failure_detail}`."
        )
    description = "\n".join([
        f"Rolling security-findings ledger for `{args.repo}` (weekly scheduled scan; HIVE-694 rollup mode).",
        "",
        f"- Open findings: **{len(findings)}** ({counts_md})",
        f"- Last scan: `{args.ref}` / `{args.sha or 'N/A'}` — {args.run_url or 'N/A'}",
        durable_line,
        f"- Ledger hash: `{digest}`",
        "",
        "This item is UPDATED IN PLACE by security-ci; do not file per-finding tasks from it wholesale.",
        "Pick up a finding → create a scoped remediation task (or batch-PR task per repo for dep advisories),",
        "record the disposition in the checked-in `.security-ci-allowlist.json` via Security-reviewed PR;",
        "the wiki page is a derived human-readable projection, never the suppression authority.",
        "",
        "| Severity | Tool | Rule | Location | Source ID |",
        "|---|---|---|---|---|",
        rows or "| — | — | — | (no findings at/above filing threshold) | — |",
    ])
    max_sev = max((f.severity for f in findings), key=lambda s: SEV_RANK.get(s, 0), default="low")
    matches = pulse_find_existing_all(api_base, token, source_id)
    # PLAT-8903 (one stable rollup per repo): with multiple items sharing the
    # ledger source_id, NO silent election is contract-safe — created_at
    # ordering elected the stale generation (PLAT-4699 over PLAT-6986), and
    # open-status ordering would have done the same here (the stale generation
    # is the OPEN one; the wiki-current rollup is completed). The signals
    # conflict, so ambiguity fails LOUD: no write, non-zero exit via
    # RuntimeError — the workflow retry/notifier owns recovery and Security
    # reconciles the pair (cancel the stale generation). Only an all-done-
    # category match set (pure archived history) is unambiguous: keep updating
    # the most recently created generation.
    if len(matches) > 1:
        done_rows = [m for m in matches if (m.get("status_category") or "") == "done"]
        if len(done_rows) != len(matches):
            detail = "; ".join(
                f"{m.get('item_key') or m.get('id')} (status={m.get('status')}, "
                f"category={m.get('status_category')}, created={m.get('created_at')})"
                for m in matches
            )
            raise RuntimeError(
                f"security-ci ledger ambiguity for {args.repo}: {len(matches)} live items "
                f"share source_id `{source_id}` — the one-stable-per-repo-rollup contract "
                f"(HIVE-694 / PLAT-4772) is violated and no silent election is safe "
                f"(PLAT-8903). NO posture was written. Reconcile: cancel/park the "
                f"stale-generation item(s) so exactly one rollup remains, then re-run. "
                f"Matches: {detail}"
            )
        # All-done match set = pure archived history: unambiguous. Keep updating
        # the MOST RECENTLY created generation (matches is created_at-ascending).
        existing = matches[-1]
    else:
        existing = matches[0] if matches else None
    if existing:
        ref = str(existing.get("item_key") or existing.get("id"))
        labels = [l for l in (existing.get("labels") or []) if isinstance(l, str)]
        if hash_label in labels:
            return f"ledger-unchanged:{ref}"
        new_labels = [
            l for l in labels
            if not l.startswith("ledger-hash:") and l != "durable-write-failed"
        ] + [hash_label]
        if not wiki_ledger.ok:
            new_labels.append("durable-write-failed")
        try:
            pulse_request("PATCH", f"{api_base}/items/{existing.get('id') or ref}/", token,
                          {"description": description, "labels": new_labels,
                           "priority": PRIORITY.get(max_sev, "normal"),
                           "append_origin_observations": [origin_observation(args)]})
            return f"ledger-updated:{ref}"
        except Exception as exc:
            # PATCH rejected. When Wiki delivery already failed, a bare refresh
            # comment would drop the durable-write-failed signal and leave the
            # prior dead pointer/description in place while main() exits 0 —
            # unbounded, uninformative telemetry (PLAT-4772 P1). Persist an
            # explicit failure comment when possible, then always propagate so
            # main() returns 2 and the workflow retry/notifier owns recovery.
            print(f"WARN: ledger PATCH failed for {ref}: {exc}", file=sys.stderr)
            if not wiki_ledger.ok:
                failure_detail = " ".join(str(wiki_ledger.error or "unknown error").split())
                failure_detail = failure_detail.replace("`", "'")[:240]
                comment = (
                    f"durable-write-failed: Wiki ledger delivery failed "
                    f"(action=`{wiki_ledger.action or 'unknown'}`; error=`{failure_detail}`); "
                    f"Pulse PATCH also failed (`{exc}`); "
                    f"{len(findings)} open finding(s) ({counts_md}); hash `{digest}`. "
                    f"run: {args.run_url or 'N/A'}"
                )
                try:
                    pulse_comment(api_base, token, ref, comment)
                except Exception as comment_exc:
                    print(
                        f"ERROR: could not post durable-write-failed comment for {ref}: "
                        f"{comment_exc}",
                        file=sys.stderr,
                    )
                raise RuntimeError(
                    f"Wiki ledger failed ({wiki_ledger.action or 'unknown'}: "
                    f"{wiki_ledger.error or 'unknown'}); "
                    f"Pulse PATCH failed for {ref}: {exc}"
                ) from exc
            # Wiki ok — keep the compact non-spammy refresh comment fallback.
            pulse_comment(api_base, token, ref,
                          f"Ledger refresh — {len(findings)} open finding(s) ({counts_md}); hash `{digest}`. "
                          f"See item description staleness note; run: {args.run_url or 'N/A'}")
            return f"ledger-comment:{ref}"
    body = {
        "mode": "task",
        "title": f"[security-ci] Findings ledger — {args.repo}"[:240],
        "description": description,
        "priority": PRIORITY.get(max_sev, "normal"),
        "severity": PULSE_SEVERITY.get(max_sev, "warning"),
        "workflow_name": "security-finding",
        "assignment_group": "security",
        "source": SECURITY_CI_SOURCE,
        "source_id": source_id,
        "source_url": args.run_url or "",
        "metadata": {"origin_observations": [origin_observation(args)]},
        "labels": (["security-ci", "security-ci:ledger", f"repo:{args.repo}", hash_label]
                   + ([] if wiki_ledger.ok else ["durable-write-failed"])),
        "assignee_id": None,
    }
    if args.project_id:
        body["project"] = int(args.project_id)
    created = pulse_request("POST", f"{api_base}/items/", token, body)
    ref = created.get("item_key") or created.get("id")
    try:
        if created.get("assignee") or created.get("assignee_id"):
            pulse_request("PATCH", f"{api_base}/items/{created.get('id')}/", token, {"assignee_id": None})
    except Exception as exc:
        print(f"WARN: could not clear auto-assigned assignee on {ref}: {exc}", file=sys.stderr)
    return f"ledger-created:{ref}"


def write_summary(path: str, findings: list[Finding], suppressed: list[tuple[Finding, str]], args: argparse.Namespace) -> None:
    lines = ["# Security CI summary", "", f"Repository: `{args.repo}`", f"SHA: `{args.sha or 'N/A'}`", ""]
    lines += [f"Active findings: **{len(findings)}**", f"Suppressed findings: **{len(suppressed)}**", ""]
    if findings:
        lines += ["## Active findings", "", "| Severity | Tool | Rule | Location | Title |", "|---|---|---|---|---|"]
        for f in findings[:200]:
            loc = f"{f.path}:{f.line}" if f.line else f.path
            title = f.title.replace("|", "\\|")[:160]
            lines.append(f"| {f.severity} | {f.tool} | `{f.rule}` | `{loc}` | {title} |")
    if suppressed:
        lines += ["", "## Suppressed findings", "", "| Tool | Rule | Source ID | Reason |", "|---|---|---|---|"]
        for f, reason in suppressed[:200]:
            lines.append(f"| {f.tool} | `{f.rule}` | `{f.source_id}` | {reason.replace('|', '/')} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def consolidate_dependency_findings(findings: list[Finding], repo: str) -> list[Finding]:
    """PLAT-2893: collapse the one-task-per-CVE flood for dependency findings.

    A single out-of-date package with N advisories (e.g. 19 django CVEs) is ONE
    logical fix (bump the package) but otherwise files N separate tasks, tripping
    the queue-starvation detector (the `SECURITY_CI_MIN_FILE_SEVERITY` gate limits
    by severity, not by package). Group osv/trivy findings by
    (repo, ecosystem, package) into ONE consolidated Finding per package listing
    every advisory. Non-package findings (semgrep/bandit code SAST) pass through
    unchanged. Even a single-advisory package uses the package-level identity so
    later advisories upsert the same task instead of forking a per-CVE task.

    The consolidated Finding's identity fields (tool='deps', repo+package-keyed
    `rule`, a stable representative `path`, a count-free `title`) are a pure
    function of (repo, ecosystem, package) — so its `source_id` is stable across
    runs regardless of how many advisories are present, letting repeated CI runs
    upsert the SAME task (composes with the source_id dedup). The advisory count
    and full list live in `detail`, which is not part of source_id, so the body
    can grow as advisories are discovered without spawning a new task.
    """
    groups: dict[tuple, list[Finding]] = {}
    passthrough: list[Finding] = []
    for f in findings:
        if f.package:
            groups.setdefault((repo, (f.ecosystem or "").lower(), f.package.lower()), []).append(f)
        else:
            passthrough.append(f)

    consolidated: list[Finding] = []
    for _key, group in groups.items():
        top = max(group, key=lambda x: SEV_RANK[x.severity])
        pkg = top.package or _key[2]
        eco = (top.ecosystem or "").strip()
        advisories = sorted({g.rule for g in group})
        paths = sorted({g.path for g in group})
        # Finding.source_id includes `path`, so dependency package findings must
        # use a synthetic package path rather than one of the current manifests.
        # Otherwise a later advisory discovered in an earlier/different manifest
        # would fork a second Pulse item for the same (repo, ecosystem, package).
        stable_path = f"deps/{eco or 'na'}/{pkg}"
        detail_lines = [
            f"{len(advisories)} advisories affect `{pkg}`"
            + (f" ({eco})" if eco else "")
            + f" in {repo}. One logical fix: upgrade `{pkg}` to a non-vulnerable version.",
            "",
            "Advisories (consolidated by PLAT-2893 — one task per package, not per CVE):",
        ]
        for g in sorted(group, key=lambda x: (-SEV_RANK[x.severity], x.rule)):
            detail_lines.append(
                f"- `{g.rule}` [{g.severity}] — {g.title}" + (f" ({g.url})" if g.url else "")
            )
        detail_lines += ["", "Manifests: " + ", ".join(f"`{p}`" for p in paths)]
        consolidated.append(Finding(
            tool="deps",
            rule=f"pkg:{repo}:{eco or 'na'}:{pkg}",  # stable per (repo, package)
            severity=top.severity,
            title=(f"Upgrade {pkg}" + (f" ({eco})" if eco else "") + " — dependency advisories")[:240],
            path=stable_path,
            line=None,
            detail="\n".join(detail_lines)[:4000],
            cwe=top.cwe,
            url=top.url,
            package=pkg,
            ecosystem=top.ecosystem,
        ))
    return consolidated + passthrough


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--semgrep")
    ap.add_argument("--bandit")
    ap.add_argument("--osv")
    ap.add_argument("--trivy")
    ap.add_argument("--allowlist", default=os.environ.get("SECURITY_CI_ALLOWLIST", ".security-ci-allowlist.json"))
    ap.add_argument("--summary", default=os.environ.get("SECURITY_CI_SUMMARY", "security-ci-summary.md"))
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or os.environ.get("FORGEJO_REPOSITORY") or "unknown/unknown")
    ap.add_argument("--ref", default=os.environ.get("GITHUB_REF") or "")
    ap.add_argument("--sha", default=os.environ.get("GITHUB_SHA") or "")
    ap.add_argument("--run-url", default=os.environ.get("GITHUB_SERVER_URL", "").rstrip('/') + "/" + os.environ.get("GITHUB_REPOSITORY", "") + "/actions/runs/" + os.environ.get("GITHUB_RUN_ID", "") if os.environ.get("GITHUB_RUN_ID") else "")
    ap.add_argument("--pulse-url", default=os.environ.get("PULSE_URL", ""))
    ap.add_argument("--pulse-api-url", default=os.environ.get("PULSE_API_URL", ""))
    ap.add_argument("--pulse-token", default=os.environ.get("PULSE_TOKEN", ""))
    ap.add_argument("--project-id", default=os.environ.get("PULSE_PROJECT_ID", ""))
    ap.add_argument("--wiki-api-url", default=os.environ.get("WIKI_API_URL", ""))
    ap.add_argument("--wiki-token", default=os.environ.get("WIKI_TOKEN", ""))
    ap.add_argument("--wiki-organization-id", type=int, default=int(os.environ.get("WIKI_ORGANIZATION_ID", "1")))
    ap.add_argument(
        "--wiki-index-page-id",
        default=os.environ.get("SECURITY_CI_WIKI_INDEX_PAGE_ID", SECURITY_WIKI_INDEX_PAGE_ID),
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fail-on", default=os.environ.get("SECURITY_CI_FAIL_ON", "high"), choices=sorted(SEV_RANK))
    args = ap.parse_args()

    raw_findings: list[Finding] = []
    for parser, path in [
        (parse_semgrep, args.semgrep),
        (parse_bandit, args.bandit),
        (parse_osv, args.osv),
        (parse_trivy, args.trivy),
    ]:
        try:
            raw_findings.extend(parser(load_json(path)))
        except Exception as exc:
            print(f"WARN: failed to parse {path}: {exc}", file=sys.stderr)

    # Deduplicate by source_id, keep highest severity copy.
    by_id: dict[str, Finding] = {}
    for f in raw_findings:
        old = by_id.get(f.source_id)
        if not old or SEV_RANK[f.severity] > SEV_RANK[old.severity]:
            by_id[f.source_id] = f
    raw_identities = sorted(
        by_id.values(),
        key=lambda f: (-SEV_RANK[f.severity], f.tool, f.path, f.line or 0),
    )

    # PLAT-5289: suppressions must be evaluated against the same stable
    # identities that are filed/upserted. Dependency scanners mint per-advisory
    # identities, while consolidate_dependency_findings() mints the final
    # package identity consumed by Pulse and the findings ledger. Normalize all
    # severities before the allowlist pass so low/medium dependency suppressions
    # receive the same coverage as high/critical ones. Code SAST findings are
    # passthrough identities, preserving their existing allowlist behaviour.
    all_findings = sorted(
        consolidate_dependency_findings(raw_identities, args.repo),
        key=lambda f: (-SEV_RANK[f.severity], f.tool, f.path, f.line or 0),
    )
    if len(all_findings) != len(raw_identities):
        print(
            f"security-ci: normalized {len(raw_identities)} raw finding(s) → "
            f"{len(all_findings)} stable finding identity/identities after "
            "per-package dedup (PLAT-2893)"
        )

    entries = load_allowlist(args.allowlist)
    findings: list[Finding] = []
    suppressed: list[tuple[Finding, str]] = []
    for f in all_findings:
        reason = allowlisted(f, entries)
        if reason:
            suppressed.append((f, reason))
        else:
            findings.append(f)

    print(f"security-ci: {len(findings)} active finding(s), {len(suppressed)} suppressed")
    for f in findings[:50]:
        loc = f"{f.path}:{f.line}" if f.line else f.path
        print(f"- {f.severity.upper():8s} {f.tool:7s} {f.rule} {loc} — {f.title[:160]}")
    if len(findings) > 50:
        print(f"... {len(findings) - 50} more")
    for f, reason in suppressed[:20]:
        print(f"suppressed {f.source_id}: {reason}")

    if args.summary:
        write_summary(args.summary, findings, suppressed, args)

    api_base = normalize_api_base(args.pulse_api_url or args.pulse_url)
    posting_requested = bool(api_base and args.pulse_token and not args.dry_run)
    if posting_requested:
        try:
            project_id = int(str(args.project_id).strip())
        except (TypeError, ValueError):
            project_id = 0
        if project_id <= 0:
            print(
                "ERROR: Pulse posting requires a positive PULSE_PROJECT_ID; "
                "refusing unscoped security-finding writes",
                file=sys.stderr,
            )
            return 2
    can_post = posting_requested
    post_errors = 0
    ledger_delivery_failed = False
    # 2026-07-03 flood post-mortem: filing EVERY finding created 646 Pulse tasks
    # in one day (105x bandit try/except-pass, 82x assert — style nits as tasks).
    # Only findings at/above SECURITY_CI_MIN_FILE_SEVERITY (default: high) become
    # Pulse items; everything else stays fully visible in the run summary + logs.
    file_min = SEV_RANK[os.environ.get("SECURITY_CI_MIN_FILE_SEVERITY", "high")]
    below = [f for f in findings if SEV_RANK[f.severity] < file_min]
    at_or_above = [f for f in findings if SEV_RANK[f.severity] >= file_min]
    # Findings already carry their final filing identities. Do not consolidate
    # again after severity filtering: doing so would make allowlist coverage
    # depend on SECURITY_CI_MIN_FILE_SEVERITY.
    to_file = at_or_above
    if below:
        print(f"security-ci: {len(below)} finding(s) below the filing threshold — tracked in summary only, not filed to Pulse")

    if can_post:
        if os.environ.get("SECURITY_CI_PER_FINDING") == "1":
            # Legacy per-finding filing (pre-HIVE-694), kept behind an env flag.
            for f in to_file:
                try:
                    print(f"pulse {f.source_id}: {pulse_create_or_update_finding(api_base, args.pulse_token, f, args)}")
                except Exception as exc:
                    # Filing findings into Pulse is best-effort telemetry for an
                    # ADVISORY scan — one bad payload / transient Pulse error must NOT
                    # abort the whole run (it used to `return 2` on the first failure,
                    # killing security-ci entirely). Log loudly and continue; the job's
                    # exit code is driven by the `--fail-on` severity gate below.
                    print(f"ERROR: Pulse create/update failed for {f.source_id}: {exc}", file=sys.stderr)
                    post_errors += 1
            if post_errors:
                print(f"security-ci: {post_errors}/{len(to_file)} filed-item(s) failed to file into Pulse (non-fatal; see errors above)", file=sys.stderr)
        else:
            # HIVE-694 default: ONE rolling ledger item per repo, updated in place.
            try:
                wiki_base = normalize_wiki_api_base(args.wiki_api_url)
                wiki_ledger = wiki_upsert_ledger(wiki_base, args.wiki_token, to_file, args)
                if wiki_ledger.ok:
                    print(f"wiki ledger: {wiki_ledger.action}:{wiki_ledger.page_id}")
                else:
                    print(
                        f"ERROR: Wiki ledger upsert failed ({wiki_ledger.action}): "
                        f"{wiki_ledger.error}",
                        file=sys.stderr,
                    )
                print(
                    f"pulse ledger: "
                    f"{pulse_upsert_ledger(api_base, args.pulse_token, to_file, args, wiki_ledger)}"
                )
            except Exception as exc:
                print(f"ERROR: Pulse ledger upsert failed: {exc}", file=sys.stderr)
                # The Wiki failure signal itself is delivered through Pulse. If
                # that durable sink is unavailable, fail the process so the
                # workflow's bounded retry and Origin run notifier become the
                # independent fail-loud path instead of losing the incident in
                # an otherwise-green CI log.
                ledger_delivery_failed = True
    elif findings:
        print("security-ci: Pulse posting skipped (dry-run or PULSE_URL/PULSE_API_URL/PULSE_TOKEN missing)")

    if ledger_delivery_failed:
        return 2

    threshold = SEV_RANK[args.fail_on]
    blocking = [f for f in findings if SEV_RANK[f.severity] >= threshold]
    if blocking:
        print(f"security-ci: failing because {len(blocking)} active finding(s) are >= {args.fail_on}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
