import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dockerfile_inputs import DockerfileInputsError
from dockerfile_pins import MAX_CANDIDATES, candidates, propose_pins, qualify, unified_patch
from dockerfile_inputs import collect_inputs
from image_age import MINIMUM_AGE, AgeStore
from image_registry import RegistryError

DAY = 24 * 60 * 60
NOW = 100 * MINIMUM_AGE


def digest(number):
    return "sha256:" + f"{number:064x}"


class Hub:
    """Scripted registry: tags with a push age in days; Docker Hub-style publication evidence."""

    def __init__(self, repositories):
        # {repository: {tag: age_in_days}}
        self.repositories = repositories
        self.by_manifest = {}
        self.inspected = []
        self.number = 0
        self.artifacts = {}
        for repository, tags in repositories.items():
            for tag, age in tags.items():
                self.number += 1
                manifest = digest(self.number)
                self.artifacts[(repository, tag)] = {
                    "reference": f"{repository}@{manifest}",
                    "manifest": manifest,
                    "config": digest(self.number + 100_000),
                    "layers": [digest(self.number + 200_000)],
                    "created_at": None,
                    "publication": {
                        "source": "docker-hub",
                        "repository": repository,
                        "manifest": manifest,
                        "config": digest(self.number + 100_000),
                        "layers": [digest(self.number + 200_000)],
                        "published_at": NOW - int(age * DAY),
                    },
                }
                self.by_manifest[(repository, manifest)] = self.artifacts[(repository, tag)]

    def tags(self, repository):
        if repository not in self.repositories:
            raise RegistryError(f"{repository}: unknown repository", status=404)
        return list(self.repositories[repository])

    def inspect(self, repository, reference):
        self.inspected.append((repository, reference))
        found = self.artifacts.get((repository, reference)) or self.by_manifest.get((repository, reference))
        if found is None:
            raise RegistryError(f"{repository} {reference}: unknown", status=404)
        return dict(found, layers=list(found["layers"]))


NODE = {
    "24-alpine3.24": 2, "24.21-alpine3.24": 2, "24.22.0-alpine3.24": 3, "24.21.0-alpine3.24": 30,
    "24.20.0-alpine3.24": 50, "25.0.0-alpine3.24": 40, "24.22.0-slim": 60, "24.22.0": 60,
}
RUST = {
    "1.99-alpine3.24": 1, "1.99.1-alpine3.24": 1, "1.99.0-alpine3.24": 16, "1.98.1-alpine3.24": 29,
    "1.98-alpine3.24": 29,
}
ALPINE = {"3.24": 30, "3.24.2": 30, "3.24.1": 120, "3.25.0": 5}
HUB = {
    "docker.io/library/node": NODE,
    "docker.io/library/rust": RUST,
    "docker.io/library/alpine": ALPINE,
    "docker.io/oven/bun": {"1.4.2": 40, "1.4.3": 2},
}
N, R, A = "docker.io/library/node", "docker.io/library/rust", "docker.io/library/alpine"


class QualifyTests(unittest.TestCase):
    def test_bare_and_namespaced_docker_hub_names_gain_their_registry(self):
        self.assertEqual(qualify("node"), "docker.io/library/node")
        self.assertEqual(qualify("oven/bun"), "docker.io/oven/bun")

    def test_names_with_an_explicit_registry_are_untouched(self):
        for name in ("docker.io/library/node", "ghcr.io/owner/app", "localhost/app", "registry:5000/team/app"):
            self.assertEqual(qualify(name), name)


class CandidateRuleTests(unittest.TestCase):
    def test_major_only_tag_spans_the_major_and_keeps_the_variant(self):
        self.assertEqual(
            candidates("24-alpine3.24", list(NODE)),
            ["24.22.0-alpine3.24", "24.21.0-alpine3.24", "24.20.0-alpine3.24"],
        )

    def test_minor_series_never_crosses_a_minor(self):
        self.assertEqual(candidates("1.99-alpine3.24", list(RUST)), ["1.99.1-alpine3.24", "1.99.0-alpine3.24"])
        self.assertEqual(candidates("1.98.1-alpine3.24", list(RUST)), ["1.98.1-alpine3.24"])

    def test_exact_patch_tag_stays_in_its_minor(self):
        self.assertEqual(candidates("24.21.0-alpine3.24", list(NODE)), ["24.21.0-alpine3.24"])

    def test_bare_version_does_not_cross_the_release_line(self):
        self.assertEqual(candidates("3.24", list(ALPINE)), ["3.24.2", "3.24.1"])

    def test_tags_without_a_version_have_no_series(self):
        self.assertEqual(candidates("latest", ["latest", "1.0.0"]), [])
        self.assertEqual(candidates("bookworm-slim", ["bookworm-slim", "12.1"]), [])

    def test_ordering_is_numeric_not_lexical(self):
        tags = ["1.9.9", "1.9.10", "1.10.0"]
        self.assertEqual(candidates("1", tags), ["1.10.0", "1.9.10", "1.9.9"])

    def test_four_component_versions_vary_only_the_last(self):
        tags = ["1.2.3.4", "1.2.3.5", "1.2.4.1"]
        self.assertEqual(candidates("1.2.3.4", tags), ["1.2.3.5", "1.2.3.4"])


class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.hub = Hub(HUB)
        self.dockerfile = self.root / "Dockerfile.source"

    def store(self):
        return AgeStore(self.root / "observations.json", now=NOW)

    def write(self, text):
        self.dockerfile.write_text(text)
        return text

    def propose(self):
        return propose_pins(self.dockerfile, self.store(), self.hub)

    def test_floating_tags_become_the_newest_exact_tag_the_gate_accepts(self):
        original = self.write(
            "# build\n"
            "FROM node:24-alpine3.24 AS client\n"
            "FROM --platform=linux/amd64 rust:1.99-alpine3.24 AS server  # toolchain\n"
            "RUN echo hi\n"
            "FROM alpine:3.24\n"
            "COPY --from=client /a /a\n"
        )
        result = self.propose()
        self.assertTrue(result["ok"], result["error"])
        self.assertTrue(result["changed"])
        self.assertEqual(
            result["rewritten"],
            "# build\n"
            "FROM docker.io/library/node:24.21.0-alpine3.24 AS client\n"
            "FROM --platform=linux/amd64 docker.io/library/rust:1.99.0-alpine3.24 AS server  # toolchain\n"
            "RUN echo hi\n"
            "FROM docker.io/library/alpine:3.24.2\n"
            "COPY --from=client /a /a\n",
        )
        by_name = {base["declared"]: base for base in result["bases"]}
        self.assertEqual(by_name["node:24-alpine3.24"]["status"], "pin")
        self.assertEqual(by_name["node:24-alpine3.24"]["chosen"], "24.21.0-alpine3.24")
        self.assertEqual(by_name["rust:1.99-alpine3.24"]["chosen"], "1.99.0-alpine3.24")
        self.assertEqual(by_name["alpine:3.24"]["chosen"], "3.24.2")
        self.assertEqual(self.dockerfile.read_text(), original, "the Dockerfile itself must never be written")

    def test_a_base_already_on_the_chosen_tag_is_left_alone(self):
        self.write("FROM docker.io/library/node:24.21.0-alpine3.24\nFROM docker.io/library/alpine:3.24.2\n")
        result = self.propose()
        self.assertTrue(result["ok"], result["error"])
        self.assertFalse(result["changed"])
        self.assertEqual({base["status"] for base in result["bases"]}, {"keep"})

    def test_a_short_name_on_the_right_tag_is_only_made_explicit(self):
        self.write("FROM node:24.21.0-alpine3.24\nFROM oven/bun:1.4.2\n")
        result = self.propose()
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(
            result["rewritten"],
            "FROM docker.io/library/node:24.21.0-alpine3.24\nFROM docker.io/oven/bun:1.4.2\n",
        )
        self.assertEqual({base["status"] for base in result["bases"]}, {"qualify"})

    def test_a_short_name_is_what_the_real_gate_would_have_refused(self):
        # The bare name is rejected by the age store once publication evidence is attached.
        self.write("FROM node:24.21.0-alpine3.24\n")
        with self.assertRaises(Exception):
            collect_inputs(self.dockerfile, self.store(), require_aged=True, registry=self.hub)

    def test_patch_downgrade_inside_a_minor_is_flagged(self):
        self.write("FROM rust:1.99.1-alpine3.24\n")
        result = self.propose()
        self.assertTrue(result["ok"], result["error"])
        base = result["bases"][0]
        self.assertEqual((base["status"], base["chosen"], base["downgrade"]), ("pin", "1.99.0-alpine3.24", True))
        self.assertEqual(result["rewritten"], "FROM docker.io/library/rust:1.99.0-alpine3.24\n")

    def test_floating_to_exact_is_not_a_downgrade(self):
        self.write("FROM node:24-alpine3.24\n")
        self.assertFalse(self.propose()["bases"][0]["downgrade"])

    def test_nothing_old_enough_is_blocked_with_the_earliest_date_and_no_edit(self):
        self.write("FROM docker.io/library/node:24.22.0-alpine3.24\n")
        result = self.propose()
        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        base = result["bases"][0]
        self.assertEqual(base["status"], "blocked")
        self.assertEqual(base["eligible_at"], NOW - 3 * DAY + MINIMUM_AGE)
        self.assertIn("seven days", base["reason"])
        self.assertIn("blocked", result["error"])

    def test_unversioned_tag_is_reported_not_guessed(self):
        self.hub = Hub({"docker.io/library/debian": {"bookworm-slim": 40, "12.5": 40}})
        self.write("FROM docker.io/library/debian:bookworm-slim\n")
        result = self.propose()
        self.assertEqual(result["bases"][0]["status"], "unpinnable")
        self.assertEqual(result["rewritten"], "FROM docker.io/library/debian:bookworm-slim\n")
        self.assertTrue(result["ok"], "an already aged unversioned tag still passes the real gate")

    def test_young_unversioned_tag_fails_the_real_gate(self):
        self.hub = Hub({"docker.io/library/debian": {"bookworm-slim": 1}})
        self.write("FROM docker.io/library/debian:bookworm-slim\n")
        result = self.propose()
        self.assertEqual(result["bases"][0]["status"], "unpinnable")
        self.assertFalse(result["ok"])

    def test_digest_pinned_base_is_never_rewritten(self):
        manifest = self.hub.artifacts[(A, "3.24.2")]["manifest"]
        self.write(f"FROM docker.io/library/alpine@{manifest}\n")
        result = self.propose()
        self.assertEqual(result["bases"][0]["status"], "unpinnable")
        self.assertEqual(result["bases"][0]["reason"], "already fixed by digest")
        self.assertFalse(result["changed"])

    def test_result_is_stable_and_idempotent(self):
        self.write("FROM node:24-alpine3.24\nFROM rust:1.99-alpine3.24\nFROM alpine:3.24\n")
        first = self.propose()
        second = self.propose()
        self.assertEqual(first["rewritten"], second["rewritten"])
        self.assertEqual([b["chosen"] for b in first["bases"]], [b["chosen"] for b in second["bases"]])
        self.write(first["rewritten"])
        again = self.propose()
        self.assertFalse(again["changed"])
        self.assertEqual({base["status"] for base in again["bases"]}, {"keep"})

    def test_a_base_used_by_two_stages_is_one_decision_and_both_lines_change(self):
        self.write("FROM node:24-alpine3.24 AS a\nFROM node:24-alpine3.24 AS b\n")
        result = self.propose()
        self.assertEqual(len(result["bases"]), 1)
        self.assertEqual(
            result["rewritten"],
            "FROM docker.io/library/node:24.21.0-alpine3.24 AS a\nFROM docker.io/library/node:24.21.0-alpine3.24 AS b\n",
        )

    def test_crlf_and_missing_final_newline_are_preserved(self):
        self.write("FROM alpine:3.24\r\nRUN true\r\nFROM node:24-alpine3.24")
        result = self.propose()
        self.assertEqual(
            result["rewritten"],
            "FROM docker.io/library/alpine:3.24.2\r\nRUN true\r\nFROM docker.io/library/node:24.21.0-alpine3.24",
        )

    def test_candidate_inspection_is_bounded(self):
        self.hub = Hub({"docker.io/library/bigrepo": {f"9.0.{patch}": 1 for patch in range(40)}})
        self.write("FROM docker.io/library/bigrepo:9.0.0\n")
        result = self.propose()
        self.assertFalse(result["ok"])
        self.assertEqual(result["bases"][0]["checked"], MAX_CANDIDATES)
        listed = [reference for repository, reference in self.hub.inspected if repository == "docker.io/library/bigrepo"]
        self.assertTrue(all(tag.startswith("9.0.") for tag in listed[:MAX_CANDIDATES]))

    def test_registry_failure_names_the_base_and_leaves_the_file_alone(self):
        self.write("FROM ghost:1.0\n")
        with self.assertRaises(RegistryError) as raised:
            self.propose()
        self.assertIn("ghost:1.0", str(raised.exception))

    def test_dockerfile_without_external_bases_is_refused(self):
        self.write("FROM scratch\n")
        with self.assertRaises(DockerfileInputsError):
            self.propose()

    def test_observations_of_inspected_candidates_are_persisted(self):
        self.write("FROM node:24-alpine3.24\n")
        self.propose()
        content = AgeStore(self.root / "observations.json", now=NOW).data["content"]
        self.assertTrue(content, "inspected digests must be recorded like any other observation")


class PatchTests(unittest.TestCase):
    def test_no_change_gives_an_empty_patch(self):
        self.assertEqual(unified_patch("FROM a:1\n", "FROM a:1\n", "Dockerfile"), "")

    def test_patch_names_the_path_and_only_the_changed_line(self):
        patch = unified_patch("A\nFROM a:1\nB\n", "A\nFROM a:1.2\nB\n", "Dockerfile.source")
        self.assertIn("--- a/Dockerfile.source\n+++ b/Dockerfile.source\n", patch)
        self.assertIn("-FROM a:1\n+FROM a:1.2\n", patch)

    @unittest.skipUnless(shutil.which("git"), "git is required to prove the patch applies")
    def test_patch_applies_with_git_including_a_missing_final_newline(self):
        for original, rewritten in [
            ("X\nFROM node:24-alpine3.24\nY\n", "X\nFROM node:24.21.0-alpine3.24\nY\n"),
            ("X\nFROM node:24-alpine3.24", "X\nFROM node:24.21.0-alpine3.24"),
        ]:
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "Dockerfile.source").write_text(original)
                patch = unified_patch(original, rewritten, "Dockerfile.source")
                run = subprocess.run(
                    ["git", "apply", "-"], input=patch, text=True, cwd=directory, capture_output=True,
                )
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual((Path(directory) / "Dockerfile.source").read_text(), rewritten)


if __name__ == "__main__":
    unittest.main()
