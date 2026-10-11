"""Configure isolated paths before any API test uses the application factory.

Works for direct ``python -m unittest backend.test_app`` as well as the runner.
All generated catalog, legacy imports, preferences, secrets and decisions live in
one temporary tree. The runner additionally denies network connections.
"""

import atexit
import importlib
import os
import tempfile
from pathlib import Path

from tests.fixtures.catalog import write_catalog, write_imports

_environment = tempfile.TemporaryDirectory(prefix="maimai-tests-")
ROOT = Path(_environment.name)
write_catalog(ROOT / "data/catalog")
IMPORTS = write_imports(ROOT / "imports")
(ROOT / "data/assets").mkdir()
os.environ["MAIMAI_RUNTIME_DIR"] = str(ROOT / "global-runtime")
os.environ["MAIMAI_IMPORT_DIR"] = str(IMPORTS)
os.environ["MAIMAI_CATALOG_DIR"] = str(ROOT / "data/catalog")
os.environ["MAIMAI_ASSETS_DIR"] = str(ROOT / "data/assets")
os.environ["MAIMAI_FRONTEND_DIR"] = str(ROOT / "frontend/dist")
os.environ["MAIMAI_ENV"] = "test"
os.environ["DIVINGFISH_IMPORT_TOKEN"] = ""

_store = importlib.import_module("backend.store")
_store.ROOT = ROOT
_app = importlib.import_module("backend.app")
create_app = _app.create_app
atexit.register(_environment.cleanup)
