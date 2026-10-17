"""Read-only fleet agent template catalog.

These templates are single-agent blueprints formerly exposed through the local
``shizuha-cron`` MCP. Hive owns fleet creation, so Hive also owns discovery of
agent templates used to create/configure fleet agents.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings


MAX_TEMPLATE_FILE_BYTES = 512 * 1024
MAX_TEMPLATES_PER_DIR = 500


@dataclass(frozen=True)
class AgentTemplate:
    slug: str
    name: str
    description: str
    tags: list[str]
    category: str
    role: str
    requires: list[str]
    model: str
    execution_method: str
    skills: list[str]
    author: str
    version: str
    source: str
    path: str
    content: str

    def summary(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "category": self.category,
            "role": self.role,
            "requires": self.requires,
            "model": self.model,
            "execution_method": self.execution_method,
            "skills": self.skills,
            "author": self.author,
            "version": self.version,
            "source": self.source,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "content": self.content}


def configured_template_dirs() -> list[Path]:
    raw = getattr(settings, "HIVE_AGENT_TEMPLATE_DIRS", None)
    if raw is None:
        raw = os.environ.get("HIVE_AGENT_TEMPLATE_DIRS")

    if isinstance(raw, str):
        dirs = [p for p in raw.split(os.pathsep) if p]
    elif raw:
        dirs = list(raw)
    else:
        dirs = [
            "/opt/templates",
            os.path.expanduser("~/.shizuha/templates"),
        ]

    return [Path(p).expanduser() for p in dirs]


def load_agent_templates() -> list[AgentTemplate]:
    templates: list[AgentTemplate] = []
    seen: set[str] = set()
    for base_dir in configured_template_dirs():
        for template in _load_from_dir(base_dir):
            key = template.slug.lower()
            if key in seen:
                continue
            seen.add(key)
            templates.append(template)
    templates.sort(key=lambda t: (t.category.lower(), t.name.lower()))
    return templates


def search_agent_templates(query: str = "", max_results: int = 20) -> list[dict[str, Any]]:
    max_results = max(1, min(int(max_results or 20), 100))
    templates = load_agent_templates()
    if not query:
        return [t.summary() for t in templates[:max_results]]

    terms = [w for w in re.split(r"\s+", query.lower()) if len(w) > 1]
    scored: list[tuple[int, AgentTemplate]] = []
    for template in templates:
        score = _score_template(template, terms)
        if score > 0:
            scored.append((score, template))
    scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
    return [{**template.summary(), "score": score} for score, template in scored[:max_results]]


def get_agent_template(identifier: str) -> AgentTemplate | None:
    ident = (identifier or "").strip().lower()
    if not ident:
        return None
    for template in load_agent_templates():
        if template.slug.lower() == ident or template.name.lower() == ident:
            return template
    return None


def _load_from_dir(base_dir: Path) -> list[AgentTemplate]:
    if not base_dir.exists() or not base_dir.is_dir():
        return []

    out: list[AgentTemplate] = []
    for child in sorted(base_dir.iterdir(), key=lambda p: p.name.lower()):
        if len(out) >= MAX_TEMPLATES_PER_DIR:
            break
        if child.name.startswith(".") or child.name == "registry.json" or not child.is_dir():
            continue
        template_file = _find_template_file(child)
        if not template_file:
            continue
        template = _parse_template_file(template_file, child.name, base_dir)
        if template:
            out.append(template)
    return out


def _find_template_file(template_dir: Path) -> Path | None:
    for name in ("TEMPLATE.md", "template.md", "Template.md"):
        candidate = template_dir / name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _parse_template_file(path: Path, fallback_slug: str, base_dir: Path) -> AgentTemplate | None:
    try:
        if path.stat().st_size > MAX_TEMPLATE_FILE_BYTES:
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    frontmatter, body = _parse_frontmatter(raw)
    name = str(frontmatter.get("name") or fallback_slug)
    return AgentTemplate(
        slug=fallback_slug,
        name=name,
        description=str(frontmatter.get("description") or ""),
        tags=_as_string_list(frontmatter.get("tags")),
        category=str(frontmatter.get("category") or ""),
        role=str(frontmatter.get("role") or ""),
        requires=_as_string_list(frontmatter.get("requires")),
        model=str(frontmatter.get("model") or ""),
        execution_method=str(frontmatter.get("executionMethod") or frontmatter.get("execution_method") or ""),
        skills=_as_string_list(frontmatter.get("skills")),
        author=str(frontmatter.get("author") or ""),
        version=str(frontmatter.get("version") or ""),
        source=str(base_dir),
        path=str(path),
        content=body.strip(),
    )


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    text = raw.strip()
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    yaml_block = text[4:end].strip()
    body = text[end + 4 :].strip()
    return _parse_simple_yaml(yaml_block), body


def _parse_simple_yaml(yaml: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key = ""
    current_array: list[Any] | None = None
    for line in yaml.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^\s+-\s+", line) and current_key:
            current_array = current_array or []
            current_array.append(_parse_scalar(re.sub(r"^\s+-\s+", "", line).strip()))
            result[current_key] = current_array
            continue
        match = re.match(r"^(\S[\w-]*)\s*:\s*(.*)$", line)
        if not match:
            continue
        current_key = match.group(1)
        current_array = None
        value = match.group(2).strip()
        if not value or value in {"|", ">"}:
            continue
        result[current_key] = _parse_scalar(value)
    return result


def _parse_scalar(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return [_unquote(v.strip()) for v in value[1:-1].split(",") if v.strip()]
    if value.startswith("{") and value.endswith("}"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if value == "true":
        return True
    if value == "false":
        return False
    return _unquote(value)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _score_template(template: AgentTemplate, terms: list[str]) -> int:
    score = 0
    name = template.name.lower()
    slug = template.slug.lower()
    desc = template.description.lower()
    tags = " ".join(template.tags).lower()
    category = template.category.lower()
    role = template.role.lower()
    skills = " ".join(template.skills).lower()
    for term in terms:
        if term == slug or term == name:
            score += 40
        elif term in slug or term in name:
            score += 25
        if term in tags:
            score += 20
        if term in category:
            score += 15
        if term in role:
            score += 15
        if term in desc:
            score += 10
        if term in skills:
            score += 10
    return score
