"""Seven-day quarantine for upstream Tree build images (base layers only).

Local Tree code and locally built RUN/COPY layers are never gated. Every
external FROM image (including compiler/build stages) and all of its OCI
blobs must be observed for seven days before production may use it.
"""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from image_age import AgeError, AgeStore
from image_registry import Registry, RegistryError

__all__ = ["TreeInputsError", "collect_inputs", "render_dockerfile"]


class TreeInputsError(ValueError):
    """Fail-closed Tree input error (unresolved ref, changed file, young base)."""


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TAG_RE = re.compile(r"[\w][\w.\-]{0,127}\Z")
_ALIAS_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*\Z")
_REF_CHARS_RE = re.compile(r"[A-Za-z0-9_.\-/:@]+\Z")
_INSTRUCTION_RE = re.compile(r"\s*([A-Za-z]+)(?:\s+|$)(.*)", re.DOTALL)


def _dockerfile_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _deadline_text(deadline: float | None) -> str:
    if deadline is None:
        return "no age evidence"
    return datetime.fromtimestamp(deadline, timezone.utc).isoformat()


def _strip_trailing_comment(line: str) -> str:
    return re.sub(r"\s+#.*$", "", line)


def _logical_lines(physical: list[str]) -> list[dict]:
    """Join backslash continuations; comments never continue."""
    logical: list[dict] = []
    index = 0
    total = len(physical)
    while index < total:
        start = index
        first = physical[index]
        if first.lstrip().startswith("#"):
            logical.append({"start": start, "end": index, "text": first})
            index += 1
            continue
        if not first.strip():
            logical.append({"start": start, "end": index, "text": first})
            index += 1
            continue
        accumulated = ""
        while True:
            if index >= total:
                raise TreeInputsError("Dockerfile ends with line continuation '\\'")
            line = physical[index]
            code = line.rstrip()
            if code.endswith("\\"):
                accumulated += code[:-1] + " "
                index += 1
                continue
            accumulated += line
            logical.append({"start": start, "end": index, "text": accumulated})
            index += 1
            break
    return logical


def _instruction(line: str) -> tuple[str, str] | None:
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    match = _INSTRUCTION_RE.match(line)
    if not match:
        return None
    return (match.group(1).upper(), match.group(2))


def _split_declared(declared: str) -> tuple[str, str]:
    """Split a FROM image into (repository, tag-or-digest) for Registry.inspect."""
    if not declared or any(char.isspace() for char in declared):
        raise TreeInputsError(f"invalid FROM image {declared!r}")
    if "$" in declared:
        raise TreeInputsError(f"unresolved FROM image {declared!r}")
    if not _REF_CHARS_RE.fullmatch(declared):
        raise TreeInputsError(f"unsupported FROM image {declared!r}")
    if declared.count("@") > 1:
        raise TreeInputsError(f"unsupported FROM image {declared!r}")
    if "@" in declared:
        repo_tag, digest = declared.split("@", 1)
        if not repo_tag or not digest:
            raise TreeInputsError(f"unsupported FROM image {declared!r}")
        if not _DIGEST_RE.fullmatch(digest) and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise TreeInputsError(f"unsupported FROM digest {declared!r}")
        reference = digest if digest.startswith("sha256:") else f"sha256:{digest}"
        repository = _strip_tag(repo_tag)
        _check_repository(repository, declared)
        return (repository, reference)
    repository, separator, tag = declared.rpartition(":")
    if separator and "/" not in tag and repository:
        # A colon after the last slash is a tag; otherwise it is a registry port.
        if "/" in declared and declared.rfind(":") < declared.rfind("/"):
            repository, tag = declared, "latest"
        _check_repository(repository, declared)
        if not _TAG_RE.fullmatch(tag):
            raise TreeInputsError(f"unsupported FROM tag {declared!r}")
        return (repository, tag)
    _check_repository(declared, declared)
    return (declared, "latest")


def _strip_tag(repo_tag: str) -> str:
    head, separator, tail = repo_tag.rpartition(":")
    if separator and "/" not in tail and head:
        if "/" in repo_tag and repo_tag.rfind(":") < repo_tag.rfind("/"):
            return repo_tag
        return head
    return repo_tag


def _check_repository(repository: str, declared: str) -> None:
    if not repository or "@" in repository or "$" in repository:
        raise TreeInputsError(f"unsupported FROM image {declared!r}")
    if "/" in repository:
        first, rest = repository.split("/", 1)
        if not first or not rest:
            raise TreeInputsError(f"unsupported FROM image {declared!r}")
        if "." in first or ":" in first or first == "localhost":
            if ":" in rest:
                raise TreeInputsError(f"unsupported FROM image {declared!r}")
        elif ":" in repository:
            raise TreeInputsError(f"unsupported FROM image {declared!r}")
    elif ":" in repository:
        raise TreeInputsError(f"unsupported FROM image {declared!r}")


def _validate_stage_ref(
    value: str, aliases: set[str], total: int, where: str
) -> None:
    candidate = value.strip()
    if len(candidate) >= 2 and (
        (candidate[0] == '"' and candidate[-1] == '"')
        or (candidate[0] == "'" and candidate[-1] == "'")
    ):
        candidate = candidate[1:-1]
    if not candidate or "$" in candidate:
        raise TreeInputsError(f"{where} has an unresolved stage {value!r}")
    if candidate.isdigit():
        if 0 <= int(candidate) < total:
            return
        raise TreeInputsError(
            f"{where} refers to unknown stage {value!r}; "
            "declare the external image with FROM so it can be quarantined"
        )
    if candidate.lower() in aliases:
        return
    raise TreeInputsError(
        f"{where} refers to {value!r}, which is not a declared build stage; "
        "declare the external image with FROM so it can be quarantined"
    )


def _parse_dockerfile(text: str) -> dict:
    physical = text.splitlines()
    logical = _logical_lines(physical)
    aliases: set[str] = set()
    occurrences: list[dict] = []
    unique: list[dict] = []
    seen: dict[str, dict] = {}
    total = 0
    for entry in logical:
        parsed = _instruction(entry["text"])
        if parsed is None or parsed[0] != "FROM":
            continue
        if entry["start"] != entry["end"]:
            raise TreeInputsError(
                f"unsupported multi-line FROM on lines "
                f"{entry['start'] + 1}-{entry['end'] + 1}"
            )
        number = entry["start"] + 1
        code = _strip_trailing_comment(entry["text"])
        tokens = code.split()
        if not tokens or tokens[0].upper() != "FROM":
            raise TreeInputsError(f"unsupported FROM syntax on line {number}")
        cursor = 1
        while cursor < len(tokens) and tokens[cursor].startswith("--"):
            flag = tokens[cursor]
            if flag == "--platform":
                cursor += 1
                if cursor >= len(tokens):
                    raise TreeInputsError(
                        f"FROM on line {number} is missing its --platform value"
                    )
                if "$" in tokens[cursor] or not tokens[cursor]:
                    raise TreeInputsError(
                        f"unresolved FROM --platform on line {number}"
                    )
                cursor += 1
            elif flag.startswith("--platform="):
                value = flag[len("--platform="):]
                if not value or "$" in value:
                    raise TreeInputsError(
                        f"unresolved FROM --platform on line {number}"
                    )
                cursor += 1
            else:
                raise TreeInputsError(
                    f"unsupported FROM flag {flag!r} on line {number}"
                )
        if cursor >= len(tokens):
            raise TreeInputsError(f"FROM on line {number} is missing its image")
        image = tokens[cursor]
        cursor += 1
        if "$" in image:
            raise TreeInputsError(
                f"unresolved FROM image {image!r} on line {number}; "
                "declare an explicit image reference"
            )
        if not _REF_CHARS_RE.fullmatch(image):
            raise TreeInputsError(
                f"unsupported FROM image {image!r} on line {number}"
            )
        alias: str | None = None
        if cursor < len(tokens):
            if (
                len(tokens) - cursor != 2
                or tokens[cursor].upper() != "AS"
                or not tokens[cursor + 1]
            ):
                raise TreeInputsError(f"unsupported FROM syntax on line {number}")
            alias = tokens[cursor + 1]
            if "$" in alias or not _ALIAS_RE.fullmatch(alias):
                raise TreeInputsError(
                    f"unsupported FROM stage name {alias!r} on line {number}"
                )
        low = image.lower()
        if low == "scratch" or low.startswith("scratch:") or low.startswith("scratch@"):
            if low != "scratch":
                raise TreeInputsError(f"unsupported FROM image {image!r} on line {number}")
            if alias is not None:
                aliases.add(alias.lower())
            total += 1
            continue
        if image.isdigit():
            if 0 <= int(image) < total:
                if alias is not None:
                    aliases.add(alias.lower())
                total += 1
                continue
            raise TreeInputsError(
                f"FROM on line {number} refers to unknown stage {image!r}"
            )
        lowered = low
        if lowered in aliases:
            if alias is not None:
                aliases.add(alias.lower())
            total += 1
            continue
        try:
            repository, reference = _split_declared(image)
        except TreeInputsError as error:
            raise TreeInputsError(f"line {number}: {error}") from None
        occurrences.append(
            {
                "declared": image,
                "repository": repository,
                "reference": reference,
                "alias": alias,
                "physical": entry["start"],
                "line": number,
            }
        )
        if image not in seen:
            record = {
                "declared": image,
                "repository": repository,
                "reference": reference,
            }
            seen[image] = record
            unique.append(record)
        if alias is not None:
            aliases.add(alias.lower())
        total += 1
    if total == 0:
        raise TreeInputsError("Dockerfile contains no FROM instructions")
    # COPY/ADD --from and RUN --mount from= may only name internal stages.
    # Unknown or external names would implicitly pull a young image.
    for entry in logical:
        parsed = _instruction(entry["text"])
        if parsed is None:
            continue
        name = parsed[0]
        number = entry["start"] + 1
        if name in ("COPY", "ADD"):
            code = _strip_trailing_comment(entry["text"])
            tokens = code.split()
            cursor = 1
            while cursor < len(tokens):
                token = tokens[cursor]
                if token == "--from":
                    cursor += 1
                    if cursor >= len(tokens):
                        raise TreeInputsError(
                            f"{name} on line {number} is missing its --from value"
                        )
                    _validate_stage_ref(
                        tokens[cursor], aliases, total,
                        f"{name} --from on line {number}",
                    )
                    cursor += 1
                elif token.startswith("--from="):
                    value = token[len("--from="):]
                    if not value:
                        raise TreeInputsError(
                            f"{name} on line {number} is missing its --from value"
                        )
                    _validate_stage_ref(
                        value, aliases, total,
                        f"{name} --from on line {number}",
                    )
                    cursor += 1
                elif token.startswith("--from"):
                    raise TreeInputsError(
                        f"unsupported {name} flag {token!r} on line {number}"
                    )
                else:
                    cursor += 1
        elif name == "RUN":
            code = _strip_trailing_comment(entry["text"])
            tokens = code.split()
            cursor = 1
            while cursor < len(tokens) and tokens[cursor].startswith("--"):
                token = tokens[cursor]
                if token == "--mount":
                    cursor += 1
                    if cursor >= len(tokens) or tokens[cursor].startswith("--"):
                        raise TreeInputsError(
                            f"RUN on line {number} is missing its --mount value"
                        )
                    _check_mount(tokens[cursor], aliases, total, number)
                    cursor += 1
                elif token.startswith("--mount="):
                    spec = token[len("--mount="):]
                    if not spec:
                        raise TreeInputsError(
                            f"RUN on line {number} is missing its --mount value"
                        )
                    _check_mount(spec, aliases, total, number)
                    cursor += 1
                elif token.startswith("--mount"):
                    raise TreeInputsError(
                        f"unsupported RUN flag {token!r} on line {number}"
                    )
                elif "=" in token:
                    cursor += 1
                else:
                    ahead = cursor + 1
                    if ahead < len(tokens) and not tokens[ahead].startswith("--"):
                        cursor += 2
                    else:
                        cursor += 1
    return {
        "occurrences": occurrences,
        "unique": unique,
        "aliases": aliases,
        "total": total,
    }


def _check_mount(spec: str, aliases: set[str], total: int, number: int) -> None:
    for part in spec.split(","):
        text = part.strip()
        if not text or "=" not in text:
            continue
        key, _, value = text.partition("=")
        if key.strip().lower() != "from":
            continue
        candidate = value.strip()
        if (candidate.startswith('"') and candidate.endswith('"') and len(candidate) >= 2) or (
            candidate.startswith("'") and candidate.endswith("'") and len(candidate) >= 2
        ):
            candidate = candidate[1:-1]
        _validate_stage_ref(
            candidate, aliases, total, f"RUN --mount from on line {number}"
        )


def _check_artifact(declared: str, repository: str, artifact: dict) -> None:
    if not isinstance(artifact, dict):
        raise RegistryError(f"Tree base {declared}: invalid registry response")
    for key in ("reference", "manifest", "config", "layers"):
        if key not in artifact:
            raise RegistryError(f"Tree base {declared}: invalid registry response")
    manifest = artifact["manifest"]
    if not isinstance(manifest, str) or not _DIGEST_RE.fullmatch(manifest):
        raise RegistryError(f"Tree base {declared}: invalid manifest digest")
    expected = f"{repository}@{manifest}"
    if artifact.get("reference") != expected:
        raise RegistryError(
            f"Tree base {declared}: registry returned {artifact.get('reference')!r}, "
            f"expected {expected!r}"
        )


def _eligible_fallback(store: AgeStore, declared: str, latest_manifest: str) -> dict | None:
    tree_inputs = store.data.get("tree_inputs", {})
    if not isinstance(tree_inputs, dict):
        raise AgeError("invalid tree_inputs observation state")
    bucket = tree_inputs.get(declared, {})
    if not isinstance(bucket, dict):
        raise AgeError("invalid tree_inputs observation state")
    best: dict | None = None
    best_key: tuple[float, float, float, str] | None = None
    for manifest, artifact in bucket.items():
        if not isinstance(artifact, dict) or manifest == latest_manifest:
            continue
        try:
            if artifact.get("manifest") != manifest:
                continue
            if not store.eligible(artifact):
                continue
            eligible_at = store.eligible_at(artifact)
            observed = max(store.data["content"][digest] for digest in AgeStore.digests(artifact))
        except (AgeError, KeyError, TypeError):
            continue
        if eligible_at is None:
            continue
        created = artifact.get("created_at")
        if created is None:
            created_rank = float("-inf")
        elif isinstance(created, int) and created >= 0:
            created_rank = float(created)
        else:
            continue
        # Newest previously observed aged rebuild: latest observation wins,
        # then newest declared build, then eligibility time, then digest.
        key = (float(observed), created_rank, float(eligible_at), str(manifest))
        if best_key is None or key > best_key:
            best_key = key
            best = artifact
    return best


def collect_inputs(
    dockerfile: Path, store: AgeStore, require_aged: bool = False,
    registry: Registry | None = None,
) -> dict:
    """Observe upstream FROM images and return an immutable base-image spec.

    Without ``require_aged`` the latest declared inputs are observed and
    returned even while young. With ``require_aged`` only age-qualified
    inputs (latest or newest aged same-tag rebuild fallback) are returned,
    after revalidation by immutable digest. Partial successes are saved
    before any failure is reported.
    """
    data = dockerfile.read_bytes()
    digest = _dockerfile_digest(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TreeInputsError("Dockerfile is not valid UTF-8") from None
    parsed = _parse_dockerfile(text)
    unique: list[dict] = parsed["unique"]
    if not unique:
        raise TreeInputsError("Dockerfile declares no external base images")
    if not isinstance(store.data.get("tree_inputs", {}), dict):
        raise AgeError("invalid tree_inputs observation state")
    store.data.setdefault("tree_inputs", {})
    active: Registry = registry if registry is not None else Registry()
    latest: dict[str, dict] = {}
    for item in unique:
        declared = item["declared"]
        repository = item["repository"]
        reference = item["reference"]
        try:
            artifact = active.inspect(repository, reference)
            _check_artifact(declared, repository, artifact)
            store.observe(artifact)
        except (RegistryError, AgeError, OSError, ValueError) as error:
            store.save()
            if isinstance(error, RegistryError):
                raise RegistryError(
                    f"Tree base {declared}: {error}", status=error.status
                ) from None
            if isinstance(error, AgeError):
                raise AgeError(f"Tree base {declared}: {error}") from None
            raise TreeInputsError(f"Tree base {declared}: {error}") from None
        bucket = store.data["tree_inputs"].setdefault(declared, {})
        if not isinstance(bucket, dict):
            store.save()
            raise AgeError("invalid tree_inputs observation state")
        bucket[artifact["manifest"]] = artifact
        latest[declared] = artifact
    if not require_aged:
        store.save()
        for declared, artifact in latest.items():
            try:
                deadline = store.eligible_at(artifact)
            except AgeError as error:
                raise AgeError(f"Tree base {declared}: {error}") from None
            if deadline is not None and store.now >= deadline:
                print(f"Tree base {declared}: {artifact['manifest']} eligible ({store.evidence_source(artifact)})")
            else:
                print(
                    f"Tree base {declared}: {artifact['manifest']} quarantined "
                    f"until {_deadline_text(deadline)} ({store.evidence_source(artifact)})"
                )
        return {
            "schema": 1,
            "dockerfile_digest": digest,
            "images": copy.deepcopy(latest),
        }
    selected: dict[str, dict] = {}
    blocked: list[str] = []
    for item in unique:
        declared = item["declared"]
        artifact = latest[declared]
        try:
            if store.eligible(artifact):
                selected[declared] = artifact
                continue
        except AgeError as error:
            store.save()
            raise AgeError(f"Tree base {declared}: {error}") from None
        fallback = _eligible_fallback(store, declared, artifact["manifest"])
        if fallback is None:
            try:
                deadline = store.eligible_at(artifact)
            except AgeError as error:
                store.save()
                raise AgeError(f"Tree base {declared}: {error}") from None
            blocked.append(
                f"{declared} {artifact['manifest']} quarantined until "
                f"{_deadline_text(deadline)} (no aged artifact for this source)"
            )
        else:
            selected[declared] = fallback
    if blocked:
        store.save()
        raise TreeInputsError(
            "Tree production blocked: " + "; ".join(blocked)
        )
    for item in unique:
        declared = item["declared"]
        repository = item["repository"]
        artifact = selected[declared]
        try:
            checked = active.inspect(repository, artifact["manifest"])
            _check_artifact(declared, repository, checked)
        except (RegistryError, OSError, ValueError) as error:
            store.save()
            if isinstance(error, RegistryError):
                raise RegistryError(
                    f"Tree base {declared}: revalidation failed: {error}",
                    status=error.status,
                ) from None
            raise TreeInputsError(
                f"Tree base {declared}: revalidation failed: {error}"
            ) from None
        if (
            checked.get("reference") != artifact.get("reference")
            or checked.get("manifest") != artifact.get("manifest")
            or checked.get("config") != artifact.get("config")
            or checked.get("layers") != artifact.get("layers")
        ):
            store.save()
            raise TreeInputsError(
                f"Tree base {declared}: immutable artifact "
                f"{artifact['manifest']} changed at the registry"
            )
        try:
            store.observe(checked)
            if not store.eligible(checked):
                deadline = store.eligible_at(checked)
                store.save()
                raise TreeInputsError(
                    f"Tree base {declared}: {artifact['manifest']} "
                    f"quarantined until {_deadline_text(deadline)}"
                )
        except AgeError as error:
            store.save()
            raise AgeError(f"Tree base {declared}: {error}") from None
    store.save()
    for declared in latest:
        artifact = selected[declared]
        if artifact["manifest"] == latest[declared]["manifest"]:
            print(f"Tree base {declared}: {artifact['manifest']} eligible ({store.evidence_source(artifact)})")
        else:
            try:
                deadline = store.eligible_at(latest[declared])
            except AgeError:
                deadline = None
            print(
                f"Tree base {declared}: {artifact['manifest']} eligible ({store.evidence_source(artifact)}) "
                f"(latest {latest[declared]['manifest']} quarantined until "
                f"{_deadline_text(deadline)})"
            )
    return {
        "schema": 1,
        "dockerfile_digest": digest,
        "images": copy.deepcopy(selected),
    }


def _render_line(line: str, declared: str, pinned: str, number: int) -> str:
    ending = ""
    body = line
    if body.endswith("\r\n"):
        ending = "\r\n"
        body = body[:-2]
    elif body.endswith("\n"):
        ending = "\n"
        body = body[:-1]
    comment = ""
    match = re.search(r"\s+#", body)
    code = body
    if match:
        code = body[:match.start()]
        comment = body[match.start():]
    spans = [(item.group(0), item.start(), item.end()) for item in re.finditer(r"\S+", code)]
    if not spans or spans[0][0].upper() != "FROM":
        raise TreeInputsError(f"provenance mismatch on Dockerfile line {number}")
    cursor = 1
    while cursor < len(spans) and spans[cursor][0].startswith("--"):
        flag = spans[cursor][0]
        if flag == "--platform":
            cursor += 2
        elif flag.startswith("--platform="):
            cursor += 1
        else:
            raise TreeInputsError(f"unsupported FROM flag {flag!r} on line {number}")
    if cursor >= len(spans) or spans[cursor][0] != declared:
        raise TreeInputsError(f"provenance mismatch on Dockerfile line {number}")
    _, start, end = spans[cursor]
    return code[:start] + pinned + code[end:] + comment + ending


def render_dockerfile(dockerfile: Path, spec: dict) -> str:
    """Pin external FROM images to the collected immutable references."""
    data = dockerfile.read_bytes()
    digest = _dockerfile_digest(data)
    if not isinstance(spec, dict) or spec.get("schema") != 1:
        raise TreeInputsError("invalid Tree inputs specification")
    if spec.get("dockerfile_digest") != digest:
        raise TreeInputsError(
            f"Dockerfile {dockerfile} changed since observation "
            f"(current {digest} != pinned {spec.get('dockerfile_digest')!r}); "
            "re-run tree-inputs to re-observe"
        )
    images = spec.get("images")
    if not isinstance(images, dict):
        raise TreeInputsError("invalid Tree inputs specification")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TreeInputsError("Dockerfile is not valid UTF-8") from None
    parsed = _parse_dockerfile(text)
    declared_set = {item["declared"] for item in parsed["unique"]}
    if set(images) != declared_set:
        raise TreeInputsError(
            "Tree inputs provenance mismatch: Dockerfile declares "
            f"{sorted(declared_set)} but specification pins {sorted(images)}; "
            "re-run tree-inputs"
        )
    for declared, artifact in images.items():
        if not isinstance(artifact, dict):
            raise TreeInputsError(
                f"invalid pinned artifact for {declared!r}"
            )
        manifest = artifact.get("manifest")
        config = artifact.get("config")
        layers = artifact.get("layers")
        reference = artifact.get("reference")
        if (
            not isinstance(manifest, str)
            or not _DIGEST_RE.fullmatch(manifest)
            or not isinstance(config, str)
            or not _DIGEST_RE.fullmatch(config)
            or not isinstance(layers, list)
            or not layers
            or not all(isinstance(layer, str) and _DIGEST_RE.fullmatch(layer) for layer in layers)
            or not isinstance(reference, str)
            or "@" not in reference
        ):
            raise TreeInputsError(f"invalid pinned artifact for {declared!r}")
        repository = _split_declared(declared)[0]
        if reference != f"{repository}@{manifest}":
            raise TreeInputsError(
                f"provenance mismatch for {declared!r}: pinned {reference!r} "
                f"does not match {repository}@{manifest}"
            )
    lines = text.splitlines(keepends=True)
    by_physical = {item["physical"]: item for item in parsed["occurrences"]}
    rendered: list[str] = []
    for index, line in enumerate(lines):
        item = by_physical.get(index)
        if item is None:
            rendered.append(line)
            continue
        declared = item["declared"]
        pinned = images[declared]["reference"]
        rendered.append(_render_line(line, declared, pinned, index + 1))
    return "".join(rendered)
