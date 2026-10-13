import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from image_age import MINIMUM_AGE

spec = importlib.util.spec_from_file_location("image_scan_updater", Path(__file__).with_name("image-update.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)
NOW = 10 * MINIMUM_AGE
REPOSITORY = "docker.io/example/service"


def setUpModule():
    tools = patch.dict(os.environ, {"HOMELAB_GIT": shutil.which("git")})
    tools.start()
    unittest.addModuleCleanup(tools.stop)


def digest(number):
    return "sha256:" + f"{number:064x}"


def image(number, published_at):
    artifact = {
        "reference": REPOSITORY + "@" + digest(number),
        "manifest": digest(number), "config": digest(number + 100),
        "layers": [digest(number + 200)], "created_at": None,
    }
    if published_at is not None:
        artifact["publication"] = {
            "source": "docker-hub", "repository": REPOSITORY,
            "manifest": artifact["manifest"], "config": artifact["config"],
            "layers": list(artifact["layers"]), "published_at": published_at,
        }
    return artifact


class Catalog:
    def __init__(self, images, revalidated=None):
        self.images = images
        self.revalidated = revalidated or {}

    def tags(self, repository):
        return list(self.images)

    def inspect(self, repository, reference):
        if reference in self.images:
            return self.images[reference]
        if reference in self.revalidated:
            return self.revalidated[reference]
        return next(value for value in self.images.values() if value["manifest"] == reference)


class PublicationScanTests(unittest.TestCase):
    def run_scan(self, catalog, baseline="1.0.0", pattern=r"(?P<version>1\.\d+\.\d+)"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        policy = {"schema": 1, "images": {"app": {
            "repository": REPOSITORY, "tag_pattern": pattern,
        }}}
        seed = root / "seed.json"
        seed.write_text(json.dumps({"schema": 1, "images": {"app": {
            "reference": REPOSITORY + "@" + digest(50),
            "version": baseline, "order": [int(value) for value in baseline.split(".")],
        }}}))
        with patch("image_age.time.time", return_value=NOW):
            result = updater.scan(root / "feed", seed, policy, root / "state",
                                  registry_factory=lambda: catalog)
        pins = json.loads((root / "feed" / "pins.json").read_text())
        state = json.loads((root / "state" / "observations.json").read_text())
        return result, pins["images"]["app"], state

    def test_first_scan_selects_aged_release_below_young_latest(self):
        result, pin, state = self.run_scan(Catalog({
            "1.3.0": image(3, NOW - MINIMUM_AGE + 1),
            "1.2.0": image(2, NOW - MINIMUM_AGE),
            "1.1.0": image(1, NOW - 2 * MINIMUM_AGE),
        }))
        self.assertEqual(result, 0)
        self.assertEqual(pin["reference"], REPOSITORY + "@" + digest(2))
        self.assertEqual(pin["version"], "1.2.0")
        self.assertEqual(set(state["content"].values()), {NOW})

    def test_missing_publication_keeps_observation_fallback(self):
        result, pin, _ = self.run_scan(Catalog({"1.2.0": image(2, None)}))
        self.assertEqual(result, 0)
        self.assertEqual(pin["version"], "1.0.0")

    def test_new_publication_at_promotion_preserves_existing_pin(self):
        result, pin, _ = self.run_scan(Catalog(
            {"1.1.0": image(1, NOW - MINIMUM_AGE)},
            revalidated={digest(1): image(1, NOW)},
        ))
        self.assertEqual(result, 1)
        self.assertEqual(pin["version"], "1.0.0")

    def test_older_aged_build_can_win_over_young_same_version_build(self):
        result, pin, _ = self.run_scan(Catalog({
            "1.1.0-ls3": image(3, NOW - 1),
            "1.1.0-ls2": image(2, NOW - MINIMUM_AGE),
            "1.1.0-ls1": image(1, NOW - 2 * MINIMUM_AGE),
        }), pattern=r"(?P<version>1\.\d+\.\d+)-ls(?P<build>\d+)")
        self.assertEqual(result, 0)
        self.assertEqual(pin["reference"], REPOSITORY + "@" + digest(2))
        self.assertEqual(pin["order"], [1, 1, 0, 2])


FRESH = [None, None, None, None]
VERSIONS = ["3.2.2", "3.2.3", "3.2.4", "3.3.3"]


def family_image(key, version, number, published_at):
    repository = f"docker.io/example/immich-{key}"
    manifest = digest(number)
    artifact = {
        "reference": repository + "@" + manifest,
        "manifest": manifest, "config": digest(number + 100),
        "layers": [digest(number + 200)], "created_at": None,
    }
    if published_at is not None:
        artifact["publication"] = {
            "source": "docker-hub", "repository": repository,
            "manifest": manifest, "config": artifact["config"],
            "layers": list(artifact["layers"]), "published_at": published_at,
        }
    return artifact


class FamilyCatalog:
    def __init__(self, key, tags, metadata, sink=None):
        self.key = key
        self.catalog = tags
        self.metadata = metadata
        self.sink = sink
        self.inspected = []

    def release_tags(self, repository):
        return list(self.catalog)

    def inspect(self, repository, reference):
        self.inspected.append((self.key, reference))
        if self.sink is not None:
            self.sink.append((self.key, reference))
        if reference in self.metadata:
            return self.metadata[reference]
        return next(value for value in self.metadata.values() if value["manifest"] == reference)


class FamilyRegistryFactory:
    def __init__(self, catalogs):
        self.catalogs = catalogs
        self.order = list(catalogs)
        self.calls = 0

    def __call__(self):
        catalog = self.catalogs[self.order[self.calls % len(self.order)]]
        self.calls += 1
        return catalog

class OlderReleaseDiscoveryTests(unittest.TestCase):
    members = ("server", "ml")

    def policy_for(self):
        return {"schema": 1, "images": {key: {
            "group": "immich", "repository": f"docker.io/example/immich-{key}",
            "release_repository": "example/immich", "tag_pattern": r"v(?P<version>3\.\d+\.\d+)",
        } for key in self.members}}

    def catalogs_for(self, metadata, sink=None, catalog=None):
        catalogs = {}
        number = [0]

        def fresh(key):
            images = {}
            for version, published_at in zip(VERSIONS, metadata):
                number[0] += 1
                images["v" + version] = family_image(key, version, number[0], published_at)
            tags = list(catalog) if catalog is not None else ["v" + version for version in VERSIONS]
            return FamilyCatalog(key, tags, images, sink)

        for key in self.members:
            catalogs[key] = fresh(key)
        return catalogs

    def seed_for(self, root, baseline="3.2.2", unreleased=False):
        seed = root / "seed.json"
        pins = {}
        for index, key in enumerate(self.members):
            pin = {
                "reference": f"docker.io/example/immich-{key}@" + digest(900 + index),
                "version": baseline, "order": [int(value) for value in baseline.split(".")],
            }
            if unreleased:
                pin["unreleased_baseline"] = True
            pins[key] = pin
        seed.write_text(json.dumps({"schema": 1, "images": pins}))
        return seed

    def run_family_scan(self, metadata, baseline="3.2.2", sink=None, unreleased=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        policy = self.policy_for()
        catalogs = self.catalogs_for(metadata, sink)
        seed = self.seed_for(root, baseline, unreleased)
        with patch("image_age.time.time", return_value=NOW):
            result = updater.scan(root / "feed", seed, policy, root / "state",
                                  registry_factory=FamilyRegistryFactory(catalogs))
        pins = json.loads((root / "feed" / "pins.json").read_text())["images"]
        return result, pins, catalogs

    def test_fresh_scan_honestly_observes_history_without_instant_approval(self):
        sink = []
        result, pins, _ = self.run_family_scan(list(FRESH), sink=sink)
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.2.2"})
        tags = {reference for _, reference in sink if not reference.startswith("sha256:")}
        self.assertTrue({"v3.3.3", "v3.2.4", "v3.2.3"} <= tags)

    def test_aged_middle_release_wins_over_young_latest(self):
        metadata = [None, None, NOW - MINIMUM_AGE, NOW - 1]
        result, pins, catalogs = self.run_family_scan(metadata)
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.2.4"})
        for key, pin in pins.items():
            expected = catalogs[key].metadata["v3.2.4"]["reference"]
            self.assertEqual(pin["reference"], expected)

    def test_older_metadata_only_scan_discovers_below_unknown_latest(self):
        metadata = [None, None, NOW - MINIMUM_AGE, None]
        sink = []
        result, pins, _ = self.run_family_scan(metadata, sink=sink)
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.2.4"})
        tags = [reference for _, reference in sink if not reference.startswith("sha256:")]
        self.assertIn("v3.3.3", tags)
        self.assertIn("v3.2.4", tags)
        self.assertEqual(len(set(sink)), len(sink))

    def test_supported_metadata_prefers_newest_aged_release(self):
        metadata = [NOW - 4 * MINIMUM_AGE, NOW - 3 * MINIMUM_AGE, NOW - MINIMUM_AGE, NOW - 1]
        result, pins, _ = self.run_family_scan(metadata)
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.2.4"})

    def test_mixed_group_ages_do_not_partially_upgrade(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        policy = self.policy_for()
        catalogs = self.catalogs_for([NOW - 3 * MINIMUM_AGE] * 4)
        catalogs["ml"].metadata["v3.2.4"]["publication"]["published_at"] = NOW - 1
        catalogs["ml"].metadata["v3.3.3"]["publication"]["published_at"] = NOW - 1
        catalogs["server"].metadata["v3.3.3"]["publication"]["published_at"] = NOW - 1
        seed = self.seed_for(root)
        with patch("image_age.time.time", return_value=NOW):
            result = updater.scan(root / "feed", seed, policy, root / "state",
                                  registry_factory=FamilyRegistryFactory(catalogs))
        pins = json.loads((root / "feed" / "pins.json").read_text())["images"]
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.2.3"})

    def test_current_floor_and_no_downgrade_hold(self):
        metadata = [NOW - 4 * MINIMUM_AGE, NOW - 3 * MINIMUM_AGE, NOW - 2 * MINIMUM_AGE, NOW - MINIMUM_AGE]
        result, pins, _ = self.run_family_scan(metadata, baseline="3.2.4")
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.3.3"})
        aged_only_below = [NOW - 4 * MINIMUM_AGE, NOW - 3 * MINIMUM_AGE, None, None]
        result, pins, _ = self.run_family_scan(aged_only_below, baseline="3.2.4")
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.2.4"})
    def test_unreleased_baseline_requires_strictly_newer_release(self):
        metadata = [NOW - 4 * MINIMUM_AGE, NOW - 3 * MINIMUM_AGE, NOW - 2 * MINIMUM_AGE, NOW - 1]
        result, pins, _ = self.run_family_scan(metadata, baseline="3.2.4", unreleased=True)
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.2.4"})

    def test_withdrawn_release_is_not_promoted(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        policy = self.policy_for()
        catalogs = self.catalogs_for(list(FRESH))
        seed = self.seed_for(root)
        with patch("image_age.time.time", return_value=NOW - MINIMUM_AGE):
            updater.scan(root / "feed", seed, policy, root / "state",
                         registry_factory=FamilyRegistryFactory(catalogs))
        for catalog in catalogs.values():
            catalog.catalog.remove("v3.3.3")
            del catalog.metadata["v3.3.3"]
        with patch("image_age.time.time", return_value=NOW):
            result = updater.scan(root / "feed", seed, policy, root / "state",
                                  registry_factory=FamilyRegistryFactory(catalogs))
        pins = json.loads((root / "feed" / "pins.json").read_text())["images"]
        self.assertEqual(result, 0)
        self.assertEqual({pin["version"] for pin in pins.values()}, {"3.2.4"})

    def test_uneven_builds_inspect_each_member_tag_once(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        members = ("server", "ml")
        policy = {"schema": 1, "images": {key: {
            "group": "immich", "repository": f"docker.io/example/immich-{key}",
            "release_repository": "example/immich",
            "tag_pattern": r"v(?P<version>3\.\d+\.\d+)(?:-ls(?P<build>\d+))?",
        } for key in members}}
        clock = NOW - MINIMUM_AGE - 1
        first_catalogs = {}
        number = 0
        layouts = {"server": ["v3.3.3-ls2", "v3.3.3-ls1"], "ml": ["v3.3.3-ls1"]}
        for key in members:
            images = {}
            for tag in layouts[key]:
                number += 1
                repository = f"docker.io/example/immich-{key}"
                manifest = digest(number)
                images[tag] = {
                    "reference": repository + "@" + manifest,
                    "manifest": manifest, "config": digest(number + 100),
                    "layers": [digest(number + 200)], "created_at": None,
                }
            first_catalogs[key] = FamilyCatalog(key, list(layouts[key]), images)
        seed = root / "seed.json"
        seed.write_text(json.dumps({"schema": 1, "images": {key: {
            "reference": f"docker.io/example/immich-{key}@" + digest(900 + index),
            "version": "3.3.3", "order": [3, 3, 3, 1],
        } for index, key in enumerate(members)}}))
        with patch("image_age.time.time", return_value=clock):
            updater.scan(root / "feed", seed, policy, root / "state",
                         registry_factory=FamilyRegistryFactory(first_catalogs))
        sink = []
        catalogs = {}
        for key, catalog in first_catalogs.items():
            catalogs[key] = FamilyCatalog(key, catalog.catalog, catalog.metadata, sink)
        with patch("image_age.time.time", return_value=clock + MINIMUM_AGE + 1):
            result = updater.scan(root / "feed", seed, policy, root / "state",
                                  registry_factory=FamilyRegistryFactory(catalogs))
        pins = json.loads((root / "feed" / "pins.json").read_text())["images"]
        self.assertEqual(result, 0)
        self.assertEqual(pins["server"]["order"], [3, 3, 3, 2])
        self.assertEqual(pins["ml"]["order"], [3, 3, 3, 1])
        tags = [reference for _, reference in sink if not reference.startswith("sha256:")]
        self.assertEqual(sorted(tags), ["v3.3.3-ls1", "v3.3.3-ls1", "v3.3.3-ls2"])
        self.assertEqual(len(set(sink)), len(sink))


if __name__ == "__main__":
    unittest.main()
