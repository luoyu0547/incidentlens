"""Runtime skill registry: bounded, deterministic skill discovery.

A ``SkillRegistry`` scans an injectable ``skills/`` root for immediate child
directories containing a ``SKILL.md`` and exposes the discovered skills through
two read-only operations that later tasks use for the skill tools and the
system-prompt builder: ``list_skills()`` returns a compact catalog string and
``load_skill(name)`` returns the markdown body for a registered skill.

Safety and determinism are the point of this module:

* Only immediate child directories are scanned — never recursion, and never the
  development-only ``.claude/skills`` tree.
* A model-provided name is resolved only against the in-memory catalog, so a
  traversal-like ``../x`` or absolute ``/etc/passwd`` simply resolves as unknown
  and no arbitrary path is ever opened.
* Malformed or unreadable entries are skipped from the catalog and recorded as
  deterministic diagnostics.
* The catalog is sorted by name, so both ``catalog()`` and ``list_skills()`` are
  stable across runs.

Frontmatter is parsed as a small YAML-like subset (``name`` and ``description``
keys only) without pulling in a YAML library; absent or empty keys fall back to
the directory name and the first ``# `` heading.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_NAME_KEY = "name"
_DESCRIPTION_KEY = "description"
_SKILL_FILENAME = "SKILL.md"
_MAX_NAME = 120
_MAX_DESCRIPTION = 500


@dataclass(frozen=True)
class SkillInfo:
    """Bounded metadata and the full markdown body of one registered skill."""

    name: str
    description: str
    body: str


class SkillRegistry:
    """A filesystem-backed registry of runtime skills.

    ``root=None`` means an empty registry: no scanning, an empty catalog and no
    loadable skills. When a ``root`` is supplied, only its immediate child
    directories that contain a ``SKILL.md`` are catalogued.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        diagnostics: list[str] = []
        catalog: list[tuple[str, str, SkillInfo]] = []  # (name, dir name, info)
        if root is not None:
            self._scan(root, catalog, diagnostics)
        # Sort by skill name, tie-breaking on the source directory name, so the
        # catalog is deterministic even when two skills share a frontmatter name.
        catalog.sort(key=lambda item: (item[0], item[1]))
        self._catalog: tuple[SkillInfo, ...] = tuple(info for _, _, info in catalog)
        self._names: tuple[str, ...] = tuple(info.name for info in self._catalog)
        self.diagnostics: tuple[str, ...] = tuple(diagnostics)

    def catalog(self) -> tuple[SkillInfo, ...]:
        """Return the registered skills in deterministic (sorted by name) order."""
        return self._catalog

    def list_skills(self) -> str:
        """Return a compact, bounded catalog string for the prompt section."""
        if not self._catalog:
            return "No skills available."
        return "\n".join(
            f"- {info.name}: {info.description}" for info in self._catalog
        )

    def load_skill(self, name: str) -> str:
        """Return the skill body for a registered name, else a bounded diagnostic.

        Only registry names resolve; a model-provided name is never used to open
        a path, so traversal-like names simply report as unknown.
        """
        for info in self._catalog:
            if info.name == name:
                return info.body
        return f"unknown skill: {name}; available: {', '.join(self._names)}"

    # -- internal -------------------------------------------------------------

    def _scan(
        self,
        root: Path,
        catalog: list[tuple[str, str, SkillInfo]],
        diagnostics: list[str],
    ) -> None:
        if not root.is_dir():
            return
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
        for entry in entries:
            if not entry.is_dir():
                diagnostics.append(f"{entry.name} is not a directory; skipped")
                continue
            skill_file = entry / _SKILL_FILENAME
            if not skill_file.is_file():
                diagnostics.append(f"{entry.name}/{_SKILL_FILENAME} is missing; skipped")
                continue
            try:
                content = skill_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                diagnostics.append(
                    f"{entry.name}/{_SKILL_FILENAME} is unreadable "
                    f"({exc.__class__.__name__}); skipped"
                )
                continue
            info = self._build_info(entry, content)
            catalog.append((info.name, entry.name, info))

    @staticmethod
    def _build_info(entry: Path, content: str) -> SkillInfo:
        meta, body = _parse_frontmatter(content)
        name = _truncate(meta.get(_NAME_KEY) or entry.name, _MAX_NAME)
        description = _truncate(
            meta.get(_DESCRIPTION_KEY) or _first_heading(body), _MAX_DESCRIPTION
        )
        return SkillInfo(name=name, description=description, body=body)


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse a YAML-like frontmatter subset, returning ``(meta, body)``.

    When there is no leading ``---`` delimiter (or no closing delimiter) the
    whole file is treated as body with empty metadata.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content
    index = 1
    while index < len(lines) and lines[index].strip() not in ("---", "..."):
        index += 1
    if index >= len(lines):
        # Opened without a closing delimiter: treat the file as plain body.
        return {}, content
    meta: dict[str, str] = {}
    for line in lines[1:index]:
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip().strip("\"'")
        if key in (_NAME_KEY, _DESCRIPTION_KEY) and value:
            meta[key] = value
    body = "\n".join(lines[index + 1 :]).lstrip("\r\n")
    return meta, body


def _first_heading(body: str) -> str:
    """Return the text of the first ``# `` heading line, else an empty string."""
    for line in body.splitlines():
        if line.lstrip().startswith("# "):
            return line.lstrip()[2:].strip()
    return ""


def _truncate(value: str, limit: int) -> str:
    """Strip a value and bound it to ``limit`` characters for stable metadata."""
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."
