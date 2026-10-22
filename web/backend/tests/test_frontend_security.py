import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class FrontendSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dist = root / "dist"
        self.dist.mkdir()
        (self.dist / "index.html").write_text("<html>SSL Sync</html>")
        (self.dist / "favicon.svg").write_text("<svg></svg>")
        (root / "private-key.pem").write_text("PRIVATE KEY MUST NOT BE SERVED")
        (self.dist / "linked-key.pem").symlink_to(root / "private-key.pem")
        # main creates a default application on import; isolate all its paths.
        with patch.dict(os.environ, {
            "SSL_SYNC_DATA_DIR": str(root / "data"),
            "SSL_SYNC_LOG_DIR": str(root / "logs"),
            "SSL_SYNC_DB_PATH": str(root / "data" / "test.db"),
            "SSL_SYNC_RUNTIME_CONFIG_DIR": str(root / "config"),
            "SSL_SYNC_RUNTIME_TMP_DIR": str(root / "runtime"),
            "SSL_SYNC_FRONTEND_DIST": str(self.dist),
        }):
            mount_frontend = importlib.import_module("app.main")._mount_frontend
        app = FastAPI()
        mount_frontend(app, self.dist)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_encoded_parent_paths_cannot_read_private_files(self):
        for path in ("/%2e%2e/private-key.pem", "/..%2fprivate-key.pem", "/%2f..%2fprivate-key.pem"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("PRIVATE KEY", response.text)

    def test_symlink_outside_frontend_is_rejected(self):
        response = self.client.get("/linked-key.pem")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("PRIVATE KEY", response.text)

    def test_static_files_and_spa_routes_still_work(self):
        self.assertEqual(self.client.get("/favicon.svg").text, "<svg></svg>")
        self.assertEqual(self.client.get("/domains/example.com").text, "<html>SSL Sync</html>")
        self.assertEqual(self.client.get("/api/missing").status_code, 404)
