"""Test fixtures for USDB Importer."""
import sys
import os
import tempfile
import shutil
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)


@pytest.fixture
def tmp_output_dir():
    d = tempfile.mkdtemp(prefix="usdb_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)
