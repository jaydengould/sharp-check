import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("make_app", Path(__file__).parent.parent / "scripts" / "make_app.py")
make_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_app)

REPO, PY = Path("/Users/someone/sharp-check"), Path("/Users/someone/sharp-check/.venv/bin/python")


def source(repo=REPO):
    return make_app.swift_source(repo, PY)


def test_app_reuses_running_server():
    s = source()
    assert s.index("if !healthy()") < s.index("sh(launch)")


def test_app_quit_kills_only_its_port():
    s = source()
    assert "func applicationWillTerminate(_ n: Notification) { sh(kill) }" in s
    assert "tcp:8766" in make_app.KILL and "8050" not in s and "8765" not in s


def test_app_binds_localhost_on_its_port():
    assert "HOST=127.0.0.1 PORT=8766" in make_app.launch_command(REPO, PY)


def test_swift_compiles_with_awkward_paths(tmp_path):
    s = source(Path('/tmp/a b/"q"\\(x)'))  # quotes, a backslash, and Swift's interpolation marker
    if shutil.which("swiftc") is None:
        pytest.skip("swiftc not available")
    src = tmp_path / "main.swift"
    src.write_text(s)
    subprocess.run(["swiftc", "-typecheck", str(src)], check=True, capture_output=True)


def test_launch_returns_immediately_with_stdout_piped(tmp_path):
    # The app waits for this shell to exit; a launch that keeps it open hangs the app forever.
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\nsleep 20\n")
    fake.chmod(0o755)
    proc = subprocess.Popen(["/bin/sh", "-c", make_app.launch_command(tmp_path, fake)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proc.communicate(timeout=5)
    finally:
        subprocess.run(["pkill", "-f", str(fake)])
    assert proc.returncode == 0
