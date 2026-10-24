# SPDX-License-Identifier: Apache-2.0
"""Plans and patch directories. Existing project files are never modified.

apply writes a new directory with unified diffs, full patched copies and a
README, so a reviewer can see exactly what would change before applying it
with git apply or patch -p1, with or without version control.
"""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import EXTENSION_ID, VERSION
from .advisory import DEFAULT, AdvisorySet
from .scanner import PATCH_MANIFEST_SCHEMA, SEVERITIES, Edit, Finding, Report, is_patch_output

PLAN_SCHEMA = "sanka-trust-audit-plan/v1"
DEFAULT_OUTPUT = "sanka-trust-patches"


@dataclass(frozen=True)
class FilePatch:
    path: str
    original_sha256: str
    patched_sha256: str
    diff: str
    patched: bytes
    rules: tuple[str, ...]


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _split(text: str) -> list[str]:
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _diff(path: str, before: list[str], after: list[str]) -> str:
    out = []
    for line in difflib.unified_diff(before, after, f"a/{path}", f"b/{path}"):
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)


def build_patches(root: Path, findings: list[Finding]) -> list[FilePatch]:
    by_path: dict[str, list[tuple[Edit, str]]] = {}
    for finding in findings:
        if finding.edit is not None:
            by_path.setdefault(finding.edit.path, []).append((finding.edit, finding.rule))
    patches = []
    for path in sorted(by_path):
        raw = (root / path).read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        before = _split(text)
        after = list(before)
        edits = sorted({edit for edit, _ in by_path[path]}, key=lambda e: (e.start, e.end))
        if any(a.end >= b.start for a, b in itertools.pairwise(edits)):
            continue  # Overlapping edits; leave the file to manual review.
        for edit in reversed(edits):
            newline = "\r\n" if before and before[0].endswith("\r\n") else "\n"
            replaced = before[edit.start - 1 : edit.end]
            # The last replacement line keeps the replaced line's ending, so a
            # file without a final newline stays that way.
            last = replaced[-1][len(replaced[-1].rstrip("\r\n")) :] if replaced else newline
            new = [
                line.rstrip("\r") + (last if index == len(edit.lines) - 1 else newline)
                for index, line in enumerate(edit.lines)
            ]
            if edit.start > len(before) and before and not before[-1].endswith("\n"):
                after[-1] += newline
            after[edit.start - 1 : edit.end] = new
        patched = "".join(after).encode("utf-8")
        if patched == raw:
            continue
        patches.append(
            FilePatch(
                path=path,
                original_sha256=hashlib.sha256(raw).hexdigest(),
                patched_sha256=hashlib.sha256(patched).hexdigest(),
                diff=_diff(path, before, after),
                patched=patched,
                rules=tuple(sorted({rule for _, rule in by_path[path]})),
            )
        )
    return patches


def build_plan(
    root: Path, report: Report, advisories: AdvisorySet = DEFAULT
) -> tuple[dict[str, object], list[FilePatch]]:
    patches = build_patches(root, report.findings)
    patched_paths = {patch.path for patch in patches}
    findings = []
    for finding in report.findings:
        item = finding.as_json()
        item["patched"] = finding.edit is not None and finding.edit.path in patched_paths
        findings.append(item)
    body: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "extension": {"id": EXTENSION_ID, "version": VERSION},
        "advisory": advisories.descriptor(),
        "counts": report.counts(),
        "findings": findings,
        "patches": [
            {
                "path": patch.path,
                "original_sha256": patch.original_sha256,
                "patched_sha256": patch.patched_sha256,
                "diff_sha256": hashlib.sha256(patch.diff.encode("utf-8")).hexdigest(),
                "rules": list(patch.rules),
            }
            for patch in patches
        ],
        "manual_findings": sum(not item["patched"] for item in findings),
        "truncated": report.truncated,
    }
    plan_hash = "sha256:" + hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
    return {**body, "plan_hash": plan_hash}, patches


# Plain names keep the printed git apply / patch commands valid without quoting.
_OUTPUT_NAME = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")


def safe_output(root: Path, value: object) -> Path:
    name = value.strip().rstrip("/") if isinstance(value, str) else ""
    if not _OUTPUT_NAME.fullmatch(name):
        raise ValueError(
            "configuration.output must be a relative directory of letters, digits, '.', '_', "
            "'-' and '/'"
        )
    relative = PurePosixPath(name)
    if not relative.parts or ".." in relative.parts or relative.parts[0] in {".git", ".sanka"}:
        raise ValueError("configuration.output must be a new directory inside the project")
    target = root.joinpath(*relative.parts)
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("configuration.output must not pass through a symlink")
    return target


def _slug(index: int, path: str) -> str:
    return f"{index:03d}-" + re.sub(r"[^A-Za-z0-9._-]+", "-", path).strip("-") + ".patch"


def _readme(plan: dict[str, object], patches: list[FilePatch], output: str) -> str:
    findings = plan["findings"]
    assert isinstance(findings, list)
    counts = plan["counts"]
    assert isinstance(counts, dict)
    described = plan["advisory"]
    assert isinstance(described, dict)
    urls = [item["url"] for item in described.get("includes", [described])]
    lines = [
        f"# {described['title']}",
        "",
        f"Generated by `{EXTENSION_ID}` {VERSION}. Plan `{plan['plan_hash']}`.",
        f"Details: {', '.join(urls)}",
        "",
        "No file in your project was changed. This directory holds the proposed changes.",
        "",
        "| Severity | Findings |",
        "| --- | --- |",
        *[f"| {level} | {counts.get(level, 0)} |" for level in SEVERITIES],
        "",
    ]
    if patches:
        lines += [
            "## Apply the patches",
            "",
            f"{len(patches)} file(s) can be fixed mechanically. Review `trust-audit.patch` or the",
            "full copies under `files/`, then from the project root:",
            "",
            "```bash",
            f"git apply --check {output}/trust-audit.patch && git apply {output}/trust-audit.patch",
            "```",
            "",
            "Without git:",
            "",
            "```bash",
            f"patch -p1 --dry-run < {output}/trust-audit.patch && \\",
            f"  patch -p1 < {output}/trust-audit.patch",
            "```",
            "",
            "Then rebuild and redeploy what uses these files: a fix that never reaches a running",
            "service or image does not protect it.",
            "",
        ]
    manual = [f for f in findings if isinstance(f, dict) and not f.get("patched")]
    if manual:
        lines += ["## Needs a person", ""]
        for item in manual:
            where = f"{item['path']}:{item['line']}" if item.get("line") else str(item["path"])
            lines += [
                f"- **{item['severity']}** `{where}` — {item['title']}",
                f"  {item['action']}",
            ]
            if item.get("evidence"):
                lines.append(f"  Evidence: `{str(item['evidence']).replace('`', "'")}`")
        lines.append("")
    lines += [
        "## Not covered",
        "",
        "This is a static read of the files in this repository. It does not inspect deployed",
        "binaries, container images that were already built, operating-system trust stores on",
        "your hosts, appliances, or dependencies resolved at install time.",
        "",
    ]
    return "\n".join(lines)


def write_patch_directory(
    target: Path, plan: dict[str, object], patches: list[FilePatch], output: str, force: bool
) -> None:
    if target.exists() or target.is_symlink():
        if not force:
            raise FileExistsError(
                f"{output} already exists; choose another output or set force to replace it"
            )
        if target.is_symlink() or not target.is_dir() or not is_patch_output(target):
            raise FileExistsError(
                f"{output} was not created by {EXTENSION_ID}; refusing to replace it"
            )
        shutil.rmtree(target)
    staging = target.with_name(f".{target.name}.partial")
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "patches").mkdir(parents=True)
    combined = []
    for index, patch in enumerate(patches, 1):
        (staging / "patches" / _slug(index, patch.path)).write_text(patch.diff, encoding="utf-8")
        copy = staging / "files" / PurePosixPath(patch.path)
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(patch.patched)
        combined.append(patch.diff)
    (staging / "trust-audit.patch").write_text("".join(combined), encoding="utf-8")
    manifest_body = {
        "schema_version": PATCH_MANIFEST_SCHEMA,
        "plan_hash": plan["plan_hash"],
        "extension": plan["extension"],
        "advisory": plan["advisory"],
        "counts": plan["counts"],
        "patches": plan["patches"],
        "findings": plan["findings"],
    }
    (staging / "MANIFEST.json").write_text(
        json.dumps(manifest_body, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (staging / "README.md").write_text(_readme(plan, patches, output), encoding="utf-8")
    staging.rename(target)
