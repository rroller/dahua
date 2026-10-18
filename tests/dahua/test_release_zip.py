"""Release inputs stay literal and HACS archives have the expected layout."""

import os
from pathlib import Path
import subprocess
import zipfile

import pytest
import yaml


def _steps():
    workflow = Path(__file__).parents[2] / ".github/workflows/release.yml"
    return yaml.safe_load(workflow.read_text())["jobs"]["zip"]["steps"]


def test_release_tag_is_data_rather_than_shell_code(tmp_path):
    marker = tmp_path / "unexpected-command"
    output = tmp_path / "output"
    tag = "rc.$(touch${IFS}" + str(marker) + ")"
    subprocess.run(
        ["bash", "-eu", "-c", _steps()[0]["run"]],
        env={**os.environ, "RELEASE_TAG": tag, "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    assert not marker.exists()
    assert output.read_text() == f"tag={tag}\n"


@pytest.mark.parametrize("tag", ["invalid\ntag", "../main"])
def test_invalid_tag_cannot_add_workflow_outputs(tmp_path, tag):
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-eu", "-c", _steps()[0]["run"]],
        env={**os.environ, "RELEASE_TAG": tag, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
    )
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("layout", ["valid", "nested", "compiled", "no-translations"])
def test_release_archive_guard_checks_hacs_layout(tmp_path, layout):
    prefix = "custom_components/dahua/" if layout == "nested" else ""
    with zipfile.ZipFile(tmp_path / "dahua.zip", "w") as archive:
        for name in ("manifest.json", "__init__.py", "const.py"):
            archive.writestr(prefix + name, "{}")
        if layout != "no-translations":
            archive.writestr(prefix + "translations/en.json", "{}")
        if layout == "compiled":
            archive.writestr("__pycache__/test.pyc", b"compiled")
    guard = next(step["run"] for step in _steps() if "Refuse to upload" in step["name"])
    result = subprocess.run(
        ["bash", "-eu", "-c", guard], cwd=tmp_path, capture_output=True
    )
    assert (result.returncode == 0) is (layout == "valid"), result.stdout
