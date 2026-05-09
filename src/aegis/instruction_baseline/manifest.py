"""Instruction-baseline data model + capture / verify (v2.2.1).

The baseline is a JSON manifest the user reviews and signs off on:

.. code-block:: json

    {
      "version": 1,
      "created_at_ns": 1745000000000000000,
      "root": "/abs/path/to/repo",
      "files": {
        "CLAUDE.md":         "<sha3_256 hex>",
        "AGENTS.md":         "<sha3_256 hex>",
        ".mcp.json":         "<sha3_256 hex>",
        ".claude-plugin/plugin.json": "<sha3_256 hex>"
      }
    }

It lives next to the repo at ``.aegis/instruction_baseline.json``.
``snapshot(root)`` walks ``DEFAULT_INSTRUCTION_PATHS`` (plus any extra
glob patterns) and produces an :class:`InstructionBaseline`.
``diff_baseline(baseline, root)`` re-hashes the same set on the fly
and returns a :class:`DriftReport` of added / removed / modified
files.

The hashing is plain SHA3-256 over the file contents — no canonical
serialisation needed because instruction files are line-oriented text
the user authored. A whitespace change is treated as drift on
purpose: poisoning often hides in trailing-whitespace-only diffs.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# v2.2: the canonical instruction-surface set. Glob patterns relative
# to the repo root, evaluated by Path.glob.
#
# v4.4 added ``tools/hooks/*.py`` — the Claude Code hook scripts
# themselves. They run with full session privileges (read transcript,
# append to audit), so a tampered hook is a high-impact compromise:
# it could exfil prompts, falsify cost data, or silently downgrade
# decisions. Baselining the hook SHA3s lets step309 detect
# self-tamper between ``aegis baseline init`` and any subsequent
# session.
DEFAULT_INSTRUCTION_PATHS: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    ".mcp.json",
    ".claude-plugin/plugin.json",
    ".claude/skills/*.md",
    ".claude/commands/*.md",
    ".cursor/rules/*.mdc",
    "tools/hooks/*.py",
)

# PR-E (OpenClaw + Local OSS LLM) — model-weight files. These are
# the per-deployment LLM artifacts (GGUF / safetensors / pytorch
# checkpoints) the local OSS track loads at runtime. Baselining
# their SHA3 catches:
#
#   * Quantization swap (fp16 → q4) silently flipping behaviour
#   * Supply-chain tampering on the artifact file itself
#   * Accidental upgrade (model.gguf overwritten by a new release)
#
# Step 309 differentiates this category from instruction drift in
# its error message — model-weight drift is a higher-severity
# signal (much rarer to legitimately change) and warrants a
# distinct operator response.
DEFAULT_MODEL_WEIGHT_PATTERNS: tuple[str, ...] = (
    "models/*.gguf",
    "models/*.safetensors",
    "models/*.bin",
    "models/*.pt",
    "models/*/pytorch_model.bin",
    "models/*/model.safetensors",
)


def hash_file(path: Path) -> str:
    """Return SHA3-256 hex of the file's contents."""
    h = hashlib.sha3_256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class InstructionBaseline:
    """Snapshot of instruction-file + (PR-E) model-weight hashes at a
    point in time.

    Two dicts so step309 can render a per-category drift message:

    * ``files`` — instruction files (CLAUDE.md / .mcp.json / hooks /
      skill manifests). Drift here usually means "the user (or an
      attacker) edited a config file"; common workflow recovery is
      ``aegis baseline reattest``.
    * ``model_weights`` (PR-E) — local OSS LLM artifacts (GGUF /
      safetensors). Drift here is much rarer and almost always
      indicates either a deliberate model upgrade (operator runs
      ``aegis baseline reattest --include-model-weights`` after
      updating ``models/llama-3.1-8b-q4.gguf``) or a supply-chain
      tamper. The error message distinguishes the two so the
      operator response is clear.
    """

    version: int
    created_at_ns: int
    root: str
    files: dict[str, str]
    model_weights: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "version": self.version,
            "created_at_ns": self.created_at_ns,
            "root": self.root,
            "files": dict(self.files),
        }
        # Keep the manifest tidy on installs that don't use model
        # weights — empty dict is omitted so existing baselines
        # round-trip byte-for-byte unchanged.
        if self.model_weights:
            out["model_weights"] = dict(self.model_weights)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> InstructionBaseline:
        files_raw = data.get("files") or {}
        if not isinstance(files_raw, dict):
            raise ValueError("baseline 'files' must be a dict")
        weights_raw = data.get("model_weights") or {}
        if not isinstance(weights_raw, dict):
            raise ValueError("baseline 'model_weights' must be a dict")
        version_raw = data.get("version", 1)
        created_raw = data.get("created_at_ns", 0)
        return cls(
            version=int(version_raw) if isinstance(version_raw, (int, str)) else 1,
            created_at_ns=int(created_raw)
            if isinstance(created_raw, (int, str))
            else 0,
            root=str(data.get("root", "")),
            files={str(k): str(v) for k, v in files_raw.items()},
            model_weights={
                str(k): str(v) for k, v in weights_raw.items()
            },
        )


@dataclass(frozen=True)
class DriftReport:
    """Result of comparing a live tree against a baseline.

    All path lists are relative to ``root``. ``modified`` carries
    ``(path, baseline_hash, current_hash)`` so callers can render a
    short diff summary in a block message.

    PR-E adds the ``*_weights`` triplet so step309 can tell instruction
    drift from model-weight drift without re-classifying paths. The
    instruction-only properties (``added``/``removed``/``modified``)
    stay identical to v0.2.0 behaviour, so existing callers see no
    change unless they explicitly look at the new fields.
    """

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[tuple[str, str, str]] = field(default_factory=list)
    # PR-E — model-weight drift, separately accounted.
    added_weights: list[str] = field(default_factory=list)
    removed_weights: list[str] = field(default_factory=list)
    modified_weights: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (
            self.added or self.removed or self.modified
            or self.added_weights or self.removed_weights
            or self.modified_weights
        )

    @property
    def has_model_drift(self) -> bool:
        """PR-E — true iff any model-weight category is non-empty.
        Step 309 surfaces this separately because it's a much higher-
        severity signal than ordinary instruction drift."""
        return bool(
            self.added_weights or self.removed_weights
            or self.modified_weights
        )

    @property
    def has_instruction_drift(self) -> bool:
        return bool(self.added or self.removed or self.modified)

    def summary(self) -> str:
        bits: list[str] = []
        if self.added:
            bits.append(f"+{len(self.added)} added")
        if self.removed:
            bits.append(f"-{len(self.removed)} removed")
        if self.modified:
            bits.append(f"~{len(self.modified)} modified")
        # PR-E — separate the model-weight changes for the summary
        # one-liner. They appear after instruction changes so the
        # most-readable form is "+1 added, ~2 modified, ⚠ 1 model
        # weight modified".
        wbits: list[str] = []
        if self.added_weights:
            wbits.append(f"+{len(self.added_weights)} added")
        if self.removed_weights:
            wbits.append(f"-{len(self.removed_weights)} removed")
        if self.modified_weights:
            wbits.append(f"~{len(self.modified_weights)} modified")
        if wbits:
            bits.append("⚠ model weights: " + ", ".join(wbits))
        return ", ".join(bits) or "no drift"


def _resolve_paths(
    root: Path, patterns: tuple[str, ...] | list[str]
) -> list[Path]:
    """Resolve glob patterns under ``root`` to existing file paths.

    Plain (non-glob) patterns are accepted as direct file paths. The
    result is sorted so the manifest is deterministic.
    """
    seen: set[Path] = set()
    for pattern in patterns:
        if any(c in pattern for c in "*?[]"):
            # Glob — relative to root.
            for p in root.glob(pattern):
                if p.is_file():
                    seen.add(p)
        else:
            p = root / pattern
            if p.is_file():
                seen.add(p)
    return sorted(seen)


def snapshot(
    root: Path,
    *,
    patterns: tuple[str, ...] | list[str] = DEFAULT_INSTRUCTION_PATHS,
    model_weight_patterns: tuple[str, ...] | list[str] | None = None,
) -> InstructionBaseline:
    """Walk ``patterns`` under ``root`` and produce an InstructionBaseline.

    PR-E: when ``model_weight_patterns`` is non-None, also walks those
    patterns under ``root`` and records each file's SHA3 in
    ``model_weights``. Pass ``DEFAULT_MODEL_WEIGHT_PATTERNS`` to
    enable model-weight baselining with the standard search paths,
    or a custom tuple for unusual deployment layouts. Pass ``None``
    (default) to skip model weights entirely — preserves v0.2.0
    behaviour for callers that haven't opted in.
    """
    root = root.resolve()
    files: dict[str, str] = {}
    for path in _resolve_paths(root, patterns):
        rel = path.relative_to(root).as_posix()
        files[rel] = hash_file(path)

    model_weights: dict[str, str] = {}
    if model_weight_patterns is not None:
        for path in _resolve_paths(root, model_weight_patterns):
            rel = path.relative_to(root).as_posix()
            model_weights[rel] = hash_file(path)

    return InstructionBaseline(
        version=1,
        created_at_ns=time.time_ns(),
        root=str(root),
        files=files,
        model_weights=model_weights,
    )


def _diff_one_dict(
    baseline_map: dict[str, str],
    root: Path,
    discovery_patterns: tuple[str, ...] | list[str],
) -> tuple[list[str], list[str], list[tuple[str, str, str]]]:
    """Helper: compute (added, removed, modified) for one category.

    Used twice — once for instruction files, once for model weights —
    so the dual-category diff stays DRY without forcing callers to
    care about the internal walking strategy.
    """
    live_hashes: dict[str, str] = {}
    for path in _resolve_paths(root, discovery_patterns):
        rel = path.relative_to(root).as_posix()
        live_hashes[rel] = hash_file(path)
    # Explicitly check baseline-keyed entries even if they don't
    # match the discovery patterns (e.g. baseline tracked a
    # bespoke path the default globs don't cover).
    for rel in baseline_map:
        if rel in live_hashes:
            continue
        candidate = root / rel
        if candidate.is_file():
            live_hashes[rel] = hash_file(candidate)

    added: list[str] = []
    removed: list[str] = []
    modified: list[tuple[str, str, str]] = []

    baseline_keys = set(baseline_map.keys())
    live_keys = set(live_hashes.keys())

    for rel in sorted(live_keys - baseline_keys):
        added.append(rel)
    for rel in sorted(baseline_keys - live_keys):
        removed.append(rel)
    for rel in sorted(baseline_keys & live_keys):
        if baseline_map[rel] != live_hashes[rel]:
            modified.append((rel, baseline_map[rel], live_hashes[rel]))

    return added, removed, modified


def diff_baseline(
    baseline: InstructionBaseline,
    root: Path,
    *,
    patterns: tuple[str, ...] | list[str] | None = None,
    model_weight_patterns: tuple[str, ...] | list[str] | None = None,
) -> DriftReport:
    """Compare the live tree under ``root`` against ``baseline``.

    Two-category compare (PR-E):

    * **Instruction files** — discovery is the union of paths in
      ``baseline.files`` ∪ the ``patterns`` argument (defaults to
      ``DEFAULT_INSTRUCTION_PATHS``). Same v0.2.0 behaviour.
    * **Model weights** — only checked when EITHER
      ``baseline.model_weights`` is non-empty OR
      ``model_weight_patterns`` is non-None. Discovery is the union
      of those two sources. Empty by default → no walking, no cost.

    Both categories produce drift independently. The DriftReport's
    legacy ``added``/``removed``/``modified`` continue to refer ONLY
    to instruction files (so existing callers see no change); the
    parallel ``*_weights`` triplet carries model-weight drift.
    """
    root = root.resolve()

    # ── Instruction files ───────────────────────────────────────
    if patterns is None:
        explicit: list[str] = list(baseline.files.keys())
        explicit.extend(DEFAULT_INSTRUCTION_PATHS)
        patterns = tuple(dict.fromkeys(explicit))  # de-dupe, keep order

    added, removed, modified = _diff_one_dict(
        baseline.files, root, patterns,
    )

    # ── Model weights (PR-E) ─────────────────────────────────────
    added_w: list[str] = []
    removed_w: list[str] = []
    modified_w: list[tuple[str, str, str]] = []
    if baseline.model_weights or model_weight_patterns is not None:
        weight_explicit: list[str] = list(baseline.model_weights.keys())
        weight_explicit.extend(model_weight_patterns or ())
        weight_patterns = tuple(dict.fromkeys(weight_explicit))
        added_w, removed_w, modified_w = _diff_one_dict(
            baseline.model_weights, root, weight_patterns,
        )

    # Filter glob-discovered fnmatches that the user may not care about.
    # Currently we surface everything; this is a placeholder for future
    # selective suppression (e.g. .claude/skills/*.md churn during dev).
    _ = fnmatch  # silence unused import (kept for future filtering)

    return DriftReport(
        added=added,
        removed=removed,
        modified=modified,
        added_weights=added_w,
        removed_weights=removed_w,
        modified_weights=modified_w,
    )


def write_baseline(baseline: InstructionBaseline, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_baseline(path: Path) -> InstructionBaseline:
    if not path.exists():
        raise FileNotFoundError(f"no instruction baseline at {path}")
    return InstructionBaseline.from_dict(json.loads(path.read_text(encoding="utf-8")))
