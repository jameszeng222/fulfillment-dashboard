"""Check the hub launcher with temporary paths and no production data/config."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import main
from app.datapaths import fact_database, operations_database
from deploy import run_site


class HubDeploymentTest(unittest.TestCase):
    def test_config_disables_local_access_and_resolves_data_paths(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "OPS_DATA_DIR": folder, "OPS_LOCAL_LOGIN": "true", "FULFILLMENT_OPEN_TEST": "true",
            "FULFILLMENT_PUBLIC_PREVIEW": "true", "FULFILLMENT_CLOUD_TICKETS": "true",
        }, clear=True):
            run_site.configure()
            self.assertEqual(os.environ["FULFILLMENT_HUB_SITE"], "true")
            for name in ("OPS_LOCAL_LOGIN", "FULFILLMENT_OPEN_TEST", "FULFILLMENT_PUBLIC_PREVIEW", "FULFILLMENT_CLOUD_TICKETS"):
                self.assertEqual(os.environ[name], "false")
            self.assertEqual(fact_database(), Path(folder).resolve() / "履约单.duckdb")
            self.assertEqual(operations_database(), Path(folder).resolve() / "ops.db")
            path = Path(folder) / "alternate.db"
            os.environ["OPS_OPS_DB"] = str(path)
            self.assertEqual(operations_database(), path.resolve())
            path.touch()
            with patch.object(run_site.subprocess, "run") as spawn:
                run_site.init_database()
                spawn.assert_not_called()

    def test_tls_uses_only_injected_paths(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            run_site.configure()
            self.assertEqual(fact_database().parent, run_site.ROOT / "data")
            with self.assertRaises(SystemExit):
                run_site.launch_args()
            cert, key = (Path(folder) / name for name in ("test-cert.pem", "test-key.pem"))
            cert.touch()
            key.touch()
            os.environ.update(ICS_TLS_CERT_FILE=str(cert), ICS_TLS_KEY_FILE=str(key))
            args = run_site.launch_args()
            self.assertIn("--no-proxy-headers", args)
            self.assertIn("--no-access-log", args)
            self.assertEqual(args[-4:], ["--ssl-certfile", str(cert), "--ssl-keyfile", str(key)])

    def test_lifespan_runs_startup_hook(self):
        async def enter():
            async with main.lifespan(main.app):
                pass

        with patch.object(main, "_startup") as startup:
            asyncio.run(enter())
            startup.assert_called_once_with()
