"""Deterministic pinning of a self-built Dockerfile's upstream base tags.

The seven-day gate (``dockerfile_inputs``) refuses a build while a base image is
younger than seven days, and a floating tag such as ``node:24-alpine3.24`` is
republished whenever upstream ships, which restarts that wait. This module picks,
for every external FROM image, the newest *exact* version tag in the same release
series whose digest the gate already accepts, and rewrites only the tag:

    FROM node:24-alpine3.24 AS client
      ->  FROM docker.io/library/node:24.21.0-alpine3.24 AS client

Rules (all derived from the declared tag, nothing is configured):

* Registry names are made explicit first: ``node`` -> ``docker.io/library/node``,
  ``oven/bun`` -> ``docker.io/oven/bun``. The age store binds publication evidence
  to a fully qualified repository and refuses a bare Docker Hub name; the image is
  the same either way.
* A tag is ``<version><suffix>``: leading dotted digits, then an optional
  ``-``/``_`` suffix (the variant, e.g. ``-alpine3.24``). Candidates must carry the
  identical suffix, so the operating-system variant never changes.
* The *series* is the leading part of the version that identifies a release line:
  ``24`` -> major 24; ``1.99`` and ``1.99.0`` -> 1.99.x; ``3.24`` -> 3.24.x; with
  four or more components all but the last. A candidate must lie in the series.
* Within the series only the most specific tags qualify (the largest component
  count present), so floating aliases such as ``24`` or ``24.21`` are never picked.
* Candidates are tried newest first, at most ``MAX_CANDIDATES``; the first one the
  age store accepts wins. A base already on that tag is left as it is.

The answer depends only on the Dockerfile text, the registry state and the clock,
and every proposed Dockerfile is re-checked with the real gate before ``ok`` is
reported. Nothing here writes to the Dockerfile.
"""

from __future__ import annotations

import difflib
import re
import tempfile
from pathlib import Path

from dockerfile_inputs import (
    DockerfileInputsError,
    _check_artifact,
    _deadline_text,
    _dockerfile_digest,
    _parse_dockerfile,
    _render_line,
    collect_inputs,
)
from image_age import AgeError, AgeStore
from image_registry import Registry, RegistryError

__all__ = ["MAX_CANDIDATES", "candidates", "propose_pins", "qualify", "unified_patch"]

MAX_CANDIDATES = 12

_TAG_RE = re.compile(r"(?P<version>\d+(?:\.\d+)*)(?P<suffix>(?:[-_][A-Za-z0-9_.-]*)?)")


def qualify(repository: str) -> str:
    """Explicit registry name: bare Docker Hub names gain ``docker.io[/library]``."""
    head, separator, _ = repository.partition("/")
    if not separator:
        return f"docker.io/library/{repository}"
    if "." in head or ":" in head or head == "localhost":
        return repository
    return f"docker.io/{repository}"


def _split_tag(tag: str) -> tuple[tuple[int, ...], str] | None:
    match = _TAG_RE.fullmatch(tag)
    if match is None:
        return None
    return tuple(int(part) for part in match.group("version").split(".")), match.group("suffix")


def _series_length(depth: int) -> int:
    if depth == 1:
        return 1
    return 2 if depth <= 3 else depth - 1


def candidates(declared_tag: str, tags: list[str]) -> list[str]:
    """Exact tags in the declared tag's series, newest first; ``[]`` if it has none."""
    parsed = _split_tag(declared_tag)
    if parsed is None:
        return []
    parts, suffix = parsed
    prefix = parts[: _series_length(len(parts))]
    pool: list[tuple[tuple[int, ...], str]] = []
    for tag in dict.fromkeys(tags):
        other = _split_tag(tag)
        if other is None:
            continue
        other_parts, other_suffix = other
        if other_suffix == suffix and other_parts[: len(prefix)] == prefix:
            pool.append((other_parts, tag))
    if not pool:
        return []
    depth = max(len(parts) for parts, _ in pool)
    exact = sorted(((parts, tag) for parts, tag in pool if len(parts) == depth), reverse=True)
    return [tag for _, tag in exact]


def propose_pins(dockerfile: Path, store: AgeStore, registry: Registry | None = None) -> dict:
    """Return the pin proposal for ``dockerfile``; the file itself is never changed.

    Result keys: ``schema``, ``dockerfile_digest`` (of the bytes read), ``ok`` (the
    real gate accepts the rewritten Dockerfile right now), ``error`` (why not),
    ``changed``, ``bases`` (one entry per declared base) and ``rewritten`` (the whole
    proposed Dockerfile). A base's ``status`` is ``pin`` (tag changes), ``qualify``
    (same tag, registry name made explicit), ``keep`` (already as proposed),
    ``blocked`` (nothing old enough) or ``unpinnable`` (no version to choose by).
    Observations of every inspected candidate are recorded in ``store`` exactly as
    ``collect_inputs`` records them.
    """
    data = dockerfile.read_bytes()
    digest = _dockerfile_digest(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DockerfileInputsError("Dockerfile is not valid UTF-8") from None
    parsed = _parse_dockerfile(text)
    unique: list[dict] = parsed["unique"]
    if not unique:
        raise DockerfileInputsError("Dockerfile declares no external base images")
    active: Registry = registry if registry is not None else Registry()
    listings: dict[str, list[str]] = {}
    bases: list[dict] = []
    replacements: dict[str, str] = {}
    try:
        for item in unique:
            declared, repository, reference = item["declared"], item["repository"], item["reference"]
            qualified = qualify(repository)
            by_digest = reference.startswith("sha256:")
            default = f"{qualified}@{reference}" if by_digest else f"{qualified}:{reference}"
            entry: dict = {
                "declared": declared,
                "repository": qualified,
                "reference": reference,
                "status": "unpinnable",
                "chosen": None,
                "proposed": default,
                "eligible_at": None,
                "evidence": None,
                "downgrade": False,
                "checked": 0,
                "reason": "",
            }
            bases.append(entry)
            if default != declared:
                replacements[declared] = default
            if by_digest:
                entry["reason"] = "already fixed by digest"
                continue
            current = _split_tag(reference)
            if current is None:
                entry["reason"] = f"tag {reference!r} carries no version, so no release series to choose from"
                continue
            try:
                if qualified not in listings:
                    listings[qualified] = active.tags(qualified)
            except (RegistryError, OSError, ValueError) as error:
                raise RegistryError(f"Base image {declared}: cannot list tags: {error}") from None
            names = candidates(reference, listings[qualified])
            if not names:
                entry["reason"] = f"no tag of the series of {reference!r} is published"
                continue
            soonest: float | None = None
            for tag in names[:MAX_CANDIDATES]:
                try:
                    artifact = active.inspect(qualified, tag)
                    _check_artifact(declared, qualified, artifact)
                    store.observe(artifact)
                    deadline = store.eligible_at(artifact)
                    accepted = store.eligible(artifact)
                except (RegistryError, AgeError, OSError, ValueError) as error:
                    if isinstance(error, RegistryError):
                        raise RegistryError(f"Base image {declared}: {tag}: {error}", status=error.status) from None
                    raise
                entry["checked"] += 1
                if deadline is not None and (soonest is None or deadline < soonest):
                    soonest = deadline
                if not accepted:
                    continue
                proposed = f"{qualified}:{tag}"
                chosen_parts = _split_tag(tag)[0]
                entry.update(
                    status="pin" if tag != reference else ("keep" if proposed == declared else "qualify"),
                    chosen=tag,
                    proposed=proposed,
                    eligible_at=deadline,
                    evidence=store.evidence_source(artifact),
                    downgrade=chosen_parts < current[0],
                    reason="",
                )
                if proposed != declared:
                    replacements[declared] = proposed
                else:
                    replacements.pop(declared, None)
                break
            else:
                entry.update(
                    status="blocked",
                    eligible_at=soonest,
                    reason=(
                        f"none of the {entry['checked']} newest tags of the series is seven days old; "
                        f"the earliest becomes usable {_deadline_text(soonest)}"
                    ),
                )
        lines = text.splitlines(keepends=True)
        rewritten_lines = list(lines)
        for index, item in {occurrence["physical"]: occurrence for occurrence in parsed["occurrences"]}.items():
            replacement = replacements.get(item["declared"])
            if replacement is not None:
                rewritten_lines[index] = _render_line(lines[index], item["declared"], replacement, index + 1)
        rewritten = "".join(rewritten_lines)
        ok, error_text = True, None
        with tempfile.TemporaryDirectory(prefix="dockerfile-pins-") as directory:
            probe = Path(directory) / "Dockerfile"
            probe.write_bytes(rewritten.encode("utf-8"))
            try:
                collect_inputs(probe, store, require_aged=True, registry=active)
            except (DockerfileInputsError, RegistryError, AgeError) as error:
                ok, error_text = False, str(error)
    finally:
        store.save()
    return {
        "schema": 1,
        "dockerfile_digest": digest,
        "ok": ok,
        "error": error_text,
        "changed": rewritten != text,
        "bases": bases,
        "rewritten": rewritten,
    }


def unified_patch(original: str, rewritten: str, path: str) -> str:
    """A ``git apply``-ready unified diff of ``path``; empty when nothing changed."""
    if original == rewritten:
        return ""
    body: list[str] = []
    for line in difflib.unified_diff(
        original.splitlines(keepends=True),
        rewritten.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ):
        body.append(line)
        if not line.endswith("\n"):
            body.append("\n\\ No newline at end of file\n")
    return "".join(body)
