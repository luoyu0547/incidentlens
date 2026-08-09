"""File-backed project memory store.

Provides atomic writes, safe scanning, bounded loading, and index
rebuilding for the ``.incidentlens/memory/`` directory.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import yaml

from incidentlens_control_plane.project_memory.domain import (
    MemoryCandidate,
    MemoryCatalogEntry,
    MemoryLimits,
    MemoryRecord,
    MemoryType,
    MemoryWriteResult,
)

MEMORY_DIR = ".incidentlens/memory"
MEMORY_INDEX = "MEMORY.md"

_FRONTMATTER_RE = re.compile(
    r"^---\n(.*?)\n---\n",
    re.DOTALL,
)


def _validate_name(name: str) -> None:
    if not re.match(r"^[a-z][a-z0-9\-]{1,62}$", name):
        raise ValueError(f"invalid memory name: {name!r}")


def _build_frontmatter(candidate: MemoryCandidate, now: datetime) -> str:
    return yaml.safe_dump(
        {
            "name": candidate.name,
            "type": candidate.type.value,
            "description": candidate.description,
            "created_at": now.isoformat(),
        },
        default_flow_style=False,
        sort_keys=False,
    ).strip()


def _record_from_path(path: Path, base_dir: Path) -> MemoryRecord | None:
    """Parse a single memory file and return a record, or *None* on error."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return None

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None

    if not isinstance(meta, dict):
        return None

    name = meta.get("name", "")
    mem_type = meta.get("type", "")
    description = meta.get("description", "")

    if not isinstance(name, str) or not isinstance(description, str):
        return None

    try:
        mtype = MemoryType(mem_type)
    except ValueError:
        return None

    size = path.stat().st_size
    created = meta.get("created_at", "")
    if isinstance(created, str) and created:
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError:
            created_at = datetime.now(tz=timezone.utc)
    else:
        created_at = datetime.now(tz=timezone.utc)

    return MemoryRecord(
        name=name,
        type=mtype,
        description=description,
        path=str(path.relative_to(base_dir.parent.parent)),
        created_at=created_at,
        size_bytes=size,
    )


class ProjectMemoryStore:
    """Safe, file-backed project memory store.

    Parameters
    ----------
    base_dir:
        The project root directory.  The memory directory will be
        resolved relative to this path.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()
        self._memory_dir = self._base_dir / MEMORY_DIR

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan(self) -> list[MemoryRecord]:
        """Scan ``.incidentlens/memory/`` and return all valid records."""
        if not self._memory_dir.is_dir():
            return []

        records: list[MemoryRecord] = []
        for entry in sorted(self._memory_dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".md":
                continue
            if entry.name == MEMORY_INDEX:
                continue
            record = _record_from_path(entry, self._memory_dir)
            if record is not None:
                records.append(record)
        return records

    # ------------------------------------------------------------------
    # Catalog (MEMORY.md)
    # ------------------------------------------------------------------

    def catalog(self) -> list[MemoryCatalogEntry]:
        """Parse the ``MEMORY.md`` index and return catalog entries."""
        index_path = self._memory_dir / MEMORY_INDEX
        if not index_path.is_file():
            return []

        raw = index_path.read_text(encoding="utf-8")
        entries: list[MemoryCatalogEntry] = []

        for match in re.finditer(r"^\| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", raw, re.MULTILINE):
            name = match.group(1).strip()
            mem_type_str = match.group(2).strip()
            description = match.group(3).strip()
            try:
                mem_type = MemoryType(mem_type_str)
            except ValueError:
                continue
            entries.append(
                MemoryCatalogEntry(name=name, type=mem_type, description=description)
            )

        return entries

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def write(
        self, candidate: MemoryCandidate, limits: MemoryLimits | None = None
    ) -> MemoryWriteResult:
        """Atomically write a memory file with YAML frontmatter.

        Raises ``ValueError`` if limits are exceeded.
        """
        limits = limits or MemoryLimits()
        _validate_name(candidate.name)

        if len(candidate.body.encode("utf-8")) > limits.max_body_bytes:
            raise ValueError(
                f"body exceeds {limits.max_body_bytes} bytes"
            )

        now = datetime.now(tz=timezone.utc)
        frontmatter = _build_frontmatter(candidate, now)
        frontmatter_bytes = len(frontmatter.encode("utf-8"))
        if frontmatter_bytes > limits.max_frontmatter_bytes:
            raise ValueError(
                f"frontmatter exceeds {limits.max_frontmatter_bytes} bytes"
            )

        content = f"---\n{frontmatter}\n---\n\n{candidate.body}\n"
        target = self._memory_dir / f"{candidate.name}.md"

        existed = target.is_file()
        self._atomic_write(target, content)

        record = _record_from_path(target, self._memory_dir)
        if record is None:
            raise RuntimeError(f"failed to re-read written file: {target}")

        return MemoryWriteResult(
            name=candidate.name,
            action="updated" if existed else "created",
            path=str(target.relative_to(self._base_dir)),
            record=record,
        )

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(
        self,
        names: Sequence[str],
        limits: MemoryLimits | None = None,
    ) -> list[MemoryRecord]:
        """Load memory files by name with path containment checks.

        Only names that resolve to files inside the memory directory are
        returned.  Results are truncated when the cumulative byte total
        exceeds ``max_body_bytes * max_total_entries``.
        """
        limits = limits or MemoryLimits()
        budget = limits.max_body_bytes * min(len(names), limits.max_total_entries)
        loaded: list[MemoryRecord] = []
        total = 0

        for name in names:
            _validate_name(name)
            target = (self._memory_dir / name).resolve()

            # Containment check — prevent path traversal.
            if not str(target).startswith(str(self._memory_dir.resolve())):
                continue

            md_path = target.with_suffix(".md")
            if not md_path.is_file():
                continue

            record = _record_from_path(md_path, self._memory_dir)
            if record is None:
                continue

            if total + record.size_bytes > budget:
                break

            loaded.append(record)
            total += record.size_bytes

        return loaded

    # ------------------------------------------------------------------
    # Index rebuilding
    # ------------------------------------------------------------------

    def rebuild_index(self) -> list[MemoryCatalogEntry]:
        """Rebuild ``MEMORY.md`` from the files on disk."""
        records = self.scan()
        lines = [
            "# Project Memory Index",
            "",
            "This file is the committed project Memory index for IncidentLens.",
            "It lists all tracked memory entries and is automatically rebuilt by the store.",
            "",
            "| Name | Type | Description |",
            "|------|------|-------------|",
        ]

        entries: list[MemoryCatalogEntry] = []
        for record in records:
            lines.append(f"| {record.name} | {record.type.value} | {record.description} |")
            entries.append(
                MemoryCatalogEntry(
                    name=record.name,
                    type=record.type,
                    description=record.description,
                )
            )

        index_path = self._memory_dir / MEMORY_INDEX
        self._atomic_write(index_path, "\n".join(lines) + "\n")
        return entries

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _atomic_write(self, target: Path, content: str) -> None:
        """Write *content* to *target* atomically via a temp file."""
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent),
            suffix=".tmp",
            prefix=target.stem,
        )
        try:
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
            Path(tmp).replace(target)
        except BaseException:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass
            raise
