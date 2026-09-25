"""Run docker/entrypoint.sh with a fake ``python`` that prints its argv."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


@pytest.fixture
def run_entrypoint(tmp_path):
    """``run_entrypoint(*args, env={})`` -> (argv the fake python received, completed process)."""
    if shutil.which("bash") is None:
        pytest.skip("bash is not available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python"
    fake.write_text("#!/bin/sh\nprintf 'ARG:%s\\n' \"$@\"\n")
    fake.chmod(0o755)

    def run(*args: str, env: dict | None = None):
        environ = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        environ.pop("PORT", None)
        environ.update(env or {})
        proc = subprocess.run(
            ["bash", str(ENTRYPOINT), *args],
            capture_output=True, text=True, env=environ, timeout=30,
        )
        argv = [line[4:] for line in proc.stdout.splitlines() if line.startswith("ARG:")]
        return argv, proc

    return run
