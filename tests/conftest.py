"""Make the repo root importable (sandbox.py and the cli/ package)."""

import shutil
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# In-repo scratch space (project rule: no paths outside the repo, so pytest's
# default /tmp-based tmp_path is off the table). Short names on purpose: unix
# socket paths made under here must stay below the ~108-byte AF_UNIX limit.
SCRATCH_ROOT = REPO_ROOT / "tests" / ".scratch"


@pytest.fixture
def scratch_dir():
    SCRATCH_ROOT.mkdir(exist_ok=True)
    d = SCRATCH_ROOT / uuid.uuid4().hex[:8]
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)
