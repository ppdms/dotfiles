import unittest
from unittest.mock import patch

from image_publication import PublicationClient


def digest(byte):
    return "sha256:" + ("%02x" % byte) * 32


MANIFEST = digest(1)
CONFIG = digest(2)
LAYER_A = digest(3)
LAYER_B = digest(4)
TOP = digest(5)
OTHER = digest(6)

OLD = "2024-09-27T06:02:12Z"
OLD_EPOCH = 1727416932
NEW = "2026-10-06T15:46:52.120911934Z"

REPO = "docker.io/library/nginx"
LSCR = "lscr.io/linuxserver/kavita"
MIRROR_META = "docker.io/linuxserver/kavita"
CODEBERG = "codeberg.org/readeck/readeck"
GHCR_ORG = "ghcr.io/example/app"
GHCR_USER = "ghcr.io/someone/app"


def artifact(manifest=MANIFEST, config=CONFIG, layers=None):
    return {
        "reference": f"{REPO}@{manifest}",
        "manifest": manifest,
        "config": config,
        "layers": [LAYER_A, LAYER_B] if layers is None else list(layers),
    }


def hub_image(digest_value=MANIFEST, pushed=OLD, arch="amd64",
              system="linux", status="active"):
    return {
        "architecture": arch, "os": system, "digest": digest_value,
        "status": status, "last_pushed": pushed,
    }


def hub_document(images, top=TOP, tag_pushed=NEW, name="1.31.2"):
    return {
        "name": name, "digest": top, "tag_last_pushed": tag_pushed,
        "last_updated": tag_pushed, "images": images,
    }


def codeberg_meta(version=MANIFEST, created="2026-09-17T17:41:03+02:00"):
    return {"version": version, "created_at": created}


def codeberg_file(name, digest_value):
    return {"name": name, "sha256": digest_value.partition(":")[2]}


def codeberg_files(manifest=MANIFEST, config=CONFIG, layers=(LAYER_A, LAYER_B)):
    files = [codeberg_file("manifest.json", manifest),
             codeberg_file("sha256_x", config)]
    files.extend(codeberg_file("sha256_y", layer) for layer in layers)
    return files


def ghcr_owner(kind="Organization", login="example"):
    return {"login": login, "type": kind}


def ghcr_version(name=MANIFEST, created=OLD, package_type="container",
                 deleted_at=None, tags=("1.0.0",)):
    return {
        "id": 1, "name": name, "created_at": created,
        "updated_at": created, "deleted_at": deleted_at,
        "metadata": {"package_type": package_type,
                     "container": {"tags": list(tags)}},
    }


class ScriptedClient(PublicationClient):

    def __init__(self, documents=(), failures=(), github_documents=(),
                 github_failures=(), token="token"):
        super().__init__(github_token=(lambda: token))
        self.documents = list(documents)
        self.failures = list(failures)
        self.github_documents = list(github_documents)
        self.github_failures = list(github_failures)
        self.calls = []
        self.seen_tokens = []

    def _get_json(self, host, path):
        from image_publication import _FetchError
        self.calls.append((host, path))
        if self.failures:
            raise self.failures.pop(0)
        if self.documents:
            return self.documents.pop(0)
        raise _FetchError("no scripted document")

    def _get_github_json(self, host, path, token):
        from image_publication import _FetchError
        self.calls.append((host, path))
        self.seen_tokens.append(token)
        if self.github_failures:
            raise self.github_failures.pop(0)
        if self.github_documents:
            return self.github_documents.pop(0)
        raise _FetchError("no scripted github document")


class ProviderRoutingTests(unittest.TestCase):
    def test_unsupported_registries_skip_the_network(self):
        client = ScriptedClient()
        self.assertIsNone(
            client.lookup("registry.example/app", "1.0.0", artifact()))
        self.assertEqual(client.calls, [])


class DockerHubTests(unittest.TestCase):
    def test_old_exact_base_qualifies_from_per_image_push_date(self):
        client = ScriptedClient([hub_document([hub_image()])])
        evidence = client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP)
        self.assertEqual(evidence, {
            "source": "docker-hub", "repository": REPO, "manifest": MANIFEST,
            "config": CONFIG, "layers": [LAYER_A, LAYER_B],
            "published_at": OLD_EPOCH,
        })

    def test_tag_level_dates_never_supply_evidence(self):
        # Per-image entry is unusable; tag_last_pushed/last_updated are young
        # but must not be consulted.
        client = ScriptedClient([hub_document(
            [hub_image(pushed="not-a-date")], tag_pushed=NEW)])
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))

    def test_wrong_digest_architecture_or_status_cannot_accelerate(self):
        for images in (
            [hub_image(digest_value=OTHER)],
            [hub_image(arch="arm64")],
            [hub_image(system="windows")],
            [hub_image(status="inactive")],
            [hub_image(digest_value=OTHER), hub_image(arch="arm64")],
        ):
            with self.subTest(images=images):
                client = ScriptedClient([hub_document(images)])
                self.assertIsNone(
                    client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))

    def test_moved_or_stale_tag_yields_none_via_top_digest(self):
        client = ScriptedClient([hub_document([hub_image()], top=OTHER)])
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))
        client = ScriptedClient(
            [hub_document([hub_image()], name="other-tag")])
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))
    def test_index_only_date_is_rejected(self):
        # Only the index digest appears; the platform manifest is absent.
        client = ScriptedClient([hub_document([hub_image(digest_value=TOP)])])
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))

    def test_young_and_future_dates_are_preserved_as_evidence(self):
        for pushed, expected in (
            ("2026-10-07T00:00:00Z", 1791331200),
            ("2030-01-01T00:00:00Z", 1893456000),
        ):
            with self.subTest(pushed=pushed):
                client = ScriptedClient(
                    [hub_document([hub_image(pushed=pushed)])])
                evidence = client.lookup(
                    REPO, "1.31.2", artifact(), top_digest=TOP)
                self.assertIsNotNone(evidence)
                self.assertEqual(evidence["published_at"], expected)

    def test_absent_or_malformed_metadata_falls_back(self):
        payloads = [
            {"name": "1.31.2", "digest": TOP, "images": "nope"},
            {"name": "1.31.2", "digest": TOP},
            ["not", "a", "dict"],
            hub_document([{"digest": MANIFEST}]),
            hub_document([hub_image(pushed="")]),
            hub_document([hub_image(pushed="0001-01-01T00:00:00Z")]),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                client = ScriptedClient([payload])
                self.assertIsNone(
                    client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))

    def test_ambiguous_timezone_cannot_supply_age_evidence(self):
        client = ScriptedClient(
            [hub_document([hub_image(pushed="2024-09-27T06:02:12")])])
        self.assertIsNone(client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))


    def test_metadata_errors_never_raise_or_become_registry_fatal(self):
        from image_publication import _FetchError
        client = ScriptedClient(failures=[_FetchError("boom")])
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))
        client = ScriptedClient(failures=[ValueError("unexpected")])
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP))

    def test_malformed_caller_input_yields_none(self):
        client = ScriptedClient([hub_document([hub_image()])])
        bad_artifact = artifact()
        bad_artifact["layers"] = ["not-a-digest"]
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", bad_artifact, top_digest=TOP))
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", "not-a-dict", top_digest=TOP))
        self.assertIsNone(
            client.lookup("not a repo!", "1.31.2", artifact(), top_digest=TOP))
        self.assertIsNone(
            client.lookup(REPO, "", artifact(), top_digest=TOP))
        self.assertIsNone(
            client.lookup(REPO, "bad tag!", artifact(), top_digest=TOP))
        # Malformed top_digest fails closed (no tag binding check possible).
        self.assertIsNone(
            client.lookup(REPO, "1.31.2", artifact(), top_digest="bogus"))

    def test_digest_lookup_reuses_cached_evidence_without_network(self):
        client = ScriptedClient([hub_document([hub_image()])])
        first = client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP)
        self.assertIsNotNone(first)
        cached = client.lookup(REPO, MANIFEST, artifact(), top_digest=TOP)
        self.assertEqual(cached, first)
        self.assertEqual(len(client.calls), 1)
        self.assertIsNot(cached, first)

    def test_digest_lookup_without_cache_returns_none(self):
        client = ScriptedClient()
        self.assertIsNone(
            client.lookup(REPO, MANIFEST, artifact(), top_digest=TOP))
        self.assertEqual(client.calls, [])

    def test_changed_config_or_layers_cannot_reuse_cached_proof(self):
        client = ScriptedClient([hub_document([hub_image()])])
        client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP)
        changed = artifact(config=OTHER)
        self.assertIsNone(
            client.lookup(REPO, MANIFEST, changed, top_digest=TOP))
        changed = artifact(layers=[LAYER_A])
        self.assertIsNone(
            client.lookup(REPO, MANIFEST, changed, top_digest=TOP))

    def test_newer_trustworthy_date_supersedes_stale_cache(self):
        client = ScriptedClient([
            hub_document([hub_image(pushed=OLD)]),
            hub_document([hub_image(pushed=NEW)]),
        ])
        first = client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP)
        second = client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP)
        self.assertLess(first["published_at"], second["published_at"])
        self.assertEqual(
            client.lookup(REPO, MANIFEST, artifact())["published_at"],
            second["published_at"])

    def test_stale_payload_cannot_resurrect_earlier_deadline(self):
        client = ScriptedClient([
            hub_document([hub_image(pushed=NEW)]),
            hub_document([hub_image(pushed=OLD)]),
        ])
        client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP)
        second = client.lookup(REPO, "1.31.2", artifact(), top_digest=TOP)
        self.assertEqual(second["published_at"], 1791301613)


class MirrorTests(unittest.TestCase):
    def test_same_digest_mirror_uses_hub_metadata_source(self):
        client = ScriptedClient([hub_document([hub_image()], name="latest")])
        evidence = client.lookup(LSCR, "latest", artifact(), top_digest=TOP)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["source"], "docker-hub-mirror")
        self.assertEqual(evidence["repository"], MIRROR_META)
        self.assertEqual(evidence["manifest"], MANIFEST)
        self.assertEqual(evidence["published_at"], OLD_EPOCH)

    def test_same_platform_with_different_index_still_proves_selected_content_age(self):
        client = ScriptedClient(
            [hub_document([hub_image()], top=OTHER, name="latest")])
        evidence = client.lookup(LSCR, "latest", artifact(), top_digest=TOP)
        self.assertEqual(evidence["manifest"], MANIFEST)
        self.assertEqual(evidence["published_at"], OLD_EPOCH)

    def test_diverged_mirror_digest_yields_none(self):
        client = ScriptedClient(
            [hub_document([hub_image(digest_value=OTHER)], name="latest")])
        self.assertIsNone(
            client.lookup(LSCR, "latest", artifact(), top_digest=TOP))

    def test_non_linuxserver_lscr_has_no_metadata_source(self):
        client = ScriptedClient()
        self.assertIsNone(
            client.lookup("lscr.io/other/image", "latest", artifact()))
        self.assertEqual(client.calls, [])


class CodebergTests(unittest.TestCase):
    def test_digest_version_with_complete_files_supplies_evidence(self):
        client = ScriptedClient([codeberg_meta(), codeberg_files()])
        evidence = client.lookup(
            CODEBERG, "0.23.4", artifact(), top_digest=TOP)
        self.assertEqual(evidence, {
            "source": "codeberg", "repository": CODEBERG,
            "manifest": MANIFEST, "config": CONFIG,
            "layers": [LAYER_A, LAYER_B],
            "published_at": 1789659663,
        })

    def test_explicit_digest_can_obtain_publication_without_tag_or_cache(self):
        client = ScriptedClient([codeberg_meta(), codeberg_files()])
        evidence = client.lookup(CODEBERG, MANIFEST, artifact(), top_digest=MANIFEST)
        self.assertEqual(evidence["published_at"], 1789659663)


    def test_missing_layer_or_config_file_rejects_date(self):
        for files in (
            codeberg_files(layers=(LAYER_A,)),
            [codeberg_file("manifest.json", MANIFEST),
             codeberg_file("sha256_x", LAYER_A),
             codeberg_file("sha256_y", LAYER_B)],
            [codeberg_file("manifest.json", OTHER),
             codeberg_file("sha256_x", CONFIG),
             codeberg_file("sha256_y", LAYER_A),
             codeberg_file("sha256_z", LAYER_B)],
            [],
            "not-a-list",
        ):
            with self.subTest(files=files):
                client = ScriptedClient([codeberg_meta(), files])
                self.assertIsNone(
                    client.lookup(CODEBERG, "0.23.4", artifact()))

    def test_mutable_tag_creation_time_is_never_used(self):
        # Tag versions carry their own created_at; only the digest-version
        # identity is accepted.
        tag_meta = codeberg_meta(version="0.23.4",
                                 created="2020-01-01T00:00:00+00:00")
        client = ScriptedClient([tag_meta, codeberg_files()])
        self.assertIsNone(client.lookup(CODEBERG, "0.23.4", artifact()))

    def test_files_endpoint_failure_yields_none(self):
        from image_publication import _FetchError
        client = ScriptedClient([codeberg_meta()],
                                failures=[_FetchError("down")])
        # First call consumes the metadata document; reorder so the metadata
        # succeeds and the files fetch fails.
        client.failures = []
        client.documents = [codeberg_meta()]
        real = client._get_json
        calls = {"count": 0}

        def flaky(host, path):
            calls["count"] += 1
            if calls["count"] == 2:
                raise _FetchError("down")
            return real(host, path)

        with patch.object(client, "_get_json", side_effect=flaky):
            self.assertIsNone(client.lookup(CODEBERG, "0.23.4", artifact()))

    def test_unparseable_created_at_yields_none(self):
        client = ScriptedClient(
            [codeberg_meta(created="yesterday"), codeberg_files()])
        self.assertIsNone(client.lookup(CODEBERG, "0.23.4", artifact()))

    def test_future_codeberg_date_is_preserved_as_evidence(self):
        client = ScriptedClient([
            codeberg_meta(created="2030-06-01T12:00:00+00:00"),
            codeberg_files(),
        ])
        evidence = client.lookup(CODEBERG, "0.23.4", artifact())
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["published_at"], 1906545600)


class GHCRTests(unittest.TestCase):
    def test_exact_platform_version_supplies_evidence_for_org(self):
        client = ScriptedClient(
            [ghcr_owner()],
            github_documents=[[ghcr_version()]],
        )
        evidence = client.lookup(GHCR_ORG, "1.0.0", artifact(), top_digest=TOP)
        self.assertEqual(evidence, {
            "source": "ghcr", "repository": GHCR_ORG,
            "manifest": MANIFEST, "config": CONFIG,
            "layers": [LAYER_A, LAYER_B],
            "published_at": OLD_EPOCH,
        })
        self.assertEqual(client.calls[0],
                         ("api.github.com", "/users/example"))
        self.assertTrue(client.calls[1][0] == "api.github.com"
                        and "/orgs/example/packages/container/app/versions"
                        in client.calls[1][1])

    def test_user_owner_uses_user_route(self):
        client = ScriptedClient(
            [ghcr_owner("User", "someone")],
            github_documents=[[ghcr_version()]],
        )
        evidence = client.lookup(GHCR_USER, "1.0.0", artifact(), top_digest=TOP)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["source"], "ghcr")
        self.assertTrue("/users/someone/packages/container/app/versions"
                        in client.calls[1][1])

    def test_index_digest_version_never_supplies_child_age(self):
        # Only the top-level index digest carries created_at; the selected
        # platform child is absent.
        client = ScriptedClient(
            [ghcr_owner()],
            github_documents=[[ghcr_version(name=TOP)]],
        )
        self.assertIsNone(
            client.lookup(GHCR_ORG, "1.0.0", artifact(), top_digest=TOP))

    def test_tag_named_version_never_supplies_age(self):
        client = ScriptedClient(
            [ghcr_owner()],
            github_documents=[[ghcr_version(name="1.0.0")]],
        )
        self.assertIsNone(
            client.lookup(GHCR_ORG, "1.0.0", artifact(), top_digest=TOP))

    def test_mismatch_unsupported_deleted_or_bad_date_returns_none(self):
        payloads = [
            [ghcr_version(name=OTHER)],
            [ghcr_version(package_type="docker")],
            [ghcr_version(deleted_at=OLD)],
            [ghcr_version(created="not-a-date")],
            {"unexpected": "shape"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                client = ScriptedClient(
                    [ghcr_owner()], github_documents=[payload])
                self.assertIsNone(
                    client.lookup(GHCR_ORG, "1.0.0", artifact()))

    def test_digest_reference_queries_versions_without_cache(self):
        client = ScriptedClient(
            [ghcr_owner()],
            github_documents=[[ghcr_version()]],
        )
        evidence = client.lookup(GHCR_ORG, MANIFEST, artifact(), top_digest=TOP)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["published_at"], OLD_EPOCH)
        self.assertEqual(len(client.calls), 2)

    def test_missing_or_invalid_token_skips_the_network(self):
        for token in (None, "", "   ", "has space", "x" * 1025, 12345,
                      b"\xff\xfe"):
            with self.subTest(token=repr(token)[:24]):
                client = ScriptedClient(token=token)
                self.assertIsNone(
                    client.lookup(GHCR_ORG, "1.0.0", artifact()))
                self.assertEqual(client.calls, [])

    def test_callback_failure_skips_the_network(self):
        def boom():
            raise RuntimeError("locked")

        client = PublicationClient(github_token=boom)
        with patch.object(client, "_get_json") as plain, \
                patch.object(client, "_get_github_json") as authed:
            self.assertIsNone(
                client.lookup(GHCR_ORG, "1.0.0", artifact()))
        plain.assert_not_called()
        authed.assert_not_called()

    def test_unknown_owner_kind_returns_none(self):
        for profile in (ghcr_owner("Bot"), {"login": "example"},
                        {"login": "other", "type": "Organization"},
                        ["not-a-dict"]):
            with self.subTest(profile=profile):
                client = ScriptedClient([profile])
                self.assertIsNone(
                    client.lookup(GHCR_ORG, "1.0.0", artifact()))

    def test_malformed_ghcr_repository_skips_the_network(self):
        client = ScriptedClient()
        for repository in ("ghcr.io/only-owner", "ghcr.io/-bad/app",
                           "ghcr.io/owner//empty", "ghcr.io/owner/has space"):
            with self.subTest(repository=repository):
                self.assertIsNone(
                    client.lookup(repository, "1.0.0", artifact()))
        self.assertEqual(client.calls, [])

    def test_pagination_finds_old_release_after_young_first_page(self):
        young = [ghcr_version(name="sha256:%064x" % byte, created=NEW)
                 for byte in range(10, 110)]
        client = ScriptedClient(
            [ghcr_owner()],
            github_documents=[young, [ghcr_version()]],
        )
        evidence = client.lookup(GHCR_ORG, "1.0.0", artifact(), top_digest=TOP)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["published_at"], OLD_EPOCH)
        self.assertEqual(len(client.calls), 3)
        self.assertIn("page=1", client.calls[1][1])
        self.assertIn("page=2", client.calls[2][1])

    def test_repeated_lookup_reuses_version_cache(self):
        client = ScriptedClient(
            [ghcr_owner()],
            github_documents=[[ghcr_version(), ghcr_version(name=OTHER)]],
        )
        first = client.lookup(GHCR_ORG, "1.0.0", artifact(), top_digest=TOP)
        self.assertIsNotNone(first)
        # A different verified artifact whose digest rode along on the
        # already-fetched page resolves without further network use.
        other = artifact(manifest=OTHER)
        second = client.lookup(GHCR_ORG, OTHER, other, top_digest=TOP)
        self.assertIsNotNone(second)
        self.assertEqual(second["manifest"], OTHER)
        self.assertEqual(second["published_at"], OLD_EPOCH)
        self.assertEqual(len(client.calls), 2)

    def test_future_ghcr_date_is_preserved_as_evidence(self):
        client = ScriptedClient(
            [ghcr_owner()],
            github_documents=[[
                ghcr_version(created="2030-06-01T12:00:00+00:00")]],
        )
        evidence = client.lookup(GHCR_ORG, "1.0.0", artifact())
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["published_at"], 1906545600)

    def test_metadata_errors_yield_none_without_evidence(self):
        from image_publication import _FetchError
        client = ScriptedClient(
            [ghcr_owner()],
            github_failures=[_FetchError("denied")])
        self.assertIsNone(
            client.lookup(GHCR_ORG, "1.0.0", artifact()))

    def test_availability_validation_remains_inspector_owned(self):
        # The publication layer binds the verified binding as supplied; it
        # never drops an unusable-config artifact into evidence.
        client = ScriptedClient(
            [ghcr_owner()],
            github_documents=[[ghcr_version()]],
        )
        bad = artifact()
        bad["config"] = "not-a-digest"
        self.assertIsNone(client.lookup(GHCR_ORG, "1.0.0", bad))
        self.assertEqual(client.calls, [])


class TransportSafetyTests(unittest.TestCase):
    def test_invalid_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            PublicationClient(timeout=0)
        with self.assertRaises(ValueError):
            PublicationClient(timeout=-1)

    def test_redirect_cannot_leave_trusted_provider_before_response_fetch(self):
        import urllib.request
        from image_publication import _FetchError, _SafeRedirectHandler

        request = urllib.request.Request("https://hub.docker.com/v2/repositories/library/nginx/tags/latest")
        for target in ("https://registry.example/private", "http://hub.docker.com/other"):
            with self.subTest(target=target), self.assertRaises(_FetchError):
                _SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)

    def test_github_redirect_preserves_authentication_and_enforces_limit(self):
        import urllib.request
        from image_publication import _FetchError, _SafeRedirectHandler

        request = urllib.request.Request(
            "https://api.github.com/orgs/example/packages/container/app/versions")
        request.add_header("Authorization", "Bearer secret")
        handler = _SafeRedirectHandler()
        for hop in range(10):
            request = handler.redirect_request(
                request, None, 302, "Found", {},
                f"https://api.github.com/next-page-{hop}")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        with self.assertRaises(_FetchError):
            handler.redirect_request(
                request, None, 302, "Found", {},
                "https://api.github.com/too-many-redirects")

    def test_github_token_confined_to_header_not_url_redirect_error_or_state(self):
        import urllib.request
        from image_publication import _FetchError

        seen = {}
        client = PublicationClient(github_token=lambda: "secret-token")

        class _Response:
            def geturl(self):
                return "https://api.github.com/page"

            def read(self, _size):
                return b""

            def close(self):
                return None

        def fake_open(request, timeout=None):
            seen["url"] = request.full_url
            seen["authorization"] = request.get_header("Authorization")
            seen["accept"] = request.get_header("Accept")
            return _Response()

        with patch.object(client._opener, "open", side_effect=fake_open):
            with patch("json.loads", return_value=[]):
                self.assertEqual(
                    client._get_github_json(
                        "api.github.com", "/users/example", "secret-token"),
                    [])
        self.assertEqual(seen["authorization"], "Bearer secret-token")
        self.assertNotIn("secret-token", seen["url"])
        self.assertNotIn("secret", repr(client._ghcr_versions))
        with self.assertRaises(_FetchError):
            client._get_github_json(
                "registry.example", "/users/example", "secret-token")
        try:
            client._get_github_json(
                "api.github.com", "https://evil.example/", "secret-token")
        except _FetchError as error:
            self.assertNotIn("secret-token", str(error))
        else:
            self.fail("alien redirect target must be refused")


if __name__ == "__main__":
    unittest.main()
