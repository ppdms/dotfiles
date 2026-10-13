import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from image_age import AgeStore, MINIMUM_AGE
from image_registry import RegistryError
from tree_image_inputs import TreeInputsError, collect_inputs, render_dockerfile

BUN = "docker.io/oven/bun:1.4.2-slim"
GO = "docker.io/library/golang:1.27.1"
PYTHON = "docker.io/library/python:3.14.7-slim-trixie"


def digest(number):
    return "sha256:" + f"{number:064x}"


def artifact(repository, number, created_at=None):
    manifest = digest(number)
    return {
        "reference": f"{repository}@{manifest}",
        "manifest": manifest,
        "config": digest(number + 100_000),
        "layers": [digest(number + 200_000)],
        "created_at": created_at,
    }


def repository_of(declared):
    head, _, _ = declared.rpartition(":")
    if head and "/" in declared and declared.rfind(":") > declared.rfind("/"):
        return head
    return declared


class ScriptedRegistry:
    """Fixture OCI transport: fixed latest artifacts plus digest revalidation."""

    def __init__(self, latest, extra=None, failures=()):
        # latest: {(repository, tag): artifact}; extra: {manifest: artifact}
        self.latest = dict(latest)
        self.extra = dict(extra or {})
        self.failures = set(failures)

    def inspect(self, repository, reference):
        if (repository, reference) in self.failures or reference in self.failures:
            raise RegistryError(f"{repository} {reference}: unavailable", status=404)
        key = (repository, reference)
        if key in self.latest:
            return dict(self.latest[key], layers=list(self.latest[key]["layers"]))
        pool = {value["manifest"]: value for value in list(self.latest.values()) + list(self.extra.values())}
        if reference in pool:
            found = pool[reference]
            if found["reference"] != f"{repository}@{reference}":
                raise RegistryError(f"{repository} {reference}: mismatched repository")
            return dict(found, layers=list(found["layers"]))
        raise RegistryError(f"{repository} {reference}: unknown test reference")


def write_dockerfile(directory, text):
    path = Path(directory) / "Dockerfile"
    path.write_text(text)
    return path


class TreeInputsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store_path = self.root / "observations.json"
        self.dockerfile = write_dockerfile(self.root, (
            f"FROM {BUN} AS browser-build\n"
            f"FROM {GO} AS go-build\n"
            f"FROM {PYTHON} AS pdf-build\n"
            f"FROM {PYTHON} AS api\n"
            "COPY --from=browser-build /dist /app\n"
            "COPY --from=go-build /binary /binary\n"
            "COPY --from=pdf-build /helper /helper\n"
        ))

    def store(self, now):
        return AgeStore(self.store_path, now=now)

    def tree_registry(self, bun=1, go=2, python=3):
        return ScriptedRegistry({
            (repository_of(BUN), "1.4.2-slim"): artifact(repository_of(BUN), bun),
            (repository_of(GO), "1.27.1"): artifact(repository_of(GO), go),
            (repository_of(PYTHON), "3.14.7-slim-trixie"): artifact(repository_of(PYTHON), python),
        })

    def test_old_published_inputs_qualify_immediately_without_backdating_observation(self):
        registry = self.tree_registry()
        now = 10 * MINIMUM_AGE
        for image in registry.latest.values():
            image["publication"] = {
                "source": "docker-hub",
                "repository": image["reference"].split("@", 1)[0],
                "manifest": image["manifest"], "config": image["config"],
                "layers": list(image["layers"]), "published_at": now - MINIMUM_AGE,
            }
        selected = collect_inputs(self.dockerfile, self.store(now),
                                  require_aged=True, registry=registry)
        self.assertEqual(selected["images"][PYTHON]["manifest"], digest(3))
        self.assertEqual(set(self.store(now).data["content"].values()), {now})

    def test_new_publication_during_revalidation_blocks_previously_qualified_input(self):
        registry = self.tree_registry()
        now = 10 * MINIMUM_AGE
        for image in registry.latest.values():
            image["publication"] = {
                "source": "docker-hub",
                "repository": image["reference"].split("@", 1)[0],
                "manifest": image["manifest"], "config": image["config"],
                "layers": list(image["layers"]), "published_at": now - MINIMUM_AGE,
            }
        original_inspect = registry.inspect

        def inspect(repository, reference):
            image = original_inspect(repository, reference)
            if reference == digest(3):
                image["publication"] = dict(image["publication"], published_at=now)
            return image

        registry.inspect = inspect
        with self.assertRaisesRegex(TreeInputsError, "quarantined"):
            collect_inputs(self.dockerfile, self.store(now),
                           require_aged=True, registry=registry)

    def test_new_build_stage_image_blocks_even_with_aged_runtime(self):
        registry = self.tree_registry()
        collect_inputs(self.dockerfile, self.store(now=100), registry=registry)
        self.dockerfile.write_text(self.dockerfile.read_text().replace(GO, repository_of(GO) + ":1.27.2"))
        registry.latest[(repository_of(GO), "1.27.2")] = artifact(repository_of(GO), 4)
        with self.assertRaisesRegex(TreeInputsError, "golang:1.27.2"):
            collect_inputs(self.dockerfile, self.store(now=100 + MINIMUM_AGE),
                           require_aged=True, registry=registry)

    def test_one_young_input_blocks_until_exact_age_boundary(self):
        registry = self.tree_registry()
        early = self.store(now=100)
        early.observe(registry.latest[(repository_of(BUN), "1.4.2-slim")])
        early.observe(registry.latest[(repository_of(GO), "1.27.1")])
        early.save()
        collect_inputs(self.dockerfile, self.store(now=1000), registry=registry)
        with self.assertRaisesRegex(TreeInputsError, "python:3.14.7-slim-trixie"):
            collect_inputs(self.dockerfile, self.store(now=1000 + MINIMUM_AGE - 1),
                           require_aged=True, registry=registry)
        approved = collect_inputs(self.dockerfile, self.store(now=1000 + MINIMUM_AGE),
                                  require_aged=True, registry=registry)
        self.assertEqual(approved["images"][PYTHON]["manifest"], digest(3))

    def test_same_tag_young_rebuild_falls_back_then_advances_after_aging(self):
        original = self.tree_registry(python=11)
        old_python = original.latest[(repository_of(PYTHON), "3.14.7-slim-trixie")]
        collect_inputs(self.dockerfile, self.store(now=200_000), registry=original)
        registry = self.tree_registry(python=12)
        registry.extra[old_python["manifest"]] = old_python
        current = self.store(now=200_000 + MINIMUM_AGE)
        selected = collect_inputs(self.dockerfile, current, require_aged=True, registry=registry)
        self.assertEqual(selected["images"][PYTHON]["manifest"], digest(11))
        next_store = self.store(now=current.now + MINIMUM_AGE)
        selected = collect_inputs(self.dockerfile, next_store, require_aged=True, registry=registry)
        self.assertEqual(selected["images"][PYTHON]["manifest"], digest(12))

    def test_explicit_changed_version_never_falls_back_to_previous_tag(self):
        old_reference = repository_of(PYTHON) + ":3.13-slim"
        path = write_dockerfile(self.root, f"FROM {old_reference} AS api\n")
        original = ScriptedRegistry({
            (repository_of(PYTHON), "3.13-slim"): artifact(repository_of(PYTHON), 21),
        })
        collect_inputs(path, self.store(now=300_000), registry=original)
        path.write_text(f"FROM {repository_of(PYTHON)}:3.14-slim AS api\n")
        registry = ScriptedRegistry({
            (repository_of(PYTHON), "3.14-slim"): artifact(repository_of(PYTHON), 22),
        })
        with self.assertRaisesRegex(TreeInputsError, "no aged artifact for this source"):
            collect_inputs(path, self.store(now=300_000 + MINIMUM_AGE),
                           require_aged=True, registry=registry)

    def test_missing_layer_preserves_other_inputs_but_blocks_production(self):
        registry = self.tree_registry()
        registry.failures.add((repository_of(GO), "1.27.1"))
        with self.assertRaisesRegex(RegistryError, "Tree base .*golang"):
            collect_inputs(self.dockerfile, self.store(now=400_000), registry=registry)
        self.assertIn(BUN, self.store(now=400_000).data["tree_inputs"])
        self.assertNotIn(GO, self.store(now=400_000).data["tree_inputs"])
        with self.assertRaises(RegistryError):
            collect_inputs(self.dockerfile, self.store(now=400_000 + MINIMUM_AGE),
                           require_aged=True, registry=registry)

    def test_registry_unavailable_during_revalidation_fails_closed(self):
        registry = self.tree_registry()
        collect_inputs(self.dockerfile, self.store(now=500_000), registry=registry)
        registry.failures.add(digest(1))
        with self.assertRaisesRegex(RegistryError, "revalidation failed"):
            collect_inputs(self.dockerfile, self.store(now=500_000 + MINIMUM_AGE),
                           require_aged=True, registry=registry)

    def test_unresolved_from_is_rejected(self):
        for text in (
            "ARG TAG=1.4.2-slim\nFROM docker.io/oven/bun:${TAG} AS build\n",
            "FROM $BASE AS build\n",
            "FROM docker.io/oven/bun:1.4.2-slim AS $STAGE\n",
            "FROM --platform=$ARCH docker.io/oven/bun:1.4.2-slim\n",
        ):
            path = write_dockerfile(self.root, text)
            with self.assertRaisesRegex(TreeInputsError, "unresolved|unsupported"):
                collect_inputs(path, self.store(now=600_000), registry=self.tree_registry())

    def test_implicit_external_copy_or_mount_image_is_rejected(self):
        for text in (
            f"FROM {BUN} AS build\nCOPY --from=docker.io/library/busybox:1.36 /bin /x\n",
            f"FROM {BUN} AS build\nCOPY --from=missing-stage /bin /x\n",
            f"FROM {BUN} AS build\nCOPY --from=7 /bin /x\n",
            f"FROM {BUN} AS build\nRUN --mount=type=cache,from=docker.io/library/busybox:1.36,target=/x echo hi\n",
        ):
            path = write_dockerfile(self.root, text)
            with self.assertRaisesRegex(TreeInputsError, "declare the external image|unknown stage"):
                collect_inputs(path, self.store(now=610_000), registry=self.tree_registry())

    def test_changed_dockerfile_cannot_use_previous_authorization(self):
        spec = collect_inputs(self.dockerfile, self.store(now=700_000), registry=self.tree_registry())
        self.dockerfile.write_text(self.dockerfile.read_text() + f"FROM {BUN} AS extra\n")
        with self.assertRaisesRegex(TreeInputsError, "changed since observation"):
            render_dockerfile(self.dockerfile, spec)

    def test_tampered_or_missing_base_provenance_is_rejected(self):
        spec = collect_inputs(self.dockerfile, self.store(now=710_000), registry=self.tree_registry())
        tampered = dict(spec, images=dict(spec["images"]))
        tampered["images"][BUN] = dict(spec["images"][BUN])
        tampered["images"][BUN]["reference"] = repository_of(GO) + "@" + digest(1)
        with self.assertRaisesRegex(TreeInputsError, "provenance mismatch"):
            render_dockerfile(self.dockerfile, tampered)
        dropped = dict(spec, images={key: value for key, value in spec["images"].items() if key != GO})
        with self.assertRaisesRegex(TreeInputsError, "provenance mismatch"):
            render_dockerfile(self.dockerfile, dropped)

    def test_old_builder_timestamp_cannot_grant_tree_age(self):
        ancient = artifact(repository_of(BUN), 51, created_at=1)
        registry = ScriptedRegistry({(repository_of(BUN), "1.4.2-slim"): ancient})
        path = write_dockerfile(self.root, f"FROM {BUN} AS build\n")
        collect_inputs(path, self.store(now=900_000), registry=registry)
        with self.assertRaisesRegex(TreeInputsError, "production blocked"):
            collect_inputs(path, self.store(now=900_000), require_aged=True, registry=registry)


if __name__ == "__main__":
    unittest.main()
