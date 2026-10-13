from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


secrets = load_module("creds_test_secrets", "homelab-secrets.py")
with patch.dict(sys.modules, {"homelab_secrets": secrets}):
    creds = load_module("creds_test_editor", "creds.py")


class CredentialLifecycleTests(unittest.TestCase):
    def test_check_neither_stages_runtime_files_nor_changes_service_lifecycle(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("readonly check attempted a mutation")
        with patch.object(creds.os, "getuid", return_value=1000), \
                patch.object(creds.os, "geteuid", return_value=1000), \
                patch.object(creds, "manifest", return_value={"operator": {"uid": 1000}}), \
                patch.object(secrets, "load_fields", return_value={"tunnel-token": "original-fixture-tunnel"}), \
                patch.object(creds, "runtime_root", side_effect=forbidden), \
                patch.object(creds, "systemctl", side_effect=forbidden), \
                patch.object(secrets, "component_lock", side_effect=forbidden), \
                patch.object(secrets, "persist", side_effect=forbidden), \
                patch.object(secrets, "store_fields", side_effect=forbidden), redirect_stdout(io.StringIO()):
            self.assertEqual(creds.main(["--check", "cloudflared"]), 0)

    def test_tree_restart_routes_two_accounts_and_preserves_manual_dev_inactivity(self):
        roles = {
            "tree-eclass": {"units": ["tree-eclass.service", "tree-eclass-migrate.service"]},
            "tree-dev": {"units": ["tree-eclass-dev.service", "tree-eclass-dev-migrate.service"]},
            "nginx": {"units": ["nginx.service"]},
        }
        states = {("tree-eclass", "tree-eclass.service"): "active",
                  ("tree-dev", "tree-eclass-dev.service"): "inactive",
                  ("nginx", "nginx.service"): "active"}
        operations = []
        def state(role, unit):
            return {"LoadState": "loaded", "UnitFileState": "enabled",
                    "ActiveState": "active" if unit == "homelab.target" else states[(role, unit)]}
        def control(role, action, unit):
            operations.append((role, action, unit))
            states[(role, unit)] = "active" if action == "start" else "inactive"
            return subprocess.CompletedProcess([], 0)
        with patch.object(creds, "principals_for_component", return_value=["tree-eclass", "tree-dev"]), \
                patch.object(creds, "principal", side_effect=roles.__getitem__), \
                patch.object(creds, "manifest", return_value={"principals": roles}), \
                patch.object(creds, "unit_state", side_effect=state), \
                patch.object(creds, "systemctl", side_effect=control):
            plan = creds.RestartPlan("tree-eclass")
            plan.stop()
            self.assertEqual(states[("tree-eclass", "tree-eclass.service")], "inactive")
            self.assertEqual(states[("nginx", "nginx.service")], "inactive")
            self.assertTrue(plan.resume())
        self.assertEqual(operations, [("nginx", "stop", "nginx.service"),
                                      ("tree-eclass", "stop", "tree-eclass.service"),
                                      ("tree-eclass", "start", "tree-eclass.service"),
                                      ("nginx", "start", "nginx.service")])
        self.assertEqual(states[("tree-dev", "tree-eclass-dev.service")], "inactive")

    def test_immich_restart_restores_the_complete_previously_running_family(self):
        units = ["immich-database.service", "immich-redis.service", "immich-machine-learning.service",
                 "immich-server.service", "immich-microservices.service"]
        roles = {"immich": {"units": units}}
        states = {unit: "active" for unit in units}
        operations = []
        def state(role, unit):
            self.assertEqual(role, "immich")
            return {"LoadState": "loaded", "UnitFileState": "enabled",
                    "ActiveState": "active" if unit == "homelab.target" else states[unit]}
        def control(role, action, unit):
            self.assertEqual(role, "immich")
            operations.append((action, unit))
            states[unit] = "active" if action == "start" else "inactive"
            return subprocess.CompletedProcess([], 0)
        with patch.object(creds, "principals_for_component", return_value=["immich"]), \
                patch.object(creds, "principal", side_effect=roles.__getitem__), \
                patch.object(creds, "manifest", return_value={"principals": roles}), \
                patch.object(creds, "unit_state", side_effect=state), \
                patch.object(creds, "systemctl", side_effect=control):
            plan = creds.RestartPlan("immich")
            plan.stop()
            self.assertTrue(plan.resume())
        self.assertEqual(operations, [("stop", unit) for unit in reversed(units)] + [("start", unit) for unit in units])
        self.assertEqual(set(states.values()), {"active"})


if __name__ == "__main__":
    unittest.main()
