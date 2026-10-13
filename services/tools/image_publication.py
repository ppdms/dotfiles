"""Digest-bound registry publication dates for verified complete OCI images.

Docker Hub supplies the selected platform image's last_pushed, never a tag or
builder date. LinuxServer's Docker Hub mirror is usable only on exact platform
digest matches. Codeberg supplies digest-version created_at only
when package files cover the manifest, config, and every layer; Forgejo checks
blob existence before publishing that version. GHCR supplies container
package-version created_at only on exact platform manifest digest matches via
authenticated api.github.com versions; tags and index digests never supply
child age, and missing credentials or metadata fall back to observation.

The inspector verifies availability before lookup. Evidence binds the exact
manifest/config/layers; missing or unusable metadata returns None. Unknown
registries use observation. Dates are rounded up to whole seconds. Immutable
Hub digest lookups reuse exact cached proof because its public API only
supports tag lookup; GHCR digest lookups may query versions directly.
AgeStore keeps proof across scans.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import math
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

__all__ = ["PublicationClient"]


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_TAG_RE = re.compile(r"[\w][\w.\-]{0,127}\Z")
_HUB_SEGMENT_RE = re.compile(r"[a-z0-9]+[a-z0-9_.-]{0,127}\Z")
_CODEBERG_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_GHCR_OWNER_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?\Z")

_USER_AGENT = "homelab-image-pins/1 (publication metadata client)"
_MAX_BODY = 1024 * 1024
_MAX_REDIRECTS = 10
_HUB_HOST = "hub.docker.com"
_CODEBERG_HOST = "codeberg.org"
_GITHUB_HOST = "api.github.com"
_GITHUB_API_VERSION = "2022-11-28"
_GHCR_PER_PAGE = 100
_GHCR_MAX_PAGES = 100


class _FetchError(Exception):
    """Internal metadata-transport failure; lookup() maps it to None."""


def _split_repository(repository: str) -> tuple[str, str]:
    """Split ``registry/repo`` into (lowercased host, repo path)."""
    repo = (repository or "").strip().strip("/")
    if not repo or "@" in repo or any(ch.isspace() for ch in repo):
        raise ValueError(f"invalid repository {repository!r}")
    head, separator, rest = repo.partition("/")
    if not separator:
        return ("docker.io", head)
    if "." in head or ":" in head or head == "localhost":
        if not rest:
            raise ValueError(f"invalid repository {repository!r}")
        return (head.lower(), rest)
    return ("docker.io", repo)


def _hub_metadata_repository(path: str) -> str | None:
    """Canonical ``docker.io`` metadata repo, or None when not Hub-shaped."""
    parts = path.split("/")
    if len(parts) == 1:
        namespace, repo = "library", parts[0]
    elif len(parts) == 2:
        namespace, repo = parts
    else:
        return None
    if not _HUB_SEGMENT_RE.match(namespace) or not _HUB_SEGMENT_RE.match(repo):
        return None
    return f"docker.io/{namespace}/{repo}"


def _mirror_metadata_repository(path: str) -> str | None:
    """Hub metadata repo for ``lscr.io/linuxserver/*``, else None."""
    parts = path.split("/")
    if len(parts) != 2 or parts[0] != "linuxserver":
        return None
    if not _HUB_SEGMENT_RE.match(parts[1]):
        return None
    return f"docker.io/linuxserver/{parts[1]}"


def _codeberg_metadata_repository(path: str) -> str | None:
    """Canonical lowercased ``codeberg.org`` repo, or None when malformed."""
    parts = path.split("/")
    if len(parts) < 2:
        return None
    lowered = [part.lower() for part in parts]
    if any(not _CODEBERG_SEGMENT_RE.match(part) for part in lowered):
        return None
    return "codeberg.org/" + "/".join(lowered)


def _ghcr_metadata_repository(path: str) -> str | None:
    """Canonical lowercased ``ghcr.io`` repo, or None when malformed."""
    parts = path.split("/")
    if len(parts) < 2:
        return None
    lowered = [part.lower() for part in parts]
    if not _GHCR_OWNER_RE.match(lowered[0]):
        return None
    for segment in lowered[1:]:
        if not _HUB_SEGMENT_RE.match(segment):
            return None
    return "ghcr.io/" + "/".join(lowered)


def _route(host: str, path: str) -> tuple[str, str] | None:
    """Map (host, path) to (evidence source, metadata-source repo)."""
    if host in ("docker.io", "registry-1.docker.io"):
        repo = _hub_metadata_repository(path)
        return ("docker-hub", repo) if repo is not None else None
    if host == "lscr.io":
        repo = _mirror_metadata_repository(path)
        return ("docker-hub-mirror", repo) if repo is not None else None
    if host == "codeberg.org":
        repo = _codeberg_metadata_repository(path)
        return ("codeberg", repo) if repo is not None else None
    if host == "ghcr.io":
        repo = _ghcr_metadata_repository(path)
        return ("ghcr", repo) if repo is not None else None
    return None


def _artifact_binding(artifact: dict) -> tuple[str, str, list[str]] | None:
    """Extract the exact (manifest, config, layers) binding, or None."""
    if not isinstance(artifact, dict):
        return None
    manifest = artifact.get("manifest")
    config = artifact.get("config")
    layers = artifact.get("layers")
    if not isinstance(manifest, str) or not _DIGEST_RE.match(manifest):
        return None
    if not isinstance(config, str) or not _DIGEST_RE.match(config):
        return None
    if not isinstance(layers, list) or not layers:
        return None
    for layer in layers:
        if not isinstance(layer, str) or not _DIGEST_RE.match(layer):
            return None
    return (manifest, config, list(layers))


def _parse_time(value: object) -> float | None:
    """Parse a registry timestamp into finite positive epoch seconds."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned[-1:] in ("Z", "z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    try:
        stamp = moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    return stamp


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPS-only bounded redirects confined to the metadata provider."""

    _HOPS = "X-homelab-redirects"

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise _FetchError("metadata redirect must use HTTPS")
        if urllib.parse.urlsplit(newurl).netloc.lower() != urllib.parse.urlsplit(req.full_url).netloc.lower():
            raise _FetchError("metadata redirect must stay on the provider host")
        try:
            hops = int(req.get_header(self._HOPS) or 0)
        except (TypeError, ValueError):
            hops = 0
        if hops >= _MAX_REDIRECTS:
            raise _FetchError("metadata redirect limit exceeded")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        if urllib.parse.urlsplit(req.full_url).netloc != _GITHUB_HOST:
            new_req.remove_header("Authorization")
        new_req.remove_header(self._HOPS)
        new_req.add_header(self._HOPS, str(hops + 1))
        return new_req


class PublicationClient:
    """Registry-publication metadata client with optional GHCR credentials.

    ``lookup`` returns digest-bound evidence for the exact verified artifact,
    or ``None`` for metadata transport errors, malformed payloads, mismatches,
    and unavailable dates. Has no dependency on ``image_registry``: the
    inspector calls this client only after verifying the complete artifact.
    ``github_token`` is an optional lazy callback returning a read:packages
    token for api.github.com only; missing or invalid tokens disable GHCR
    evidence without network use, and the token never leaves the
    Authorization header.
    """

    def __init__(
        self,
        *,
        timeout: float = 30,
        github_token: Callable[[], str | None] | None = None,
    ) -> None:
        if not timeout or timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        self._github_token = github_token
        self._cache: dict[tuple[str, str, str], dict] = {}
        self._ghcr_owner_cache: dict[str, str] = {}
        self._ghcr_versions: dict[str, dict[str, int]] = {}
        self._ghcr_next_page: dict[str, int] = {}
        self._ghcr_exhausted: set[str] = set()
        self._opener = urllib.request.build_opener(
            _SafeRedirectHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def lookup(
        self,
        repository: str,
        reference: str,
        artifact: dict,
        *,
        top_digest: str | None = None,
    ) -> dict | None:
        """Return publication evidence for the verified artifact, or None.

        ``reference`` is the tag or digest the inspector resolved; ``artifact``
        carries the verified ``manifest``/``config``/``layers`` binding and
        ``top_digest`` the inspected top-level manifest digest when the
        reference resolved through an index. Hub digest references reuse cached
        proof; Codeberg and GHCR can query digest versions directly. GHCR
        ignores mutable tags and never uses an index digest for child age.
        Missing metadata returns None for the observation fallback.
        """
        try:
            return self._lookup(repository, reference, artifact, top_digest)
        except (_FetchError, ValueError, TypeError, KeyError, OverflowError):
            return None

    def _lookup(
        self,
        repository: str,
        reference: str,
        artifact: dict,
        top_digest: str | None,
    ) -> dict | None:
        binding = _artifact_binding(artifact)
        if binding is None:
            return None
        if top_digest is not None and (
            not isinstance(top_digest, str) or not _DIGEST_RE.match(top_digest)
        ):
            return None
        ref = reference.strip() if isinstance(reference, str) else ""
        if not ref:
            return None
        try:
            host, path = _split_repository(repository)
        except ValueError:
            return None
        route = _route(host, path)
        if route is None:
            return None
        provider, meta_repo = route
        if _DIGEST_RE.match(ref) or _HEX64_RE.match(ref):
            cached = self._cached(provider, meta_repo, binding)
            if cached is not None:
                return cached
            if provider == "codeberg":
                evidence = self._codeberg_evidence(meta_repo, binding)
            elif provider == "ghcr":
                evidence = self._ghcr_evidence(meta_repo, binding)
            else:
                return None
            return self._remember(evidence) if evidence is not None else None
        if not _TAG_RE.match(ref):
            return None
        if provider == "codeberg":
            # Content-addressed digest-version; the mutable tag is ignored.
            evidence = self._codeberg_evidence(meta_repo, binding)
        elif provider == "ghcr":
            # Content-addressed digest-version; the mutable tag is ignored.
            # The inspected index digest never supplies child age.
            evidence = self._ghcr_evidence(meta_repo, binding)
        else:
            evidence = self._hub_evidence(
                meta_repo, ref, binding, top_digest, source=provider
            )
        if evidence is None:
            return None
        return self._remember(evidence)

    def _cached(
        self, provider: str, meta_repo: str, binding: tuple[str, str, list[str]]
    ) -> dict | None:
        """Return cached evidence for an immutable digest reference, or None.

        Never performs network I/O: Docker Hub offers no digest lookup, and
        cross-scan persistence belongs to the AgeStore.
        """
        manifest, config, layers = binding
        cached = self._cache.get((provider, meta_repo, manifest))
        if cached is None:
            return None
        if cached["config"] != config or cached["layers"] != layers:
            return None
        return dict(cached, layers=list(cached["layers"]))

    def _remember(self, evidence: dict) -> dict:
        """Cache validated evidence, keeping the newest trustworthy date.

        A newer timestamp for the same exact binding supersedes an older one
        so stale payloads can never resurrect an earlier deadline.
        """
        key = (evidence["source"], evidence["repository"], evidence["manifest"])
        cached = self._cache.get(key)
        if (
            cached is not None
            and cached["config"] == evidence["config"]
            and cached["layers"] == evidence["layers"]
            and cached["published_at"] > evidence["published_at"]
        ):
            evidence = dict(cached, layers=list(cached["layers"]))
        self._cache[key] = dict(evidence, layers=list(evidence["layers"]))
        return dict(evidence, layers=list(evidence["layers"]))

    def _github_bearer(self) -> str | None:
        """Return the validated api.github.com token, or None without use."""
        callback = self._github_token
        if callback is None or not callable(callback):
            return None
        try:
            raw = callback()
        except Exception:
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("ascii")
            except Exception:
                return None
        if not isinstance(raw, str):
            return None
        token = raw.strip()
        if not token or len(token) > 1024:
            return None
        for char in token:
            code = ord(char)
            if code < 33 or code > 126:
                return None
        return token

    def _ghcr_owner_kind(self, owner: str) -> str | None:
        """Resolve ``orgs`` vs ``users`` via the public profile, or None."""
        key = owner.lower()
        cached = self._ghcr_owner_cache.get(key)
        if cached is not None:
            return cached
        path = f"/users/{urllib.parse.quote(owner, safe='')}"
        document = self._get_json(_GITHUB_HOST, path)
        if not isinstance(document, dict):
            return None
        login = document.get("login")
        kind = document.get("type")
        if not isinstance(login, str) or login.lower() != key:
            return None
        if kind == "Organization":
            resolved = "orgs"
        elif kind == "User":
            resolved = "users"
        else:
            return None
        self._ghcr_owner_cache[key] = resolved
        return resolved

    def _ghcr_evidence(
        self, meta_repo: str, binding: tuple[str, str, list[str]]
    ) -> dict | None:
        """Fetch GHCR container-version created_at for the exact digest."""
        manifest, config, layers = binding
        token = self._github_bearer()
        if token is None:
            return None
        segments = meta_repo[len("ghcr.io/"):].split("/")
        owner = segments[0]
        package = "/".join(segments[1:])
        kind = self._ghcr_owner_kind(owner)
        if kind is None:
            return None
        known = self._ghcr_versions.get(meta_repo)
        if known is not None and manifest in known:
            return {
                "source": "ghcr",
                "repository": meta_repo,
                "manifest": manifest,
                "config": config,
                "layers": list(layers),
                "published_at": known[manifest],
            }
        if meta_repo in self._ghcr_exhausted:
            return None
        start = self._ghcr_next_page.get(meta_repo, 1)
        if start > _GHCR_MAX_PAGES:
            return None
        version_map = self._ghcr_versions.setdefault(meta_repo, {})
        owner_enc = urllib.parse.quote(owner, safe="")
        package_enc = urllib.parse.quote(package, safe="")
        for page in range(start, _GHCR_MAX_PAGES + 1):
            path = (
                f"/{kind}/{owner_enc}/packages/container/{package_enc}/versions"
                f"?per_page={_GHCR_PER_PAGE}&page={page}&state=active"
            )
            payload = self._get_github_json(_GITHUB_HOST, path, token)
            if not isinstance(payload, list):
                return None
            if not payload:
                self._ghcr_exhausted.add(meta_repo)
                self._ghcr_next_page.pop(meta_repo, None)
                return None
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                version_id = entry.get("id")
                if isinstance(version_id, bool) or not isinstance(version_id, int):
                    continue
                if entry.get("deleted_at") is not None:
                    continue
                name = entry.get("name")
                if not isinstance(name, str) or not _DIGEST_RE.match(name):
                    continue
                metadata = entry.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                if metadata.get("package_type") != "container":
                    continue
                stamp = _parse_time(entry.get("created_at"))
                if stamp is None:
                    continue
                published = math.ceil(stamp)
                previous = version_map.get(name)
                if previous is None or published > previous:
                    version_map[name] = published
            if manifest in version_map:
                if len(payload) < _GHCR_PER_PAGE:
                    self._ghcr_exhausted.add(meta_repo)
                    self._ghcr_next_page.pop(meta_repo, None)
                else:
                    self._ghcr_next_page[meta_repo] = page + 1
                return {
                    "source": "ghcr",
                    "repository": meta_repo,
                    "manifest": manifest,
                    "config": config,
                    "layers": list(layers),
                    "published_at": version_map[manifest],
                }
            if len(payload) < _GHCR_PER_PAGE:
                self._ghcr_exhausted.add(meta_repo)
                self._ghcr_next_page.pop(meta_repo, None)
                return None
            self._ghcr_next_page[meta_repo] = page + 1
        self._ghcr_next_page[meta_repo] = _GHCR_MAX_PAGES + 1
        return None

    def _hub_evidence(
        self,
        meta_repo: str,
        tag: str,
        binding: tuple[str, str, list[str]],
        top_digest: str | None,
        *,
        source: str,
    ) -> dict | None:
        """Fetch Docker Hub per-image push evidence for one tag document."""
        manifest, config, layers = binding
        namespace, _, repo = meta_repo[len("docker.io/"):].partition("/")
        path = (
            f"/v2/repositories/{urllib.parse.quote(namespace, safe='')}"
            f"/{urllib.parse.quote(repo, safe='')}"
            f"/tags/{urllib.parse.quote(tag, safe='')}?page_size=100"
        )
        document = self._get_json(_HUB_HOST, path)
        if not isinstance(document, dict):
            return None
        if document.get("name") != tag:
            return None
        if source == "docker-hub" and top_digest is not None and document.get("digest") != top_digest:
            return None
        images = document.get("images")
        if not isinstance(images, list):
            return None
        stamps: list[float] = []
        for entry in images:
            if not isinstance(entry, dict):
                continue
            if entry.get("digest") != manifest:
                continue
            if entry.get("os") != "linux" or entry.get("architecture") != "amd64":
                continue
            if entry.get("status") != "active":
                continue
            stamp = _parse_time(entry.get("last_pushed"))
            if stamp is not None:
                stamps.append(stamp)
        if not stamps:
            return None
        return {
            "source": source,
            "repository": meta_repo,
            "manifest": manifest,
            "config": config,
            "layers": list(layers),
            "published_at": math.ceil(max(stamps)),
        }

    def _codeberg_evidence(
        self, meta_repo: str, binding: tuple[str, str, list[str]]
    ) -> dict | None:
        """Fetch Forgejo digest-version evidence after validating all files."""
        manifest, config, layers = binding
        segments = meta_repo[len("codeberg.org/"):].split("/")
        owner = urllib.parse.quote(segments[0], safe="")
        image = "/".join(urllib.parse.quote(part, safe="") for part in segments[1:])
        version = urllib.parse.quote(manifest, safe="")
        base = f"/api/v1/packages/{owner}/container/{image}/{version}"
        meta = self._get_json(_CODEBERG_HOST, base)
        if not isinstance(meta, dict):
            return None
        if meta.get("version") != manifest:
            return None
        stamp = _parse_time(meta.get("created_at"))
        if stamp is None:
            return None
        files = self._get_json(_CODEBERG_HOST, f"{base}/files")
        if not isinstance(files, list):
            return None
        want_manifest = manifest.partition(":")[2]
        want = {config.partition(":")[2]}
        want.update(layer.partition(":")[2] for layer in layers)
        seen: set[str] = set()
        manifest_file = False
        for entry in files:
            if not isinstance(entry, dict):
                continue
            sha = entry.get("sha256")
            if not isinstance(sha, str):
                continue
            sha = sha.lower()
            if not _HEX64_RE.match(sha):
                continue
            seen.add(sha)
            if entry.get("name") == "manifest.json" and sha == want_manifest:
                manifest_file = True
        if not manifest_file or not want.issubset(seen):
            return None
        return {
            "source": "codeberg",
            "repository": meta_repo,
            "manifest": manifest,
            "config": config,
            "layers": list(layers),
            "published_at": math.ceil(stamp),
        }

    def _get_json(self, host: str, path: str):
        """GET one JSON document over HTTPS (single attempt, no retries).

        Test seam: returns the parsed payload. Raises ``_FetchError`` on any
        transport, redirect, size, or decode failure.
        """
        if host == _GITHUB_HOST:
            token = self._github_bearer()
            if token is not None:
                return self._get_github_json(host, path, token)
        request = urllib.request.Request(
            f"https://{host}{path}",
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        return self._read_json(host, request)

    def _get_github_json(self, host: str, path: str, token: str):
        """GET one GitHub JSON document with a confined bearer token.

        Test seam: returns the parsed payload. Raises ``_FetchError`` on any
        transport, redirect, size, or decode failure without exposing the
        token in messages, URLs, or state.
        """
        if host != _GITHUB_HOST:
            raise _FetchError("GitHub metadata host mismatch")
        if not isinstance(token, str) or not token:
            raise _FetchError(f"{host}: metadata unavailable")
        for char in token:
            code = ord(char)
            if code < 33 or code > 126:
                raise _FetchError(f"{host}: metadata unavailable")
        if not isinstance(path, str) or not path.startswith("/"):
            raise _FetchError(f"{host}: metadata unavailable")
        request = urllib.request.Request(
            f"https://{host}{path}",
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                "Authorization": f"Bearer {token}",
            },
        )
        return self._read_json(host, request)

    def _read_json(self, host: str, request: urllib.request.Request):
        """Read one bounded HTTPS JSON document behind the safe redirecter."""
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as error:
            raise _FetchError(f"{host}: metadata unavailable") from error
        try:
            final = getattr(response, "geturl", None)
            if callable(final):
                parts = urllib.parse.urlsplit(final())
                if parts.scheme != "https" or (parts.hostname or "").lower() != host:
                    raise _FetchError(f"{host}: unsafe metadata redirect")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_BODY:
                    raise _FetchError(f"{host}: metadata exceeds size limit")
                chunks.append(chunk)
            body = b"".join(chunks)
        finally:
            response.close()
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise _FetchError(f"{host}: metadata is not valid JSON") from error
