"""Public OCI/Docker-registry client over Python stdlib HTTPS.

Used by the image-update scanner to enumerate stable tags and verify that
a candidate linux/amd64 image (manifest, config and every layer blob) is
retrievable before attaching publication evidence or recording observation.

Public repositories on docker.io, ghcr.io, lscr.io, codeberg.org and other
OCI hosts. The updater-only broker-scoped GitHub PAT authenticates API calls
and GHCR pull-token exchanges; other registries retain anonymous access.

Guarantees relevant to the scanner:

* GHCR obtains repository-scoped pull tokens before sending registry requests
  when the GitHub PAT is configured. Other registries use bearer challenges.
  Tokens are cached per host+scope and refreshed once on 401. GitHub API
  credentials stay on api.github.com; the PAT's Basic exchange stays on
  ghcr.io/token. Cross-host blob/CDN redirects never receive credentials.
* Every RegistryError message is passed through a redactor covering
  signed-URL query parameters (signatures, tokens, keys) plus bearer/JWT
  shapes, so exception text is safe to log.
* Manifest and config bytes are always hash-verified: the registry's
  ``Docker-Content-Digest`` must equal ``sha256(body)``, and a manifest
  fetched through an index descriptor must match that descriptor digest.
* Multi-arch indexes resolve to exactly one linux/amd64 child; zero or
  multiple matches are an error, so the scanner can never quarantine the
  wrong architecture by accident.
* Foreign / nondistributable layers (external URLs) are rejected: blobs that
  do not live in the registry cannot be covered by the local availability
  quarantine.
* Builder ``created`` timestamps are supplemental metadata, not publication
  evidence. Registry-specific publication metadata is bound to the verified
  platform manifest, config, and complete layer list.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import cached_property

from image_publication import PublicationClient

__all__ = ["Registry", "RegistryError", "stable_tags"]

def _github_token() -> str | None:
    """Only the updater identity may read its optional scoped registry PAT."""
    try:
        from principals import current_principal
        if current_principal() != "image-updates":
            return None
        import homelab_secrets
        raw = homelab_secrets.load_field("image-updates", "github-token")
        return raw.decode("ascii") if raw is not None else None
    except (ImportError, OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        # An unavailable broker or invalid record cannot authorize age.
        return None


class RegistryError(Exception):
    """Sanitized registry failure: repository/reference/status only.

    Never carries bearer tokens, request headers, or raw HTTP error bodies.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_TAG_RE = re.compile(r"[\w][\w.\-]{0,127}\Z")
_NUM_RE = re.compile(r"\d+")
_CHALLENGE_PARAM_RE = re.compile(r'(\w+)=("[^"]*"|[^,\s]+)')
_LINK_NEXT_RE = re.compile(r'<([^>]*)>\s*;\s*rel="?next"?', re.IGNORECASE)

_MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
_INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
_CONFIG_TYPES = frozenset({
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
})

_USER_AGENT = "homelab-image-pins/1 (OCI distribution client)"
_PAGE_SIZE = 1000
_MAX_TAG_PAGES = 1000
_MAX_TAGS_PAGE = 8 * 1024 * 1024
_MAX_MANIFEST = 8 * 1024 * 1024
_MAX_CONFIG = 8 * 1024 * 1024
_MAX_TOKEN = 64 * 1024
_FUTURE_SKEW = 3600


def _split_repository(repository: str) -> tuple[str, str]:
    """Split ``registry/repo`` into (registry host, repo path).

    ``docker.io`` maps to the real Docker Hub endpoint
    ``registry-1.docker.io``; bare Docker Hub names gain ``library/``.
    """
    repo = (repository or "").strip().strip("/")
    if not repo or "@" in repo or any(ch.isspace() for ch in repo):
        raise RegistryError(f"invalid repository {repository!r}")
    head, sep, rest = repo.partition("/")
    if not sep:
        return ("registry-1.docker.io", f"library/{head}")
    if "." in head or ":" in head or head == "localhost":
        if not rest:
            raise RegistryError(f"invalid repository {repository!r}")
        host = "registry-1.docker.io" if head == "docker.io" else head
        return (host, rest)
    return ("registry-1.docker.io", repo)


def _normalize_reference(reference: str) -> str:
    """Accept a tag or a sha256 digest (with or without ``sha256:``)."""
    ref = (reference or "").strip()
    if not ref:
        raise RegistryError("empty image reference")
    if _DIGEST_RE.match(ref):
        return ref
    if _HEX64_RE.match(ref):
        return f"sha256:{ref}"
    if ref.startswith("sha256:"):
        raise RegistryError(
            f"malformed digest {ref!r}: expected sha256:<64 hex>")
    if not _TAG_RE.match(ref):
        raise RegistryError(
            f"invalid tag {ref!r}: expected [\\w][\\w.-]{{0,127}}")
    return ref


def _require_digest(value: object, what: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        raise RegistryError(
            f"invalid {what} digest {value!r}: expected sha256:<64 hex>")
    return value


def _parse_challenge(headers: object) -> tuple[str, str, str] | None:
    """Parse a ``WWW-Authenticate: Bearer realm=...`` challenge."""
    get = getattr(headers, "get", None)
    if get is None:
        return None
    auth = get("WWW-Authenticate") or get("www-authenticate")
    if not auth:
        return None
    scheme, _, params = auth.partition(" ")
    if scheme.lower() != "bearer" or not params.strip():
        return None
    kv = {}
    for match in _CHALLENGE_PARAM_RE.finditer(params):
        kv[match.group(1).lower()] = match.group(2).strip('"')
    if not kv.get("realm"):
        return None
    return (kv["realm"], kv.get("service", ""), kv.get("scope", ""))


_SIGNED_URL_RE = re.compile(
    r"(X-Amz-Signature|Signature|sig|token|access_token|secrete|secret"
    r"|password|passwd|pwd|auth|authorization|bearer|key|api[_-]?key"
    r"|session[_-]?token|x-ms-signature)=[^&\s'\"]+",
    re.IGNORECASE)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/=]+", re.IGNORECASE)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def _sanitize(text: object) -> str:
    """Redact signed-URL query params and bearer/JWT-shaped secrets."""
    cleaned = text if isinstance(text, str) else str(text)
    cleaned = _SIGNED_URL_RE.sub(r"\1=[REDACTED]", cleaned)
    cleaned = _BEARER_RE.sub(r"\1[REDACTED]", cleaned)
    return _JWT_RE.sub("[REDACTED]", cleaned)


def _next_tags_path(link: str | None) -> str | None:
    if not link:
        return None
    match = _LINK_NEXT_RE.search(link)
    if match is None:
        return None
    parts = urllib.parse.urlsplit(match.group(1))
    path = parts.path
    if parts.query:
        path = f"{path}?{parts.query}"
    if not path.startswith("/v2/"):
        raise RegistryError(
            f"unexpected tags page link {_sanitize(match.group(1))!r}")
    return path


def _read_capped(response: object, limit: int, what: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read = getattr(response, "read")
    while True:
        chunk = read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise RegistryError(f"{what} exceeds {limit} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_time(text: str) -> float | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned[-1:] in ("Z", "z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _created_at(config: dict) -> int | None:
    """Maximum valid declared timestamp, or None.

    Supplemental metadata only: the scanner's local observation history is
    the sole source of image age. Zero/unparseable/far-future values carry
    no evidence and are ignored.
    """
    candidates: list[str] = []
    created = config.get("created")
    if isinstance(created, str):
        candidates.append(created)
    history = config.get("history")
    if isinstance(history, list):
        for entry in history:
            if isinstance(entry, dict) and isinstance(entry.get("created"), str):
                candidates.append(entry["created"])
    now = time.time()
    best: float | None = None
    for text in candidates:
        stamp = _parse_time(text)
        if stamp is None or stamp <= 0 or stamp > now + _FUTURE_SKEW:
            continue
        if best is None or stamp > best:
            best = stamp
    return int(best) if best is not None else None


def stable_tags(
    policy: dict, tags: list[str]
) -> list[tuple[tuple[int, ...], str, str]]:
    """Rank tags matching ``policy["tag_pattern"]``, newest first.

    The pattern is a fullmatch regex with a required ``(?P<version>...)``
    group and an optional ``(?P<build>...)`` group (LinuxServer-style
    ``-lsN`` rebuild counters). Ordering is numeric over the version's digit
    runs, then the build number. Returns ``(ordering, tag, version)`` tuples
    sorted descending. Only tags the policy explicitly matches are returned,
    so prerelease/``dev``/``latest`` aliases never surface unless the policy
    deliberately pins them.
    """
    if not isinstance(policy, dict):
        raise ValueError("policy must be a dict")
    pattern = policy.get("tag_pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("policy is missing required 'tag_pattern'")
    try:
        expression = re.compile(pattern)
    except re.error as error:
        raise ValueError(f"invalid tag_pattern {pattern!r}: {error}") from None
    if "version" not in expression.groupindex:
        raise ValueError(
            f"tag_pattern {pattern!r} must define a (?P<version>...) group")
    has_build = "build" in expression.groupindex
    ranked: list[tuple[tuple[int, ...], str, str]] = []
    for tag in dict.fromkeys(tags):
        if not isinstance(tag, str):
            continue
        match = expression.fullmatch(tag)
        if match is None:
            continue
        version = match.group("version") or ""
        numbers = tuple(int(part) for part in _NUM_RE.findall(version))
        ordering = numbers if numbers else (0,)
        if has_build:
            raw_build = match.group("build") or ""
            ordering = ordering + (int(raw_build) if raw_build.isdigit() else 0,)
        ranked.append((ordering, tag, version))
    ranked.sort(reverse=True)
    return ranked


class _NoLeakRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep GitHub auth on its exact origin, never on blob/CDN handoffs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https":
            raise RegistryError("registry redirect must use HTTPS")
        authorization = req.get_header("Authorization") or ""
        if authorization.startswith("Basic "):
            raise RegistryError("authenticated token endpoint must not redirect")
        same_github_origin = (
            source.scheme == "https"
            and source.netloc in {"api.github.com", "ghcr.io"}
            and target.netloc == source.netloc
        )
        if source.netloc == "api.github.com" and not same_github_origin:
            raise RegistryError("GitHub API redirect must stay on api.github.com")
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and not same_github_origin:
            new_req.remove_header("Authorization")
        return new_req


class Registry:
    """Public OCI client with optional authenticated GitHub/GHCR access."""

    def __init__(self, *, timeout: float = 30) -> None:
        if not timeout or timeout <= 0:
            raise ValueError("timeout must be positive")
        self._timeout = timeout
        self._tokens: dict[tuple[str, str], str] = {}
        self._publication = PublicationClient(timeout=timeout, github_token=lambda: self._github_pat)
        self._opener = urllib.request.build_opener(
            _NoLeakRedirectHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    @cached_property
    def _github_pat(self) -> str | None:
        return _github_token()

    @cached_property
    def _github_login(self) -> str:
        response = self._api("api.github.com", "/user", scope="", accept="application/vnd.github+json")
        try:
            document = json.loads(_read_capped(response, _MAX_TOKEN, "GitHub identity"))
        except (ValueError, UnicodeDecodeError):
            raise RegistryError("GitHub credential identity is invalid") from None
        finally:
            response.close()
        login = document.get("login") if isinstance(document, dict) else None
        if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login):
            raise RegistryError("GitHub credential identity is invalid")
        return login

    def github_credentials(self) -> tuple[str, str] | None:
        """Resolve the optional Keyring PAT and its authenticated GitHub login."""
        if self._github_pat is None:
            return None
        return self._github_login, self._github_pat

    def release_tags(self, repository: str) -> list[str]:
        """Discover published stable GitHub releases, not CI/PR image tags."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid GitHub release repository")
        names = []
        for page in range(1, _MAX_TAG_PAGES + 1):
            path = f"/repos/{repository}/releases?per_page=100&page={page}"
            response = self._api("api.github.com", path, scope="", accept="application/vnd.github+json")
            try:
                releases = json.loads(_read_capped(response, _MAX_TAGS_PAGE, "GitHub releases"))
            finally:
                response.close()
            if not isinstance(releases, list):
                raise RegistryError(f"{repository}: invalid release list")
            for release in releases:
                if not isinstance(release, dict) or not isinstance(release.get("tag_name"), str):
                    raise RegistryError(f"{repository}: invalid release")
                if not release.get("draft") and not release.get("prerelease"):
                    names.append(release["tag_name"])
            if len(releases) < 100:
                return names
        raise RegistryError(f"{repository}: release list exceeds page limit")

    def tags(self, repository: str) -> list[str]:
        """List all tags for ``repository`` (follows pagination)."""
        host, repo = _split_repository(repository)
        scope = f"repository:{repo}:pull"
        names: list[str] = []
        path = f"/v2/{repo}/tags/list?n={_PAGE_SIZE}"
        for _ in range(_MAX_TAG_PAGES):
            response = self._api(host, path, scope=scope,
                                 accept="application/json")
            try:
                body = _read_capped(response, _MAX_TAGS_PAGE,
                                    f"{repository} tag list")
                link = response.headers.get("Link")
            finally:
                response.close()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                raise RegistryError(
                    f"{repository}: tag list is not valid JSON") from None
            if not isinstance(payload, dict):
                raise RegistryError(f"{repository}: tag list is not valid JSON")
            page = payload.get("tags") or []
            if not isinstance(page, list):
                raise RegistryError(f"{repository}: tag list is not valid JSON")
            names.extend(tag for tag in page if isinstance(tag, str))
            path = _next_tags_path(link)
            if path is None:
                return list(dict.fromkeys(names))
        raise RegistryError(f"{repository}: tag list exceeds page limit")

    def inspect(self, repository: str, reference: str) -> dict:
        """Verify and describe the linux/amd64 image for a tag or digest.

        Returns ``reference`` (``repository@<platform manifest digest>``),
        ``manifest``, ``config`` and ``layers`` digests, supplemental
        ``created_at``, and optional digest-bound ``publication`` evidence.
        """
        host, repo = _split_repository(repository)
        ref = _normalize_reference(reference)
        scope = f"repository:{repo}:pull"
        accept_any = ", ".join(_INDEX_TYPES + _MANIFEST_TYPES)
        raw, top_digest, top_type = self._fetch_manifest(
            host, repo, ref, scope, accept_any, repository)
        document = self._manifest_json(raw, repository, ref)
        media = document.get("mediaType") or top_type
        if media in _INDEX_TYPES or "manifests" in document:
            child = self._select_amd64(document, repository, ref)
            raw, platform_digest, child_type = self._fetch_manifest(
                host, repo, child, scope, ", ".join(_MANIFEST_TYPES),
                repository)
            if platform_digest != child:
                raise RegistryError(
                    f"{repository} {ref}: index entry {child} resolved to "
                    f"a different manifest {platform_digest}")
            document = self._manifest_json(raw, repository, ref)
            media = document.get("mediaType") or child_type
            if media not in _MANIFEST_TYPES or "config" not in document:
                raise RegistryError(
                    f"{repository} {ref}: expected an image manifest for "
                    f"linux/amd64, got media type {media!r}")
        elif media in _MANIFEST_TYPES:
            platform_digest = top_digest
        else:
            raise RegistryError(
                f"{repository} {ref}: unsupported manifest media type "
                f"{media!r} (only Docker schema2 and OCI manifests/indexes "
                "are accepted)")
        config_desc = document.get("config")
        layers_desc = document.get("layers")
        if not isinstance(config_desc, dict):
            raise RegistryError(
                f"{repository} {ref}: manifest has no config descriptor")
        if config_desc.get("mediaType") not in _CONFIG_TYPES:
            raise RegistryError(
                f"{repository} {ref}: unexpected config media type "
                f"{config_desc.get('mediaType')!r}")
        config_digest = _require_digest(config_desc.get("digest"),
                                        f"{repository} {ref} config")
        if not isinstance(layers_desc, list) or not layers_desc:
            raise RegistryError(
                f"{repository} {ref}: manifest has no layers")
        layer_digests: list[str] = []
        for position, layer in enumerate(layers_desc):
            entry = layer if isinstance(layer, dict) else {}
            digest = _require_digest(
                entry.get("digest"),
                f"{repository} {ref} layer[{position}]")
            layer_type = entry.get("mediaType") or ""
            if ("foreign" in layer_type or "nondistributable" in layer_type
                    or "urls" in entry):
                raise RegistryError(
                    f"{repository} {ref}: layer {digest} is foreign/external "
                    "(media type "
                    f"{layer_type!r}); external blobs bypass the local "
                    "availability quarantine and are not accepted")
            layer_digests.append(digest)
        config = self._fetch_config(host, repo, config_digest, scope,
                                    repository, ref)
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise RegistryError(f"{repository} {ref}: config is not linux/amd64")
        for digest in layer_digests:
            self._head_blob(host, repo, digest, scope, repository, ref)
        artifact = {
            "reference": f"{repository}@{platform_digest}",
            "manifest": platform_digest,
            "config": config_digest,
            "layers": layer_digests,
            "created_at": _created_at(config),
        }
        publication = self._publication.lookup(repository, ref, artifact, top_digest=top_digest)
        if publication is not None:
            artifact["publication"] = publication
        return artifact

    def _manifest_json(self, raw: bytes, repository: str, ref: str) -> dict:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            raise RegistryError(
                f"{repository} {ref}: manifest is not valid JSON") from None
        if not isinstance(document, dict):
            raise RegistryError(
                f"{repository} {ref}: manifest is not valid JSON")
        if document.get("schemaVersion") != 2:
            raise RegistryError(f"{repository} {ref}: unsupported manifest schema")
        return document

    @staticmethod
    def _select_amd64(document: dict, repository: str, ref: str) -> str:
        manifests = document.get("manifests")
        if not isinstance(manifests, list) or not manifests:
            raise RegistryError(
                f"{repository} {ref}: index lists no manifests")
        matches = []
        for entry in manifests:
            if not isinstance(entry, dict):
                continue
            platform = entry.get("platform") or {}
            if (platform.get("os") == "linux"
                    and platform.get("architecture") == "amd64"):
                matches.append(entry)
        if len(matches) != 1:
            raise RegistryError(
                f"{repository} {ref}: expected exactly one linux/amd64 "
                f"manifest in index, found {len(matches)}")
        return _require_digest(matches[0].get("digest"),
                               f"{repository} {ref} linux/amd64 index entry")

    def _fetch_manifest(self, host: str, repo: str, ref: str, scope: str,
                        accept: str, repository: str
                        ) -> tuple[bytes, str, str]:
        path = f"/v2/{repo}/manifests/{urllib.parse.quote(ref, safe=':')}"
        response = self._api(host, path, scope=scope, accept=accept)
        try:
            raw = _read_capped(response, _MAX_MANIFEST,
                               f"{repository} {ref} manifest")
            digest = (response.headers.get("Docker-Content-Digest") or "")
            content_type = response.headers.get_content_type()
        finally:
            response.close()
        _require_digest(digest.strip(),
                        f"{repository} {ref} manifest Docker-Content-Digest")
        digest = digest.strip()
        actual = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if actual != digest:
            raise RegistryError(
                f"{repository} {ref}: manifest digest mismatch "
                f"(registry said {digest}, bytes hash {actual})")
        if _DIGEST_RE.fullmatch(ref) and actual != ref:
            raise RegistryError(f"{repository} {ref}: requested manifest digest mismatch")
        return (raw, digest, content_type)

    def _fetch_config(self, host: str, repo: str, digest: str, scope: str,
                      repository: str, ref: str) -> dict:
        path = f"/v2/{repo}/blobs/{digest}"
        response = self._api(host, path, scope=scope,
                             accept="application/octet-stream")
        try:
            raw = _read_capped(response, _MAX_CONFIG,
                               f"{repository} {ref} config")
            header = (response.headers.get("Docker-Content-Digest") or "")
        finally:
            response.close()
        header = header.strip()
        if header and header != digest:
            raise RegistryError(
                f"{repository} {ref}: config digest mismatch "
                f"(descriptor {digest}, registry said {header})")
        actual = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if actual != digest:
            raise RegistryError(
                f"{repository} {ref}: config digest mismatch "
                f"(descriptor {digest}, bytes hash {actual})")
        try:
            config = json.loads(raw)
        except json.JSONDecodeError:
            raise RegistryError(
                f"{repository} {ref}: config blob is not JSON") from None
        if not isinstance(config, dict):
            raise RegistryError(
                f"{repository} {ref}: config blob is not JSON")
        return config

    def _head_blob(self, host: str, repo: str, digest: str, scope: str,
                   repository: str, ref: str) -> None:
        path = f"/v2/{repo}/blobs/{digest}"
        try:
            response = self._api(host, path, scope=scope,
                                 accept="application/octet-stream",
                                 method="HEAD")
        except RegistryError as error:
            if error.status in (405, 501):
                self._probe_blob(host, path, digest, scope, repository, ref)
                return
            raise
        try:
            status = getattr(response, "status", 200)
            header = (response.headers.get("Docker-Content-Digest") or "")
        finally:
            response.close()
        if status != 200:
            raise RegistryError(
                f"{repository} {ref}: layer {digest} unavailable "
                f"(HTTP {status})")
        header = header.strip()
        if header and header != digest:
            raise RegistryError(
                f"{repository} {ref}: layer {digest} digest mismatch "
                f"(registry said {header})")

    def _probe_blob(self, host: str, path: str, digest: str, scope: str,
                    repository: str, ref: str) -> None:
        """Range-probe fallback for registries without blob HEAD support."""
        response = self._api(host, path, scope=scope,
                             accept="application/octet-stream",
                             extra={"Range": "bytes=0-0"})
        try:
            response.read(1)
            status = getattr(response, "status", 200)
            header = (response.headers.get("Docker-Content-Digest") or "")
        finally:
            response.close()
        if status not in (200, 206):
            raise RegistryError(
                f"{repository} {ref}: layer {digest} unavailable "
                f"(HTTP {status})")
        header = header.strip()
        if header and header != digest:
            raise RegistryError(
                f"{repository} {ref}: layer {digest} digest mismatch "
                f"(registry said {header})")

    def _fetch_token(self, realm: str, service: str, scope: str, *, github: bool = False) -> str:
        parts = urllib.parse.urlsplit(realm)
        if github:
            if (realm != "https://ghcr.io/token" or service != "ghcr.io"
                    or not re.fullmatch(r"repository:[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)+:pull", scope)):
                raise RegistryError("refusing unexpected GHCR pull-token challenge")
            if self._github_pat is None:
                raise RegistryError("GitHub registry credential is unavailable")
        if parts.scheme != "https" or not parts.netloc:
            raise RegistryError(
                f"refusing token endpoint {realm!r}: https URL required")
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        names = {name for name, _ in query}
        if service and "service" not in names:
            query.append(("service", service))
        if scope and "scope" not in names:
            query.append(("scope", scope))
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path or "/",
             urllib.parse.urlencode(query), ""))
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if github:
            credentials = f"{self._github_login}:{self._github_pat}".encode("ascii")
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        request = urllib.request.Request(url, headers=headers)
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            raise RegistryError(
                f"token endpoint {parts.netloc}: HTTP {error.code}",
                status=error.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise RegistryError(
                f"token endpoint {parts.netloc}: unreachable "
                f"({_sanitize(error)})") from None
        try:
            if github and response.geturl() != url:
                raise RegistryError("authenticated GHCR token endpoint changed")
            body = _read_capped(response, _MAX_TOKEN, "bearer token")
        finally:
            response.close()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise RegistryError(
                f"token endpoint {parts.netloc}: invalid JSON") from None
        token = payload.get("token") or payload.get("access_token")
        if not token or not isinstance(token, str):
            raise RegistryError(
                f"token endpoint {parts.netloc}: no token issued")
        return token

    def _api(self, host: str, path: str, *, scope: str,
             accept: str = "*/*", method: str = "GET",
             extra: dict[str, str] | None = None):
        """GET/HEAD with one bearer challenge; failures wait for the next scan."""
        url = f"https://{host}{path}"
        refreshed = False
        github_pat = self._github_pat if host in {"api.github.com", "ghcr.io"} else None
        authenticated_ghcr = host == "ghcr.io" and github_pat is not None
        if authenticated_ghcr and (host, scope) not in self._tokens:
            self._tokens[(host, scope)] = self._fetch_token(
                "https://ghcr.io/token", "ghcr.io", scope, github=True)
        for _ in range(2):
            headers = {"User-Agent": _USER_AGENT, "Accept": accept}
            if extra:
                headers.update(extra)
            token = github_pat if host == "api.github.com" else self._tokens.get((host, scope))
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(url, headers=headers,
                                             method=method)
            try:
                return self._opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as error:
                if error.code == 401 and not refreshed and host != "api.github.com":
                    refreshed = True
                    challenge = _parse_challenge(error.headers)
                    error.close()
                    if challenge is None:
                        raise RegistryError(
                            f"{host}{path}: registry access denied "
                            "(no bearer challenge)") from None
                    realm, service, challenge_scope = challenge
                    effective = challenge_scope or scope
                    if authenticated_ghcr and effective != scope:
                        raise RegistryError("refusing unexpected GHCR pull-token scope")
                    fresh = self._fetch_token(realm, service, effective, github=authenticated_ghcr)
                    self._tokens[(host, scope)] = fresh
                    self._tokens[(host, effective)] = fresh
                    continue
                raise self._http_error(host, path, error) from None
            except (urllib.error.URLError, TimeoutError,
                    ConnectionError) as error:
                raise RegistryError(
                    f"{host}{_sanitize(path)}: request failed "
                    f"({_sanitize(error)})") from None
        raise RegistryError(f"{host}{_sanitize(path)}: authentication failed")

    @staticmethod
    def _http_error(host: str, path: str,
                    error: urllib.error.HTTPError) -> RegistryError:
        error.close()
        return RegistryError(f"{host}{_sanitize(path)}: HTTP {error.code}", status=error.code)
