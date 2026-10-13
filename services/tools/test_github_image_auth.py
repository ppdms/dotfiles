import base64
from email.message import Message
import io
import json
import sys
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from image_registry import Registry, RegistryError, _NoLeakRedirectHandler, _github_token
from test_image_registry import FixtureRegistry, Response, digest

PAT = "ghp_isolated_fixture"
REPOSITORY = "ghcr.io/example/app"
SCOPE = "repository:example/app:pull"


class AuthenticatedServer:
    """Reject anonymous GitHub calls and wrong per-repository credentials."""
    def __init__(self):
        self.image = FixtureRegistry()
        self.exchange_count = 0
        self.expire_once = False
        self.expired = False
        self.challenge = None
        self.login = "fixture-owner"
        self.foreign_requests = []

    def open(self, request, **kwargs):
        url = urllib.parse.urlsplit(request.full_url)
        auth = request.get_header("Authorization")
        headers = Message()
        if url.netloc == "api.github.com":
            if auth != "Bearer " + PAT:
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", headers, io.BytesIO())
            if url.path == "/user":
                payload = {"login": self.login}
            elif url.path == "/users/example":
                payload = {"login": "example", "type": "Organization"}
            elif url.path.endswith("/releases"):
                payload = [{"tag_name": "v3.2.4", "draft": False, "prerelease": False}]
            elif url.path.endswith("/versions"):
                payload = [{"id": 1, "name": digest(self.image.manifest), "created_at": "2026-09-01T00:00:00Z", "metadata": {"package_type": "container"}}]
            else:
                raise AssertionError("unexpected fixture API path")
            return self.response(request.full_url, json.dumps(payload).encode())
        if url.netloc != "ghcr.io":
            self.foreign_requests.append(url.netloc)
            if auth is not None:
                raise AssertionError("credential escaped GitHub hosts")
            return self.response(request.full_url, b"{}")
        if url.path == "/token":
            expected = "Basic " + base64.b64encode(("fixture-owner:" + PAT).encode()).decode()
            if auth != expected:
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", headers, io.BytesIO())
            query = urllib.parse.parse_qs(url.query)
            if query != {"service": ["ghcr.io"], "scope": [SCOPE]}:
                raise AssertionError("unexpected token permission")
            self.exchange_count += 1
            return self.response(request.full_url, json.dumps({"token": "scoped-pull-" + str(self.exchange_count)}).encode())
        if auth != "Bearer scoped-pull-" + str(self.exchange_count) or self.exchange_count == 0:
            raise AssertionError("registry request was not authenticated before first use")
        if self.expire_once and not self.expired:
            self.expired = True
            headers["WWW-Authenticate"] = self.challenge or 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="' + SCOPE + '"'
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", headers, io.BytesIO())
        if "/manifests/" in url.path:
            raw = self.image.manifest
        elif url.path.endswith(digest(self.image.config)):
            raw = self.image.config
        elif url.path.endswith(digest(self.image.layer)):
            raw = self.image.layer
        else:
            raise AssertionError("unexpected fixture registry path")
        response = Response(raw, "application/json")
        response.geturl = lambda: request.full_url
        return response

    @staticmethod
    def response(url, body):
        response = Response(body, "application/json")
        response.geturl = lambda: url
        return response


class GitHubAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.credential = patch("image_registry._github_token", return_value=PAT)
        self.credential.start()
        self.addCleanup(self.credential.stop)
        self.server = AuthenticatedServer()
        self.registry = Registry()
        self.registry._opener = self.server
        self.registry._publication._opener = self.server

    def test_authenticated_release_profile_publication_and_immutable_pull(self):
        self.assertEqual(self.registry.release_tags("example/project"), ["v3.2.4"])
        artifact = self.registry.inspect(REPOSITORY, "v3.2.4")
        self.assertEqual(artifact["manifest"], digest(self.server.image.manifest))
        self.assertEqual(artifact["publication"]["source"], "ghcr")
        approved = self.registry.inspect(REPOSITORY, artifact["manifest"])
        self.assertEqual(approved["reference"], artifact["reference"])
        self.assertEqual(self.server.exchange_count, 1)
        self.assertNotIn(PAT, json.dumps(approved))

    def test_expired_repository_token_is_refreshed_without_anonymous_request(self):
        self.server.expire_once = True
        artifact = self.registry.inspect(REPOSITORY, "v3.2.4")
        self.assertEqual(artifact["manifest"], digest(self.server.image.manifest))
        self.assertEqual(self.server.exchange_count, 2)

    def test_hostile_challenge_cannot_expand_permissions_or_send_pat_elsewhere(self):
        for realm, service, scope in (
            ("https://evil.example/token", "ghcr.io", SCOPE),
            ("https://ghcr.io/token?unexpected=1", "ghcr.io", SCOPE),
            ("https://ghcr.io/token", "evil.example", SCOPE),
            ("https://ghcr.io/token", "ghcr.io", "repository:example/app:pull,push"),
            ("https://ghcr.io/token", "ghcr.io", "repository:another/app:pull"),
        ):
            with self.subTest(realm=realm, service=service, scope=scope):
                server = AuthenticatedServer()
                server.expire_once = True
                server.challenge = f'Bearer realm="{realm}",service="{service}",scope="{scope}"'
                registry = Registry()
                registry._opener = server
                with self.assertRaises(RegistryError) as error:
                    registry.inspect(REPOSITORY, "v3.2.4")
                self.assertEqual(server.exchange_count, 1)
                self.assertEqual(server.foreign_requests, [])
                self.assertNotIn(PAT, str(error.exception))

    def test_invalid_authenticated_identity_cannot_be_used_for_basic_exchange(self):
        self.server.login = "owner:injected"
        with self.assertRaises(RegistryError):
            self.registry.inspect(REPOSITORY, "v3.2.4")
        self.assertEqual(self.server.exchange_count, 0)

    def test_other_registry_never_receives_github_credentials(self):
        response = self.registry._api("registry.example", "/v2/", scope="repository:example/app:pull")
        response.close()
        self.assertEqual(self.server.foreign_requests, ["registry.example"])
        self.assertEqual(self.server.exchange_count, 0)

    def test_github_api_auth_failure_never_retries_anonymously(self):
        registry = Registry()
        registry._github_pat = "wrong-fixture-token"
        registry._opener = self.server
        with self.assertRaises(RegistryError) as error:
            registry.release_tags("example/project")
        self.assertEqual(error.exception.status, 401)
        self.assertNotIn("wrong-fixture-token", str(error.exception))


class GitHubRedirectTests(unittest.TestCase):
    def test_same_origin_stays_authenticated_but_cdn_receives_no_bearer(self):
        handler = _NoLeakRedirectHandler()
        for host in ("api.github.com", "ghcr.io"):
            request = urllib.request.Request("https://" + host + "/original", headers={"Authorization": "Bearer fixture"})
            forwarded = handler.redirect_request(request, None, 302, "Found", {}, "https://" + host + "/next")
            self.assertEqual(forwarded.get_header("Authorization"), "Bearer fixture")
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/blob")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_api_cannot_redirect_pat_to_another_host_or_port(self):
        request = urllib.request.Request("https://api.github.com/user", headers={"Authorization": "Bearer fixture"})
        for target in ("https://evil.example/", "https://api.github.com:8443/", "https://api.github.com@evil.example/", "http://api.github.com/"):
            with self.subTest(target=target), self.assertRaises(RegistryError):
                _NoLeakRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)


    def test_basic_token_exchange_refuses_even_same_host_redirect(self):
        request = urllib.request.Request("https://ghcr.io/token", headers={"Authorization": "Basic fixture"})
        for target in ("https://ghcr.io/next", "https://evil.example/token"):
            with self.subTest(target=target), self.assertRaises(RegistryError):
                _NoLeakRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)


class TokenPrincipalTests(unittest.TestCase):
    def test_application_account_does_not_request_updater_token_from_broker(self):
        with patch("principals.current_principal", return_value="immich"), \
             patch.dict(sys.modules, {"homelab_secrets": SimpleNamespace(load_field=lambda *args: self.fail("app requested updater credential"))}):
            self.assertIsNone(_github_token())

    def test_updater_reads_only_its_scoped_token(self):
        fields = []
        def load(component, field):
            fields.append((component, field))
            return PAT.encode()
        with patch("principals.current_principal", return_value="image-updates"), \
             patch.dict(sys.modules, {"homelab_secrets": SimpleNamespace(load_field=load)}):
            self.assertEqual(_github_token(), PAT)
        self.assertEqual(fields, [("image-updates", "github-token")])


if __name__ == "__main__":
    unittest.main()
