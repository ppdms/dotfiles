import hashlib
import io
import json
from email.message import Message
import unittest

from image_registry import Registry, RegistryError, stable_tags

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


def encoded(value):
    return json.dumps(value).encode()


def digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


class Response(io.BytesIO):
    def __init__(self, body, media):
        super().__init__(body)
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = media
        self.headers["Docker-Content-Digest"] = digest(body)


class FixtureRegistry(Registry):
    def __init__(self, architecture="amd64"):
        super().__init__()
        self.config = encoded({"os": "linux", "architecture": architecture})
        self.layer = b"compressed layer content"
        self.manifest = encoded({
            "schemaVersion": 2, "mediaType": MANIFEST,
            "config": {"mediaType": CONFIG, "digest": digest(self.config)},
            "layers": [{"mediaType": LAYER, "digest": digest(self.layer)}],
        })
        self.missing_layer = False
        self.corrupt_config = False

    def _api(self, host, path, **kwargs):
        if "/manifests/" in path:
            return Response(self.manifest, MANIFEST)
        if path.endswith(digest(self.config)):
            return Response(b"corrupted" if self.corrupt_config else self.config, CONFIG)
        if path.endswith(digest(self.layer)) and not self.missing_layer:
            return Response(self.layer, LAYER)
        raise RegistryError("blob unavailable", status=404)


class RegistryValidationTests(unittest.TestCase):
    def test_single_platform_arm_image_is_rejected(self):
        with self.assertRaisesRegex(RegistryError, "not linux/amd64"):
            FixtureRegistry("arm64").inspect("registry.example/app", "1.0.0")

    def test_missing_layer_cannot_produce_an_approvable_artifact(self):
        registry = FixtureRegistry()
        registry.missing_layer = True
        with self.assertRaisesRegex(RegistryError, "unavailable"):
            registry.inspect("registry.example/app", "1.0.0")

    def test_corrupt_config_is_rejected(self):
        registry = FixtureRegistry()
        registry.corrupt_config = True
        with self.assertRaisesRegex(RegistryError, "digest mismatch"):
            registry.inspect("registry.example/app", "1.0.0")

    def test_immutable_reference_cannot_resolve_to_different_bytes(self):
        with self.assertRaisesRegex(RegistryError, "requested manifest digest mismatch"):
            FixtureRegistry().inspect("registry.example/app", "sha256:" + "0" * 64)

    def test_ambiguous_platform_is_rejected(self):
        document = {"manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:" + digit * 64}
            for digit in ("1", "2")
        ]}
        with self.assertRaises(RegistryError):
            Registry._select_amd64(document, "registry.example/app", "1.0.0")

    def test_cpu_track_excludes_accelerators_and_prereleases(self):
        policy = {"tag_pattern": r"v(?P<version>3\.\d+\.\d+)"}
        selected = stable_tags(policy, ["v3.3.0", "v3.3.0-openvino", "v3.4.0-rc.1", "v3.2.4"])
        self.assertEqual([version for _, _, version in selected], ["3.3.0", "3.2.4"])


if __name__ == "__main__":
    unittest.main()
