import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


quadlet = load("quadlet_metrics", "quadlet-metrics.py")
smart = load("smart_metrics", "smart-metrics.py")


class RoleMetricTests(unittest.TestCase):
    def test_collector_excludes_other_role_cgroups_even_if_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "metrics/immich"
            output.mkdir(parents=True)
            owned = root / "cgroups/immich-server.service"
            foreign = root / "cgroups/nginx.service"
            for unit, memory in ((owned, "17"), (foreign, "999")):
                unit.mkdir(parents=True)
                (unit / "memory.current").write_text(memory)
                (unit / "cpu.stat").write_text("usage_usec 1000000\n")
            with patch.object(quadlet, "current_principal", return_value="immich"), \
                 patch.object(quadlet, "principal", return_value={"uid": os.getuid(), "units": ["immich-server.service"]}), \
                 patch.object(quadlet, "manifest", return_value={"metricsDirectory": str(root / "metrics")}), \
                 patch.object(quadlet, "podman_units", return_value={"immich_server": "immich-server.service"}), \
                 patch.object(Path, "glob", return_value=[owned, foreign]):
                quadlet.main()
            text = (output / "quadlet.prom").read_text()
            self.assertIn('unit="immich-server.service",stack="immich"', text)
            self.assertNotIn("nginx", text)
            self.assertNotIn("999", text)
            self.assertEqual((output / "quadlet.prom").stat().st_mode & 0o777, 0o644)

    def test_smart_refuses_application_identity_before_contacting_exporter(self):
        with patch.object(smart, "current_principal", return_value="alloy"), \
             patch.object(smart.urllib.request, "urlopen") as request:
            with self.assertRaisesRegex(RuntimeError, "metrics account"):
                smart.main()
            request.assert_not_called()

    def test_failed_smart_collection_preserves_previous_textfile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "metrics"
            output.mkdir()
            previous = output / "smart.prom"
            previous.write_text("previous healthy metrics\n")
            with patch.object(smart, "current_principal", return_value="metrics"), \
                 patch.object(smart, "manifest", return_value={"metricsDirectory": str(root)}), \
                 patch.object(smart.urllib.request, "urlopen", side_effect=TimeoutError("fixture timeout")):
                self.assertEqual(smart.main(), 1)
            self.assertEqual(previous.read_text(), "previous healthy metrics\n")


if __name__ == "__main__":
    unittest.main()
