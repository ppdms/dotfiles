"""Conservative age evidence for content-addressed container artifacts.

Optional digest-bound registry publication evidence (docker-hub,
docker-hub-mirror, codeberg) replaces first-seen quarantine for the exact
full artifact when valid; verified proofs persist under state["publication"]
keyed by "<artifact-repository>@<manifest>", conservative max() of attached
and stored dates prevents stale resurrection, and every digest still needs
genuine verified observe() availability before approval. Builder created
never grants approval and never overrides trusted publication.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import time

MINIMUM_AGE = 7 * 24 * 60 * 60
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

_PUBLICATION_SOURCES = frozenset({"docker-hub", "docker-hub-mirror", "codeberg", "ghcr"})
_PUBLICATION_KEYS = frozenset({"source", "repository", "manifest", "config", "layers", "published_at"})
_REPO_HOST_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?\Z")
_REPO_PATH_PART_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*\Z")


class AgeError(RuntimeError):
    pass


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _check_repo_string(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise AgeError(f"unsafe {what} repository")
    if not value or value != value.strip():
        raise AgeError(f"unsafe {what} repository")
    if any(char.isspace() for char in value):
        raise AgeError(f"unsafe {what} repository")
    if "@" in value or "$" in value or "?" in value or "#" in value or ":" in value:
        raise AgeError(f"unsafe {what} repository")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise AgeError(f"unsafe {what} repository")
    if "/" not in value:
        raise AgeError(f"unsafe {what} repository")
    host, _, path = value.partition("/")
    if not host or not path:
        raise AgeError(f"unsafe {what} repository")
    if "." not in host:
        raise AgeError(f"unsafe {what} repository")
    if not _REPO_HOST_RE.fullmatch(host):
        raise AgeError(f"unsafe {what} repository")
    for part in path.split("/"):
        if not part or part in (".", ".."):
            raise AgeError(f"unsafe {what} repository")
        if not _REPO_PATH_PART_RE.fullmatch(part):
            raise AgeError(f"unsafe {what} repository")
    return value


def _artifact_repository(artifact: dict) -> str:
    reference = artifact.get("reference")
    if not isinstance(reference, str) or reference.count("@") != 1:
        raise AgeError("artifact has no valid reference for publication binding")
    repo, _, digest_part = reference.partition("@")
    _check_repo_string(repo, "artifact")
    manifest = artifact.get("manifest")
    if not isinstance(manifest, str) or not DIGEST.fullmatch(manifest):
        raise AgeError("artifact has an invalid content digest")
    if digest_part != manifest:
        raise AgeError("artifact reference does not match manifest for publication binding")
    return repo


def _validated_attached(artifact: dict) -> dict | None:
    if "publication" not in artifact:
        return None
    proof = artifact["publication"]
    if proof is None:
        return None
    _validate_publication(proof)
    source = proof["source"]
    proof_repo = proof["repository"]
    if proof["manifest"] != artifact.get("manifest"):
        raise AgeError("publication evidence does not match artifact manifest")
    if proof["config"] != artifact.get("config"):
        raise AgeError("publication evidence does not match artifact config")
    artifact_layers = artifact.get("layers")
    if not isinstance(artifact_layers, list) or proof["layers"] != artifact_layers:
        raise AgeError("publication evidence does not match artifact layers")
    artifact_repo = _artifact_repository(artifact)
    if not _stored_matches(artifact_repo, proof):
        raise AgeError("publication source does not match artifact repository")
    return {
        "source": source,
        "repository": proof_repo,
        "manifest": proof["manifest"],
        "config": proof["config"],
        "layers": list(proof["layers"]),
        "published_at": proof["published_at"],
    }


def _validate_publication(entry: object) -> None:
    if not isinstance(entry, dict) or set(entry.keys()) != _PUBLICATION_KEYS:
        raise AgeError("invalid publication evidence state")
    if not isinstance(entry["source"], str) or entry["source"] not in _PUBLICATION_SOURCES:
        raise AgeError("invalid publication evidence state")
    _check_repo_string(entry["repository"], "publication")
    for name in ("manifest", "config"):
        value = entry[name]
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            raise AgeError("invalid publication evidence state")
    layers = entry["layers"]
    if not isinstance(layers, list) or not layers:
        raise AgeError("invalid publication evidence state")
    for value in layers:
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            raise AgeError("invalid publication evidence state")
    published_at = entry["published_at"]
    if isinstance(published_at, bool) or not isinstance(published_at, (int, float)):
        raise AgeError("invalid publication evidence state")
    if isinstance(published_at, float) and not math.isfinite(published_at):
        raise AgeError("invalid publication evidence state")
    if published_at < 0:
        raise AgeError("invalid publication evidence state")


def _publication_winner(attached: dict | None, stored: dict | None) -> dict | None:
    if attached is None:
        return stored
    if stored is None:
        return attached
    if attached["published_at"] > stored["published_at"]:
        return attached
    return stored


def _stored_matches(artifact_repo: str, entry: dict) -> bool:
    source = entry["source"]
    proof_repo = entry["repository"]
    hosts = {"docker-hub": "docker.io/", "codeberg": "codeberg.org/", "ghcr": "ghcr.io/"}
    if source in hosts:
        return proof_repo.startswith(hosts[source]) and proof_repo == artifact_repo
    if source == "docker-hub-mirror":
        return (
            artifact_repo.startswith("lscr.io/linuxserver/")
            and proof_repo.startswith("docker.io/linuxserver/")
            and artifact_repo[len("lscr.io/"):] == proof_repo[len("docker.io/"):]
        )
    return False


class AgeStore:
    def __init__(self, path: Path, now: int | None = None):
        self.path = path
        self.live_clock = now is None
        self.now = time.time() if now is None else now
        self.data = json.loads(path.read_text()) if path.exists() else {
            "schema": 1, "last_observed": 0, "content": {}, "candidates": {}
        }
        if self.data.get("schema") != 1:
            raise AgeError("unsupported image observation state")
        if self.now < self.data["last_observed"]:
            raise AgeError("clock moved backwards; refusing to approve images")
        bucket = self.data.get("publication")
        if bucket is not None:
            if not isinstance(bucket, dict):
                raise AgeError("invalid publication evidence state")
            for key, entry in bucket.items():
                _validate_publication(entry)
                if not isinstance(key, str) or "@" not in key:
                    raise AgeError("invalid publication evidence state")
                repo, _, manifest = key.rpartition("@")
                _check_repo_string(repo, "publication")
                if entry["manifest"] != manifest:
                    raise AgeError("invalid publication evidence state")

    @staticmethod
    def digests(artifact: dict) -> list[str]:
        digests = [artifact["manifest"], artifact["config"], *artifact["layers"]]
        if not all(isinstance(value, str) and DIGEST.fullmatch(value) for value in digests):
            raise AgeError("artifact has an invalid content digest")
        return digests

    def _stored_publication(self, artifact: dict) -> dict | None:
        bucket = self.data.get("publication")
        if bucket is None:
            return None
        if not isinstance(bucket, dict):
            raise AgeError("invalid publication evidence state")
        manifest = artifact.get("manifest")
        if not isinstance(manifest, str):
            return None
        try:
            artifact_repo = _artifact_repository(artifact)
        except AgeError:
            return None
        entry = bucket.get(f"{artifact_repo}@{manifest}")
        if entry is None:
            return None
        _validate_publication(entry)
        if entry["manifest"] != manifest:
            raise AgeError("invalid publication evidence state")
        if entry["config"] != artifact.get("config"):
            return None
        artifact_layers = artifact.get("layers")
        if not isinstance(artifact_layers, list) or entry["layers"] != artifact_layers:
            return None
        if not _stored_matches(artifact_repo, entry):
            return None
        return {
            "source": entry["source"],
            "repository": entry["repository"],
            "manifest": entry["manifest"],
            "config": entry["config"],
            "layers": list(entry["layers"]),
            "published_at": entry["published_at"],
        }

    def _effective_publication(self, artifact: dict) -> dict | None:
        attached = _validated_attached(artifact)
        stored = self._stored_publication(artifact)
        return _publication_winner(attached, stored)

    def observe(self, artifact: dict) -> None:
        # The caller establishes blob availability before recording evidence.
        if self.live_clock:
            self.now = time.time()
        if self.now < self.data["last_observed"]:
            raise AgeError("clock moved backwards during observation")
        for digest in self.digests(artifact):
            self.data["content"].setdefault(digest, self.now)
        self.data["last_observed"] = self.now
        bucket = self.data.get("publication")
        if bucket is not None and not isinstance(bucket, dict):
            raise AgeError("invalid publication evidence state")
        attached = _validated_attached(artifact)
        if attached is None:
            return
        if bucket is None:
            bucket = {}
            self.data["publication"] = bucket
        artifact_repo = _artifact_repository(artifact)
        key = f"{artifact_repo}@{attached['manifest']}"
        stored = self._stored_publication(artifact)
        if stored is None:
            bucket[key] = attached
        elif attached["published_at"] > stored["published_at"]:
            bucket[key] = attached

    def eligible_at(self, artifact: dict) -> float | None:
        digests = self.digests(artifact)
        winner = self._effective_publication(artifact)
        observations = [self.data["content"].get(digest) for digest in digests]
        if any(observed is None for observed in observations):
            return None
        if not all(isinstance(observed, (int, float)) and 0 <= observed <= self.now for observed in observations):
            raise AgeError("invalid or future artifact observation time")
        if winner is not None:
            return winner["published_at"] + MINIMUM_AGE
        declared = artifact.get("created_at")
        if declared is not None and (not isinstance(declared, int) or declared < 0):
            raise AgeError("invalid declared image creation time")
        return max([*observations, *([declared] if declared is not None else [])]) + MINIMUM_AGE

    def eligible(self, artifact: dict) -> bool:
        deadline = self.eligible_at(artifact)
        return deadline is not None and self.now >= deadline

    def evidence_source(self, artifact: dict) -> str:
        self.digests(artifact)
        winner = self._effective_publication(artifact)
        if winner is not None:
            return winner["source"]
        return "observation"

    def save(self) -> None:
        atomic_json(self.path, self.data)
