import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
from image_age import AgeError, AgeStore, MINIMUM_AGE

spec = importlib.util.spec_from_file_location("image_update", Path(__file__).with_name("image-update.py"))
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def digest(number):
    return "sha256:" + f"{number:064x}"


def artifact(number, created_at=None):
    return {
        "reference": "registry.example/app@" + digest(number),
        "manifest": digest(number), "config": digest(number + 100),
        "layers": [digest(number + 200)], "created_at": created_at,
    }

def registered(number, published_at, repository="docker.io/example/app", created_at=None, source="docker-hub"):
    manifest = digest(number)
    image = {
        "reference": f"{repository}@{manifest}",
        "manifest": manifest, "config": digest(number + 100),
        "layers": [digest(number + 200)], "created_at": created_at,
    }
    if published_at is not None:
        image["publication"] = {
            "source": source, "repository": repository,
            "manifest": image["manifest"], "config": image["config"],
            "layers": list(image["layers"]), "published_at": published_at,
        }
    return image


def mirrored(number, published_at, created_at=None):
    manifest = digest(number)
    image = {
        "reference": f"lscr.io/linuxserver/app@{manifest}",
        "manifest": manifest, "config": digest(number + 100),
        "layers": [digest(number + 200)], "created_at": created_at,
    }
    if published_at is not None:
        image["publication"] = {
            "source": "docker-hub-mirror", "repository": "docker.io/linuxserver/app",
            "manifest": image["manifest"], "config": image["config"],
            "layers": list(image["layers"]), "published_at": published_at,
        }
    return image


class AgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "observations.json"

    def test_old_builder_timestamp_cannot_bypass_observed_quarantine(self):
        store = AgeStore(self.path, now=10 * MINIMUM_AGE)
        image = artifact(1, created_at=1)
        store.observe(image)
        store.now += MINIMUM_AGE - 1
        self.assertFalse(store.eligible(image))
        store.now += 1
        self.assertTrue(store.eligible(image))

    def test_new_manifest_reusing_old_layers_still_waits_seven_days(self):
        store = AgeStore(self.path, now=100)
        old = artifact(1)
        store.observe(old)
        store.now += MINIMUM_AGE
        new = dict(old, manifest=digest(2), reference="registry.example/app@" + digest(2))
        store.observe(new)
        self.assertTrue(store.eligible(old))
        self.assertFalse(store.eligible(new))
        self.assertEqual(store.eligible_at(new), store.now + MINIMUM_AGE)

    def test_unobserved_layer_blocks_approval(self):
        store = AgeStore(self.path, now=100)
        image = artifact(1)
        store.observe(image)
        store.now += MINIMUM_AGE
        del store.data["content"][image["layers"][0]]
        self.assertFalse(store.eligible(image))
        self.assertIsNone(store.eligible_at(image))

    def test_future_declared_timestamp_can_delay_but_not_accelerate_approval(self):
        store = AgeStore(self.path, now=100)
        image = artifact(1, created_at=100 + MINIMUM_AGE)
        store.observe(image)
        store.now += MINIMUM_AGE
        self.assertFalse(store.eligible(image))
        store.now += MINIMUM_AGE
        self.assertTrue(store.eligible(image))

    def test_repeated_observation_does_not_restart_quarantine(self):
        store = AgeStore(self.path, now=100)
        image = artifact(1)
        store.observe(image)
        store.now += MINIMUM_AGE
        store.observe(image)
        store.save()
        self.assertTrue(AgeStore(self.path, now=store.now).eligible(image))

    def test_clock_rollback_fails_closed(self):
        store = AgeStore(self.path, now=100)
        store.observe(artifact(1))
        store.save()
        with self.assertRaises(AgeError):
            AgeStore(self.path, now=99)

    def test_live_observation_starts_hold_after_verification_not_scan_start(self):
        with patch("image_age.time.time", return_value=100.0):
            store = AgeStore(self.path)
        with patch("image_age.time.time", return_value=105.75):
            store.observe(artifact(1))
        store.now = 100 + MINIMUM_AGE
        self.assertFalse(store.eligible(artifact(1)))
        store.now = 105.74 + MINIMUM_AGE
        self.assertFalse(store.eligible(artifact(1)))
        store.now = 105.75 + MINIMUM_AGE
        self.assertTrue(store.eligible(artifact(1)))

    def test_old_publication_qualifies_at_exact_boundary_without_backdating_observation(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = registered(1, now - MINIMUM_AGE)
        store.observe(image)
        self.assertTrue(store.eligible(image))
        self.assertEqual(store.eligible_at(image), now)
        self.assertEqual(store.evidence_source(image), "docker-hub")
        self.assertEqual(set(store.data["content"].values()), {now})
        young = registered(1, now - MINIMUM_AGE + 1)
        self.assertFalse(store.eligible(young))
        self.assertEqual(store.eligible_at(young), now + 1)

    def test_young_publication_blocks_despite_aged_observation(self):
        store = AgeStore(self.path, now=100)
        aged = registered(1, None)
        store.observe(aged)
        store.now += 2 * MINIMUM_AGE
        self.assertTrue(store.eligible(aged))
        young = registered(1, store.now)
        store.observe(young)
        self.assertFalse(store.eligible(young))
        self.assertEqual(store.eligible_at(young), store.now + MINIMUM_AGE)
        self.assertEqual(store.evidence_source(young), "docker-hub")
        self.assertEqual(store.data["content"][young["manifest"]], 100)

    def test_newer_publication_supersedes_older_stored_proof(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        store.observe(registered(1, now - 2 * MINIMUM_AGE))
        self.assertTrue(store.eligible(registered(1, now - 2 * MINIMUM_AGE)))
        store.observe(registered(1, now))
        revalidated = registered(1, now)
        self.assertFalse(store.eligible(revalidated))
        self.assertEqual(store.evidence_source(revalidated), "docker-hub")
        stale = registered(1, now - 2 * MINIMUM_AGE)
        self.assertFalse(store.eligible(stale))
        self.assertEqual(store.eligible_at(stale), now + MINIMUM_AGE)

    def test_stored_publication_survives_reload_and_absent_revalidation(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        store.observe(registered(1, now - MINIMUM_AGE))
        store.save()
        revived = AgeStore(self.path, now=now)
        bare = registered(1, None)
        self.assertTrue(revived.eligible(bare))
        self.assertEqual(revived.eligible_at(bare), now)
        self.assertEqual(revived.evidence_source(bare), "docker-hub")
        revived.observe(bare)
        self.assertTrue(revived.eligible(bare))
        self.assertEqual(revived.evidence_source(bare), "docker-hub")

    def test_changed_image_cannot_inherit_publication_proof(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        store.observe(registered(1, now - MINIMUM_AGE))
        rebuilt = registered(1, None, created_at=None)
        rebuilt["config"] = digest(999)
        rebuilt["layers"] = [digest(998)]
        store.observe(rebuilt)
        self.assertFalse(store.eligible(rebuilt))
        self.assertEqual(store.evidence_source(rebuilt), "observation")
        original = dict(registered(1, now - MINIMUM_AGE))
        self.assertTrue(store.eligible(original))

    def test_injected_layer_cannot_inherit_publication_proof(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = registered(1, now - MINIMUM_AGE)
        store.observe(image)
        tampered = registered(1, None)
        tampered["layers"] = tampered["layers"] + [digest(999)]
        store.observe(tampered)
        self.assertFalse(store.eligible(tampered))
        self.assertEqual(store.evidence_source(tampered), "observation")
        self.assertTrue(store.eligible(registered(1, now - MINIMUM_AGE)))

    def test_unobserved_artifact_with_publication_still_requires_availability(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = registered(1, now - MINIMUM_AGE)
        self.assertIsNone(store.eligible_at(image))
        self.assertFalse(store.eligible(image))
        store.observe(image)
        self.assertTrue(store.eligible(image))

    def test_future_publication_remains_quarantined(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = registered(1, now + 100)
        store.observe(image)
        self.assertFalse(store.eligible(image))
        self.assertEqual(store.eligible_at(image), now + 100 + MINIMUM_AGE)
        self.assertEqual(store.evidence_source(image), "docker-hub")
        store.now += 100 + MINIMUM_AGE
        self.assertTrue(store.eligible(image))

    def test_publication_without_builder_date_shortcut(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        fresh = registered(1, now, created_at=1)
        store.observe(fresh)
        self.assertFalse(store.eligible(fresh))
        self.assertEqual(store.eligible_at(fresh), now + MINIMUM_AGE)
        self.assertEqual(store.evidence_source(fresh), "docker-hub")

    def test_builder_created_cannot_override_trusted_publication(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = registered(1, now - 100, created_at=now + MINIMUM_AGE)
        store.observe(image)
        self.assertEqual(store.eligible_at(image), now - 100 + MINIMUM_AGE)
        self.assertFalse(store.eligible(image))
        self.assertEqual(store.evidence_source(image), "docker-hub")

    def test_codeberg_publication_qualifies_with_exact_repository(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = registered(1, now - MINIMUM_AGE, repository="codeberg.org/readeck/readeck", source="codeberg")
        store.observe(image)
        self.assertTrue(store.eligible(image))
        self.assertEqual(store.evidence_source(image), "codeberg")

    def test_ghcr_publication_cannot_cross_packages_with_identical_content(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = registered(1, now - MINIMUM_AGE, repository="ghcr.io/immich-app/immich-server", source="ghcr")
        store.observe(image)
        self.assertTrue(store.eligible(image))
        self.assertEqual(store.evidence_source(image), "ghcr")
        other_package = registered(1, None, repository="ghcr.io/another-owner/immich-server")
        store.observe(other_package)
        self.assertFalse(store.eligible(other_package))
        self.assertEqual(store.evidence_source(other_package), "observation")
        other_package["publication"] = dict(image["publication"])
        with self.assertRaises(AgeError):
            store.observe(other_package)

    def test_mirror_publication_qualifies_with_exact_path(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = mirrored(1, now - MINIMUM_AGE)
        store.observe(image)
        self.assertTrue(store.eligible(image))
        self.assertEqual(store.evidence_source(image), "docker-hub-mirror")

    def test_malformed_publication_fails_closed(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        base = registered(1, now - MINIMUM_AGE)
        malformed = [
            dict(base, publication="not-a-dict"),
            dict(base, publication={"source": "docker-hub"}),
            dict(base, publication=dict(base["publication"], published_at=True)),
            dict(base, publication=dict(base["publication"], published_at="yesterday")),
            dict(base, publication=dict(base["publication"], published_at=float("inf"))),
            dict(base, publication=dict(base["publication"], published_at=float("nan"))),
            dict(base, publication=dict(base["publication"], published_at=-1)),
            dict(base, publication=dict(base["publication"], manifest=digest(2))),
            dict(base, publication=dict(base["publication"], config=digest(2))),
            dict(base, publication=dict(base["publication"], layers=[digest(999)])),
            dict(base, publication=dict(base["publication"], layers=list(base["layers"]) + [digest(999)])),
            dict(base, publication=dict(base["publication"], layers="not-a-list")),
            dict(base, publication=dict(base["publication"], extra="field")),
        ]
        missing = dict(base["publication"])
        del missing["published_at"]
        malformed.append(dict(base, publication=missing))
        swapped = dict(base)
        swapped["publication"] = dict(base["publication"], manifest=base["config"], config=base["manifest"])
        malformed.append(swapped)
        for index, image in enumerate(malformed):
            with self.subTest(index=index):
                with self.assertRaises(AgeError):
                    store.observe(image)
                with self.assertRaises(AgeError):
                    store.eligible_at(image)
                with self.assertRaises(AgeError):
                    store.evidence_source(image)

    def test_source_or_repository_mismatch_fails_closed(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        base = registered(1, now - MINIMUM_AGE)
        mismatched = [
            dict(base, publication=dict(base["publication"], source="ghcr")),
            dict(base, publication=dict(base["publication"], source="docker-hub", repository="docker.io/other/app")),
            dict(base, publication=dict(base["publication"], source="docker-hub", repository="example.com/app")),
            dict(base, publication=dict(base["publication"], source="codeberg", repository="docker.io/example/app")),
            dict(base, publication=dict(base["publication"], source="docker-hub-mirror", repository="docker.io/example/app")),
            dict(base, publication=dict(base["publication"], source="docker-hub-mirror", repository="docker.io/linuxserver/app")),
            dict(base, publication=dict(base["publication"], source="docker-hub", repository="docker.io/example/app?x=1")),
            dict(base, publication=dict(base["publication"], source="docker-hub", repository="docker.io/../secret")),
            dict(base, publication=dict(base["publication"], source="docker-hub", repository="docker.io:5000/example/app")),
            dict(base, publication=dict(base["publication"], source="docker-hub", repository="docker.io/example/app#frag")),
            dict(base, publication=dict(base["publication"], source="docker-hub", repository="example/app")),
        ]
        wrong_mirror = mirrored(1, now - MINIMUM_AGE)
        wrong_mirror["publication"] = dict(wrong_mirror["publication"], repository="docker.io/linuxserver/other")
        mismatched.append(wrong_mirror)
        same_repo_mirror = mirrored(1, now - MINIMUM_AGE)
        same_repo_mirror["publication"] = dict(same_repo_mirror["publication"], repository="lscr.io/linuxserver/app")
        mismatched.append(same_repo_mirror)
        for index, image in enumerate(mismatched):
            with self.subTest(index=index):
                with self.assertRaises(AgeError):
                    store.observe(image)
                with self.assertRaises(AgeError):
                    store.eligible_at(image)
                with self.assertRaises(AgeError):
                    store.evidence_source(image)

    def test_tampered_stored_publication_fails_closed(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        store.observe(registered(1, now - MINIMUM_AGE))
        store.save()
        key = "docker.io/example/app@" + digest(1)
        tampered = AgeStore(self.path, now=now)
        tampered.data["publication"][key] = dict(
            tampered.data["publication"][key], published_at=float("inf"))
        with self.assertRaises(AgeError):
            tampered.eligible(registered(1, None))
        with self.assertRaises(AgeError):
            tampered.evidence_source(registered(1, None))

    def test_invalid_stored_evidence_rejected_on_load(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        store.observe(registered(1, now - MINIMUM_AGE))
        store.data["publication"]["docker.io/example/app@" + digest(1)] = {
            "source": "docker-hub", "repository": "docker.io/example/app",
            "manifest": digest(1), "config": digest(101),
            "layers": [digest(201)], "published_at": True,
        }
        store.save()
        with self.assertRaises(AgeError):
            AgeStore(self.path, now=now)

    def test_observation_fallback_boundary_without_publication(self):
        now = 10 * MINIMUM_AGE
        store = AgeStore(self.path, now=now)
        image = registered(1, None)
        store.observe(image)
        self.assertEqual(store.eligible_at(image), now + MINIMUM_AGE)
        self.assertEqual(store.evidence_source(image), "observation")
        self.assertFalse(store.eligible(image))
        store.now += MINIMUM_AGE
        self.assertTrue(store.eligible(image))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = AgeStore(Path(self.temporary.name) / "age.json", now=100)
        self.policy = {"schema": 1, "images": {
            "server": {"group": "immich"}, "ml": {"group": "immich"},
        }}
        self.current = {"schema": 1, "images": {
            key: {"reference": "registry.example/app@" + digest(index), "version": "3.2.2"}
            for index, key in enumerate(("server", "ml"), start=50)
        }}
        self.available = {key: {"v3.3.0", "v3.4.0"} for key in self.current["images"]}

    def record(self, key, number, version):
        image = artifact(number)
        self.store.observe(image)
        self.store.data["candidates"].setdefault(key, {})[image["manifest"]] = {
            "tag": "v" + version, "version": version,
            "order": list(updater.version_order(version)),
            "first_seen": self.store.now, "artifact": image,
        }

    def test_lockstep_release_waits_for_youngest_component(self):
        self.record("server", 1, "3.3.0")
        self.store.now += 100
        self.record("ml", 2, "3.3.0")
        self.store.now += MINIMUM_AGE - 100
        self.assertEqual(updater.choose_pins(self.policy, self.current, self.store, self.available), self.current)
        self.store.now += 100
        selected = updater.choose_pins(self.policy, self.current, self.store, self.available)
        self.assertEqual({pin["version"] for pin in selected["images"].values()}, {"3.3.0"})

    def test_selects_newest_eligible_release_not_young_latest(self):
        for key, number in (("server", 1), ("ml", 2)):
            self.record(key, number, "3.3.0")
        self.store.now += MINIMUM_AGE
        for key, number in (("server", 3), ("ml", 4)):
            self.record(key, number, "3.4.0")
        selected = updater.choose_pins(self.policy, self.current, self.store, self.available)
        self.assertEqual({pin["version"] for pin in selected["images"].values()}, {"3.3.0"})

    def test_missing_registry_does_not_partially_upgrade_family(self):
        for key, number in (("server", 1), ("ml", 2)):
            self.record(key, number, "3.3.0")
        self.store.now += MINIMUM_AGE
        self.available.pop("ml")
        self.assertEqual(updater.choose_pins(self.policy, self.current, self.store, self.available), self.current)

    def test_withdrawn_release_is_not_promoted(self):
        for key, number in (("server", 1), ("ml", 2)):
            self.record(key, number, "3.3.0")
        self.store.now += MINIMUM_AGE
        self.available["server"].remove("v3.3.0")
        self.assertEqual(updater.choose_pins(self.policy, self.current, self.store, self.available), self.current)

    def test_older_release_is_not_selected(self):
        for key, number in (("server", 1), ("ml", 2)):
            self.record(key, number, "3.3.0")
            self.current["images"][key]["version"] = "3.4.0"
        self.store.now += MINIMUM_AGE
        self.assertEqual(updater.choose_pins(self.policy, self.current, self.store, self.available), self.current)

    def test_unreleased_baseline_never_downgrades_to_its_release_floor(self):
        for key, number in (("server", 1), ("ml", 2)):
            self.record(key, number, "3.3.0")
            self.current["images"][key]["version"] = "3.3.0"
            self.current["images"][key]["unreleased_baseline"] = True
        self.store.now += MINIMUM_AGE
        self.assertEqual(updater.choose_pins(self.policy, self.current, self.store, self.available), self.current)
        for key, number in (("server", 3), ("ml", 4)):
            self.record(key, number, "3.4.0")
        self.store.now += MINIMUM_AGE
        selected = updater.choose_pins(self.policy, self.current, self.store, self.available)
        self.assertEqual({pin["version"] for pin in selected["images"].values()}, {"3.4.0"})


class FeedTransactionTests(unittest.TestCase):
    def setUp(self):
        executable = shutil.which("git")
        self.tools = patch.dict(os.environ, {"HOMELAB_GIT": executable})
        self.tools.start()
        self.addCleanup(self.tools.stop)

    def test_initialization_waits_for_observation_and_preserves_published_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed, seed, policy_file, state = (root / leaf for leaf in ("feed", "seed.json", "policy.json", "state"))
            policy = {"schema": 1, "images": {"app": {"repository": "registry.example/app"}}}
            old = {"schema": 1, "images": {"app": {"reference": "registry.example/app@" + digest(1), "version": "1.0.0"}}}
            new = {"schema": 1, "images": {"app": {"reference": "registry.example/app@" + digest(2), "version": "1.1.0"}}}
            seed.write_text(json.dumps(old))
            policy_file.write_text(json.dumps(policy))
            updater.initialize(feed, seed, policy)
            attempted = threading.Event()
            published = threading.Event()
            results, premature = [], []
            flock, initialize = updater.fcntl.flock, updater.initialize

            def announce_lock(descriptor, operation):
                attempted.set()
                return flock(descriptor, operation)

            def initialize_after_publication(*arguments):
                premature.append(not published.is_set())
                return initialize(*arguments)

            def activate():
                results.append(updater.main([
                    "--state-dir", str(state), "--feed", str(feed),
                    "--seed", str(seed), "--policy", str(policy_file), "init",
                ]))

            worker = threading.Thread(target=activate, daemon=True)
            with patch.object(updater, "principal", return_value={"uid": os.getuid()}), \
                    patch.object(updater, "initialize", side_effect=initialize_after_publication):
                try:
                    with updater.locked(state):
                        with patch.object(updater.fcntl, "flock", side_effect=announce_lock):
                            worker.start()
                            self.assertTrue(attempted.wait(5), "initializer never attempted the real feed lock")
                            updater.publish(feed, new, policy)
                            published.set()
                finally:
                    worker.join(10)
                self.assertFalse(worker.is_alive(), "initializer remained blocked after the owner released its lock")
            self.assertEqual(results, [0])
            self.assertEqual(premature, [False])
            self.assertEqual(json.loads((feed / "pins.json").read_text()), new)
            self.assertEqual(updater.git(feed, "status", "--porcelain"), "")

    def test_failed_commit_preserves_committed_input_and_working_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            feed, seed = root / "feed", root / "seed.json"
            policy = {"schema": 1, "images": {"app": {"repository": "registry.example/app"}}}
            old = {"schema": 1, "images": {"app": {"reference": "registry.example/app@" + digest(1), "version": "1.0.0"}}}
            seed.write_text(json.dumps(old))
            updater.initialize(feed, seed, policy)
            before = updater.git(feed, "rev-parse", "HEAD")
            hook = feed / ".git/hooks/pre-commit"
            hook.write_text("#!/bin/sh\nexit 42\n")
            hook.chmod(0o700)
            new = {"schema": 1, "images": {"app": {"reference": "registry.example/app@" + digest(2), "version": "1.1.0"}}}
            with self.assertRaises(updater.UpdateError):
                updater.publish(feed, new, policy)
            self.assertEqual(updater.git(feed, "rev-parse", "HEAD"), before)
            self.assertEqual(json.loads((feed / "pins.json").read_text()), old)
            self.assertEqual(updater.git(feed, "status", "--porcelain"), "")


class AcquisitionTests(unittest.TestCase):
    def test_failed_authenticated_acquisition_removes_private_credentials_and_redacts_errors(self):
        token = "ghp_isolated_acquisition_fixture"
        image = "ghcr.io/linuxserver/kavita@" + digest(1)
        seen = []

        def failed_copy(*arguments, **kwargs):
            self.assertNotIn(token, " ".join(arguments))
            authfile = Path(arguments[arguments.index("--authfile") + 1])
            seen.append(authfile)
            self.assertEqual(authfile.stat().st_mode & 0o777, 0o600)
            self.assertEqual(authfile.parent.stat().st_mode & 0o777, 0o700)
            encoded = next(iter(json.loads(authfile.read_text())["auths"].values()))["auth"]
            raise updater.UpdateError(f"registry rejected {token} / {encoded}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            archive = root / "cache/image.tar"
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}), \
                 patch.object(updater, "current_principal", return_value="image-updates"), \
                 patch.object(updater, "registry_archive_path", return_value=archive), \
                 patch.object(updater.Registry, "github_credentials", return_value=("fixture-user", token)), \
                 patch.object(updater, "role_command", side_effect=lambda role, *args, **kwargs: failed_copy(*args, **kwargs)):
                with self.assertRaises(updater.UpdateError) as failure:
                    updater.acquire_images([image], Path("policy.json"))
            self.assertNotIn(token, str(failure.exception))
            self.assertEqual(str(failure.exception), "registry rejected [redacted] / [redacted]")
            self.assertFalse(seen[0].parent.exists())
            self.assertEqual(list(runtime.iterdir()), [])
            self.assertEqual(list(archive.parent.iterdir()), [])

    def test_authenticated_timeout_cleans_credentials_and_partial_archive(self):
        token = "ghp_timeout_fixture"
        image = "ghcr.io/example/app@" + digest(1)
        seen = []

        def interrupted(role, *arguments, **kwargs):
            authfile = Path(arguments[arguments.index("--authfile") + 1])
            seen.append(authfile)
            destination = arguments[-1].removeprefix("oci-archive:").split(":", 1)[0]
            Path(destination).write_bytes(b"interrupted archive")
            raise updater.subprocess.TimeoutExpired(arguments, 1800, stderr=token)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            archive = root / "cache/image.tar"
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}), \
                 patch.object(updater, "current_principal", return_value="image-updates"), \
                 patch.object(updater, "registry_archive_path", return_value=archive), \
                 patch.object(updater.Registry, "github_credentials", return_value=("fixture", token)), \
                 patch.object(updater, "role_command", side_effect=interrupted):
                with self.assertRaisesRegex(updater.UpdateError, "timed out") as failure:
                    updater.acquire_images([image], Path("policy.json"))
            self.assertNotIn(token, str(failure.exception))
            self.assertFalse(seen[0].exists())
            self.assertEqual(list(runtime.iterdir()), [])
            self.assertEqual(list(archive.parent.iterdir()), [])

    def test_cached_manifest_mismatch_is_rejected_without_registry_access(self):
        image = "ghcr.io/example/app@" + digest(1)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "image.tar"
            archive.write_bytes(b"wrong archive")
            with patch.object(updater, "current_principal", return_value="image-updates"), \
                 patch.object(updater, "registry_archive_path", return_value=archive), \
                 patch.object(updater.Registry, "github_credentials") as credentials, \
                 patch.object(updater, "run_as", return_value=Mock(returncode=0, stdout=b"wrong manifest")), \
                 patch.object(updater, "role_command") as copy:
                with self.assertRaisesRegex(updater.UpdateError, "pinned digest"):
                    updater.acquire_images([image], Path("policy.json"))
                credentials.assert_not_called()
                copy.assert_not_called()

    def test_cached_unreadable_archive_is_rejected_without_registry_access(self):
        image = "ghcr.io/example/app@" + digest(1)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "image.tar"
            archive.write_bytes(b"truncated archive")
            with patch.object(updater, "current_principal", return_value="image-updates"), \
                 patch.object(updater, "registry_archive_path", return_value=archive), \
                 patch.object(updater.Registry, "github_credentials") as credentials, \
                 patch.object(updater, "run_as", return_value=Mock(returncode=1)), \
                 patch.object(updater, "role_command") as copy:
                with self.assertRaisesRegex(updater.UpdateError, "invalid registry image archive"):
                    updater.acquire_images([image], Path("policy.json"))
                credentials.assert_not_called()
                copy.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [archive])

    def test_valid_cache_is_reused_and_stale_staging_is_not_published(self):
        raw = b'{"schemaVersion":2,"layers":[]}'
        image = "ghcr.io/example/app@sha256:" + hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            cache.mkdir()
            archive = cache / "image.tar"
            archive.write_bytes(b"complete archive")
            stale = cache / ".acquire-interrupted"
            stale.mkdir()
            (stale / "image.tar").write_bytes(b"partial archive")
            with patch.object(updater, "current_principal", return_value="image-updates"), \
                 patch.object(updater, "registry_archive_path", return_value=archive), \
                 patch.object(updater.Registry, "github_credentials") as credentials, \
                 patch.object(updater, "run_as", return_value=Mock(returncode=0, stdout=raw)), \
                 patch.object(updater, "role_command"):
                updater.acquire_images([image], Path("policy.json"))
                credentials.assert_not_called()
            self.assertEqual(archive.read_bytes(), b"complete archive")
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
            self.assertEqual(set(cache.iterdir()), {archive, stale})

    def test_new_archive_is_published_only_after_manifest_validation(self):
        raw = b'{"schemaVersion":2,"layers":[]}'
        image = "docker.io/example/app@sha256:" + hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "cache/image.tar"

            def copy(role, *arguments, **kwargs):
                self.assertFalse(archive.exists())
                staging = Path(arguments[-1].removeprefix("oci-archive:").split(":", 1)[0])
                staging.write_bytes(b"complete archive")

            def inspect(role, arguments, **kwargs):
                self.assertFalse(archive.exists())
                staging = Path(arguments[-1].removeprefix("oci-archive:").split(":", 1)[0])
                self.assertEqual(staging.read_bytes(), b"complete archive")
                self.assertEqual(staging.stat().st_mode & 0o777, 0o600)
                return Mock(returncode=0, stdout=raw)

            with patch.object(updater, "current_principal", return_value="image-updates"), \
                 patch.object(updater, "registry_archive_path", return_value=archive), \
                 patch.object(updater, "run_as", side_effect=inspect), \
                 patch.object(updater, "role_command", side_effect=copy):
                updater.acquire_images([image], Path("policy.json"))
            self.assertEqual(archive.read_bytes(), b"complete archive")
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(archive.parent.iterdir()), [archive])


class RuntimeApplyTests(unittest.TestCase):
    def test_only_changed_previously_active_family_is_restarted_and_restored(self):
        services = [
            {"principal": "immich", "unit": "immich-database.service", "name": "db", "image": "db@sha256:1"},
            {"principal": "immich", "unit": "immich-server.service", "name": "server", "image": "server@sha256:2"},
            {"principal": "immich", "unit": "immich-redis.service", "name": "redis", "image": "redis@sha256:3"},
        ]
        operations = []
        def runtime(role, executable, *args, **kwargs):
            operations.append((role, args))
            if args[:2] == ("--user", "show"):
                return "inactive" if args[-1] == "immich-redis.service" else "active"
            if args[:2] == ("container", "inspect"):
                return "old" if args[-1] == "server" else "same"
            if args[:2] == ("image", "inspect"):
                return "new" if args[-1].startswith("server@") else "same"
            return ""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.json"
            path.write_text(json.dumps(services))
            with patch.object(updater, "current_principal", return_value="immich"), \
                 patch.object(updater, "principal_for_unit", return_value="immich"), \
                 patch.object(updater, "role_command", side_effect=runtime):
                updater.activate_images(path)
        self.assertIn(("immich", ("--user", "restart", "immich-server.service")), operations)
        self.assertIn(("immich", ("--user", "start", "immich-database.service", "immich-server.service")), operations)
        self.assertFalse(any("redis" == argument for _, args in operations for argument in args))

    def test_cross_principal_apply_rejects_before_runtime_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.json"
            path.write_text(json.dumps([{"principal": "nginx", "unit": "nginx.service"}]))
            with patch.object(updater, "current_principal", return_value="immich"), \
                 patch.object(updater, "role_command") as runtime:
                with self.assertRaises(updater.UpdateError):
                    updater.activate_images(path)
                runtime.assert_not_called()


class DistributionTests(unittest.TestCase):
    def test_unavailable_target_runtime_is_not_treated_as_missing_image(self):
        specification = Mock()
        specification.resolve.return_value = Path("/nix/store/immutable/spec.json")
        specification.read_text.return_value = json.dumps([{
            "principal": "immich", "unit": "immich-server.service", "name": "server",
            "image": "ghcr.io/example/app@" + digest(1),
        }])
        with patch.object(updater, "require_admin"), \
             patch.object(updater, "principal"), \
             patch.object(updater, "principal_for_unit", return_value="immich"), \
             patch.object(updater, "run_as", return_value=Mock(returncode=125)), \
             patch.object(updater, "role_command") as acquire, \
             patch.object(updater, "stream_image") as stream:
            with self.assertRaises(updater.UpdateError):
                updater.distribute_images(specification, Path("/nix/store/policy.json"))
            acquire.assert_not_called()
            stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
