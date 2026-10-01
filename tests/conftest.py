import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture(scope="session")
def screenplay(tmp_path_factory):
    from fixtures import make_screenplay
    path = tmp_path_factory.mktemp("script") / "scene.pdf"
    make_screenplay(path)
    return path


@pytest.fixture(scope="session")
def project(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    from fixtures import make_project
    return make_project(tmp_path_factory.mktemp("project"))
