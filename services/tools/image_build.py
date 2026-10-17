"""Generic commit-to-image pipeline for self-built homelab services.

A `BuildTarget` names one locally built image: its git checkout, Dockerfile,
the principal that runs `podman build` (the builder) and the principal that
runs the service (the owner). The helpers authorize upstream base images
through the seven-day updater gate, build clean committed code from a pinned
rendering with `--pull=never`, cache candidates by provenance labels, and move
exact image IDs between runtime stores. They make no service decisions: callers
(`tree-dev`, `homelab-image-build`) own stopping, tagging and health.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from dockerfile_inputs import _parse_dockerfile, _render_line, _split_declared
from dockerfile_pins import qualify, unified_patch
from principals import principal, run_as, stream_image, tool

REVISION_LABEL = "org.opencontainers.image.revision"


class BuildError(Exception):
    pass


def base_images_label(namespace: str) -> str:
    return f"{namespace}.base-images"


def dockerfile_digest_label(namespace: str) -> str:
    return f"{namespace}.dockerfile-digest"


@dataclass(frozen=True)
class BuildTarget:
    name: str
    repository: str
    checkout: Path
    dockerfile: str
    builder: str
    owner: str
    label_namespace: str
    markers: tuple[str, ...]
    revision_build_arg: str | None = None
    # Longest `podman build` may run, in seconds. A compile-heavy image needs
    # far more than a script-sized default; the build is killed if it stalls.
    build_timeout: int = 600

    @property
    def base_images_label(self) -> str:
        return base_images_label(self.label_namespace)

    @property
    def dockerfile_digest_label(self) -> str:
        return dockerfile_digest_label(self.label_namespace)

    def reference(self, tag: str) -> str:
        return f"{self.repository}:{tag}"


def podman(role: str, *arguments: str, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return run_as(
            role,
            [tool("podman"), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise BuildError("podman is not on PATH") from None


def image_exists(role: str, reference: str) -> bool:
    result = podman(role, "image", "exists", reference, timeout=30)
    if result.returncode not in (0, 1):
        raise BuildError(f"{role} image store is unavailable")
    return result.returncode == 0


def inspect_labels(role: str, image: str) -> dict | None:
    result = podman(role, "image", "inspect", "--format", "{{json .Labels}}", image, timeout=30)
    if result.returncode != 0:
        return None
    try:
        labels = json.loads(result.stdout.strip() or "null")
    except ValueError:
        raise BuildError(f"could not parse labels for {image}") from None
    return labels if isinstance(labels, dict) else {}


def image_identity(role: str, image: str) -> str:
    result = podman(role, "image", "inspect", "--format", "{{.Id}}", image, timeout=30)
    if result.returncode != 0:
        raise BuildError(f"could not inspect {image}: {result.stderr[-1000:]}")
    return result.stdout.strip()


def checked_image(role: str, image: str) -> str:
    """Resolve a reference to a validated image ID in one runtime store."""
    result = podman(role, "image", "inspect", "--format", "{{.Id}}", image, timeout=30)
    identity = result.stdout.strip()
    digest = identity.removeprefix("sha256:")
    if result.returncode or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise BuildError(f"required image is unavailable: {image}")
    return identity


def set_tag(role: str, image: str, reference: str) -> None:
    tagged = podman(role, "tag", image, reference, timeout=30)
    if tagged.returncode:
        raise BuildError(f"could not retag {reference}: {tagged.stderr[-1000:]}")


def remove_container(role: str, name: str) -> None:
    removed = podman(role, "rm", "--ignore", name, timeout=60)
    if removed.returncode:
        raise BuildError(f"could not remove stale container {name}: {removed.stderr[-1000:]}")


def checkout_root(target: BuildTarget) -> Path:
    root = target.checkout
    if not (root / ".git").exists():
        raise BuildError(f"{root} is not a git checkout")
    for marker in target.markers:
        if not (root / marker).is_file():
            raise BuildError(f"{root} does not look like the {target.name} checkout (missing {marker})")
    dockerfile = Path(target.dockerfile)
    if dockerfile.is_absolute() or ".." in dockerfile.parts or not (root / dockerfile).is_file():
        raise BuildError(f"{target.name} Dockerfile {target.dockerfile} is missing from {root}")
    return root


def git_rev(root: Path, allow_dirty: bool) -> str:
    try:
        status = subprocess.run(
            [tool("git"), "-c", f"safe.directory={root}", "status", "--porcelain"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        if status.returncode != 0:
            raise BuildError("git status failed; is this a git checkout?")
        if status.stdout.strip() and not allow_dirty:
            raise BuildError("working tree is dirty; commit or stash before deploy")
        rev = subprocess.run(
            [tool("git"), "-c", f"safe.directory={root}", "rev-parse", "HEAD"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        if rev.returncode != 0:
            raise BuildError("git rev-parse HEAD failed")
        head = rev.stdout.strip()
        if len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
            raise BuildError("HEAD is not a full 40-hex commit SHA")
        return head
    except FileNotFoundError:
        raise BuildError("git is not on PATH") from None


def bytes_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def dockerfile_digest(dockerfile: Path) -> str:
    try:
        raw = dockerfile.read_bytes()
    except OSError as error:
        raise BuildError(f"could not read {dockerfile}: {error}") from error
    return bytes_digest(raw)


def base_provenance(specification: dict) -> str:
    try:
        references = {declared: artifact["reference"] for declared, artifact in specification["images"].items()}
    except (AttributeError, KeyError, TypeError) as error:
        raise BuildError("inputs spec names no immutable base references") from error
    if any(not isinstance(ref, str) or "@sha256:" not in ref for ref in references.values()):
        raise BuildError("inputs spec names a mutable base reference")
    return json.dumps(references, sort_keys=True, separators=(",", ":"))


def spec_dockerfile_digest(specification: dict) -> str:
    digest = specification.get("dockerfile_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise BuildError("inputs spec names no Dockerfile digest")
    return digest


def authorize_bases(
    target: BuildTarget, root: Path, *, require_aged: bool, directory: Path
) -> tuple[dict, Path]:
    """Authorize upstream bases; never the local image or its layers."""
    dockerfile = root / target.dockerfile
    rendered_path = directory / "Dockerfile.pinned"
    arguments = [tool("image_update"), "inputs", "--dockerfile-stdin", "--json"]
    if require_aged:
        arguments.append("--require-aged")
    try:
        result = run_as("image-updates", arguments, input=dockerfile.read_bytes(), capture_output=True, timeout=300)
    except OSError as error:
        raise BuildError(f"could not run image-update inputs: {error}") from error
    if result.returncode != 0:
        raise BuildError(
            result.stderr.decode("utf-8", "replace").strip()
            or f"upstream {target.name} bases are not seven-day eligible; nothing was changed"
        )
    try:
        response = json.loads(result.stdout)
        specification = response["specification"]
        rendered_path.write_text(response["rendered"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise BuildError(f"could not read inputs spec: {error}") from error
    if not isinstance(specification, dict) or specification.get("schema") != 1:
        raise BuildError("inputs spec has an unsupported schema")
    if spec_dockerfile_digest(specification) != dockerfile_digest(dockerfile):
        raise BuildError("inputs spec does not match the committed Dockerfile")
    base_provenance(specification)
    if not rendered_path.is_file():
        raise BuildError("inputs rendered no pinned Dockerfile")
    return specification, rendered_path


def expected_pin_rewrite(original: str, bases: list) -> str:
    """Re-derive the proposal locally: per base only the tag and the registry name may change."""
    try:
        parsed = _parse_dockerfile(original)
        declared = {item["declared"]: item for item in parsed["unique"]}
        proposals: dict[str, str] = {}
        for base in bases:
            name, proposed = base["declared"], base["proposed"]
            if name in proposals:
                raise BuildError(f"pin proposal lists {name} twice")
            item = declared.get(name)
            if item is None:
                raise BuildError(f"pin proposal names {name}, which the Dockerfile does not declare")
            if not isinstance(proposed, str):
                raise BuildError(f"pin proposal for {name} is not a reference")
            repository, reference = _split_declared(proposed)
            qualified = qualify(item["repository"])
            if repository != qualified:
                raise BuildError(f"pin proposal retargets {name} to repository {repository}; expected {qualified}")
            if item["reference"].startswith("sha256:"):
                if proposed != f"{qualified}@{item['reference']}":
                    raise BuildError(f"pin proposal changes the digest of {name}")
            elif "@" in proposed:
                raise BuildError(f"pin proposal replaces the tag of {name} with a digest")
            proposals[name] = proposed
        missing = sorted(set(declared) - set(proposals))
        if missing:
            raise BuildError(f"pin proposal omits {', '.join(missing)}")
        lines = original.splitlines(keepends=True)
        for occurrence in {entry["physical"]: entry for entry in parsed["occurrences"]}.values():
            index, name = occurrence["physical"], occurrence["declared"]
            if proposals[name] != name:
                lines[index] = _render_line(lines[index], name, proposals[name], index + 1)
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, BuildError):
            raise
        raise BuildError(f"pin proposal is not usable: {error}") from error
    return "".join(lines)


def check_pin_rewrite(original: str, rewritten: str, bases: list) -> None:
    expected = expected_pin_rewrite(original, bases)
    if rewritten == expected:
        return
    before, after = expected.splitlines(keepends=True), rewritten.splitlines(keepends=True)
    if len(before) != len(after):
        raise BuildError(f"pin proposal changed the Dockerfile line count ({len(before)} -> {len(after)})")
    number = next(index for index, (left, right) in enumerate(zip(before, after), 1) if left != right)
    line = after[number - 1].rstrip()
    original_line = original.splitlines(keepends=True)[number - 1]
    kind = "is not a FROM line" if (original_line.split() or [""])[0].upper() != "FROM" else "changes more than the tag or registry name"
    raise BuildError(f"pin proposal changed line {number}, which {kind}: {line!r}")


def propose_pins(target: BuildTarget, root: Path) -> dict:
    """Ask the updater which exact tags the gate accepts; never writes the checkout.

    The working-tree Dockerfile is what the patch must apply to. The response is
    trusted only after its digest binds it to the bytes sent and its rewrite is
    shown to touch FROM lines alone.
    """
    dockerfile = root / target.dockerfile
    try:
        raw = dockerfile.read_bytes()
        original = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise BuildError(f"could not read {dockerfile}: {error}") from error
    try:
        result = run_as(
            "image-updates",
            [tool("image_update"), "pin", "--dockerfile-stdin", "--json"],
            input=raw,
            capture_output=True,
            timeout=300,
        )
    except OSError as error:
        raise BuildError(f"could not run image-update pin: {error}") from error
    if result.returncode != 0:
        raise BuildError(
            result.stderr.decode("utf-8", "replace").strip() or f"image-update pin failed for {target.name}"
        )
    try:
        response = json.loads(result.stdout)
        rewritten = response["rewritten"]
        valid = response["schema"] == 1 and isinstance(rewritten, str) and isinstance(response["bases"], list)
        digest = response["dockerfile_digest"]
    except (ValueError, KeyError, TypeError) as error:
        raise BuildError(f"could not read pin proposal: {error}") from error
    if not valid:
        raise BuildError("pin proposal has an unsupported schema")
    if digest != bytes_digest(raw):
        raise BuildError("pin proposal does not match the working-tree Dockerfile")
    check_pin_rewrite(original, rewritten, response["bases"])
    return dict(response, patch=unified_patch(original, rewritten, target.dockerfile))


def acquire_base(target: BuildTarget, reference: str) -> None:
    """Load an approved base from the updater's exact-registry OCI archive."""
    if "@sha256:" not in reference:
        raise BuildError(f"refusing to acquire a mutable base reference: {reference}")
    presence = podman(target.builder, "image", "exists", reference, timeout=60)
    if presence.returncode not in (0, 1):
        raise BuildError(f"{target.builder} image store is unavailable")
    if presence.returncode == 0:
        return
    result = run_as(
        "image-updates",
        [tool("image_update"), "acquire", "--signature-policy", os.environ["HOMELAB_SIGNATURE_POLICY"], reference],
        text=True,
        capture_output=True,
        timeout=1800,
    )
    if result.returncode != 0:
        raise BuildError(f"could not acquire approved base {reference}:\n{result.stderr[-4000:]}")
    stream_image("image-updates", target.builder, reference)
    if podman(target.builder, "image", "exists", reference, timeout=60).returncode:
        raise BuildError("approved base OCI load did not retain its immutable reference")


def build_image(
    target: BuildTarget,
    root: Path,
    tag: str,
    rev: str,
    specification: dict,
    rendered: Path,
    *,
    committed: bool = True,
) -> str:
    """Build clean committed code (or, if allowed, the working tree) from a pinned Dockerfile."""
    provenance = base_provenance(specification)
    digest = spec_dockerfile_digest(specification)
    for reference in sorted({artifact["reference"] for artifact in specification["images"].values()}):
        acquire_base(target, reference)
    identity = principal(target.builder)
    with tempfile.TemporaryDirectory(prefix="build-context-", dir=identity["home"]) as temporary:
        context = Path(temporary) / "context"
        if committed:
            context.mkdir(mode=0o700)
            with tempfile.TemporaryFile() as errors:
                archive = subprocess.Popen(
                    [tool("git"), "-c", f"safe.directory={root}", "archive", "--format=tar", rev],
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                )
                try:
                    with tarfile.open(fileobj=archive.stdout, mode="r|") as files:
                        files.extractall(context, filter="data")
                    archive.stdout.close()
                    if archive.wait(timeout=120):
                        raise BuildError(f"could not export the committed {target.name} source")
                finally:
                    if archive.poll() is None:
                        archive.kill()
                        archive.wait()
                    archive.stdout.close()
        else:
            shutil.copytree(root, context, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        if dockerfile_digest(context / target.dockerfile) != digest:
            raise BuildError("build context Dockerfile differs from its authorized input")
        pinned = context / ".homelab.Dockerfile"
        pinned.write_bytes(rendered.read_bytes())
        for directory, dirs, files in os.walk(temporary, followlinks=False):
            os.chown(directory, identity["uid"], identity["gid"])
            os.chmod(directory, 0o700)
            for name in dirs + files:
                path = Path(directory) / name
                os.chown(path, identity["uid"], identity["gid"], follow_symlinks=False)
                if not path.is_symlink():
                    # Keep the committed permission bits: build tools copy them
                    # into the image, so squeezing a 0755 entrypoint to 0700
                    # would leave it unreadable to a non-root container user.
                    # The 0700 context directory still blocks other accounts.
                    path.chmod((path.stat().st_mode & 0o777) | 0o600 if path.is_file() else 0o700)
        arguments = [
            "build",
            "--pull=never",
            "-f",
            str(pinned),
            "--label",
            f"{REVISION_LABEL}={rev}",
            "--label",
            f"{target.base_images_label}={provenance}",
            "--label",
            f"{target.dockerfile_digest_label}={digest}",
        ]
        if target.revision_build_arg is not None:
            arguments += ["--build-arg", f"{target.revision_build_arg}={rev}"]
        result = podman(
            target.builder, *arguments, "-t", target.reference(tag), str(context), timeout=target.build_timeout
        )
    if result.returncode != 0:
        raise BuildError(f"podman build :{tag} failed:\n{result.stderr[-4000:]}")
    built = image_identity(target.builder, target.reference(tag))
    print(f"Built {target.reference(tag)} from {rev[:12]} on selected immutable bases.")
    return built


def candidate_image(target: BuildTarget, root: Path, rev: str, specification: dict, rendered: Path) -> str:
    """Reuse only a cache proving the same revision, bases, and Dockerfile."""
    reference = target.reference(rev)
    cached = image_exists(target.builder, reference)
    labels = inspect_labels(target.builder, reference) if cached else None
    if (
        labels is not None
        and labels.get(REVISION_LABEL) == rev
        and labels.get(target.base_images_label) == base_provenance(specification)
        and labels.get(target.dockerfile_digest_label) == spec_dockerfile_digest(specification)
    ):
        return image_identity(target.builder, reference)
    if cached:
        print("Cached candidate predates the approved bases or Dockerfile; rebuilding.")
    return build_image(target, root, rev, rev, specification, rendered)


def transfer_image(target: BuildTarget, identity: str) -> None:
    """Move an exact image ID from the builder to the owner store, labels intact."""
    transferred = stream_image(target.builder, target.owner, identity)
    if transferred != identity:
        raise BuildError(f"authorized {target.name} image changed before transfer to its owner")
    owner_labels = podman(
        target.owner, "image", "inspect", "--format", "{{json .Labels}}", identity, timeout=30
    )
    if owner_labels.returncode or json.loads(owner_labels.stdout) != inspect_labels(target.builder, identity):
        raise BuildError(f"{target.name} image transfer did not preserve provenance labels")
