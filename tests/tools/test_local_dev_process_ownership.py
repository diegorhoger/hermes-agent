"""Local-dev process ownership, negative-proof tests (dispatch 141, P1).

The start/stop scripts must never adopt or signal a process they cannot
verify as this development environment's own synth provider. These tests
spawn real unrelated processes and real (foreign) synth-provider copies and
prove:

* startup refuses to touch a foreign process holding the provider port and
  signals nothing;
* a PID file naming a live but foreign PID is never accepted as healthy and
  never killed;
* a PID file whose start identity (lstart) does not match is never accepted
  (PID-reuse guard);
* a synth_provider.py at a *different path* is never adopted (exact
  development-root identity);
* stop never kills an unrelated process — foreign process survives;
* a stale PID file (dead PID) is removed and startup proceeds cleanly;
* the happy path starts, verifies ownership end to end, and stops — ports
  freed, nothing foreign involved.

Tests skip when the fixed provider ports (8401/8402) are held by anything —
they are hardcoded in the scripts and must not be fought over.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LOCAL_DEV = REPO / "tools" / "local-dev"
PORT_A, PORT_B = 8401, 8402
AUTOROUTER_SRC = Path(os.environ.get("AUTOROUTER_SRC", str(REPO.parent / "autorouter")))

pytestmark = pytest.mark.skipif(
    shutil.which("lsof") is None or shutil.which("curl") is None,
    reason="ownership verification requires lsof and curl")


def _port_busy(port: int) -> bool:
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True, check=False)
    return bool(out.stdout.strip())


def _wait_port(port: int, want_busy: bool, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_busy(port) == want_busy:
            return True
        time.sleep(0.1)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # A zombie is not alive for our purposes.
    out = subprocess.run(["ps", "-p", str(pid), "-o", "state="],
                         capture_output=True, text=True, check=False)
    return "Z" not in out.stdout


class _Scratch:
    pass


@pytest.fixture
def scratch(tmp_path):
    """A scratch HERMES_DEV_ROOT (config, home, runtime) for the scripts."""
    if _port_busy(PORT_A) or _port_busy(PORT_B):
        pytest.skip("provider ports 8401/8402 are held — cannot run ownership tests")
    root = tmp_path / "devroot"
    home = root / "hermes-home"
    runtime = root / "runtime"
    root.mkdir()
    home.mkdir()
    runtime.mkdir()
    # Isolated profile with the AutoRouter plugin symlinked, mirroring the
    # developer environment setup (loopback-only, dummy credentials).
    plugins = home / "plugins"
    plugins.mkdir()
    if AUTOROUTER_SRC.is_dir():
        os.symlink(AUTOROUTER_SRC, plugins / "autorouter",
                   target_is_directory=True)
    (home / "config.yaml").write_text(
        "model:\n  provider: ollama\n  default: ornith-1.5:9b\n"
        "  base_url: http://127.0.0.1:11434/v1\n"
        "providers:\n"
        "  ollama:\n    base_url: http://127.0.0.1:11434/v1\n    api_key: sk-ollama-local-dummy-not-a-secret\n"
        "  locala:\n    base_url: http://127.0.0.1:8401/v1\n    api_key: sk-local-a-dummy-not-a-secret\n"
        "  localb:\n    base_url: http://127.0.0.1:8402/v1\n    api_key: sk-local-b-dummy-not-a-secret\n"
        "plugins:\n"
        "  enabled: [autorouter]\n"
        "  entries:\n"
        "    autorouter:\n"
        "      settings:\n"
        "        enabled: true\n"
        "        enable_chat_router: true\n"
        "        enable_native_routing: true\n"
        "        free_only: true\n"
        "        providers: [ollama, locala, localb]\n"
        "        requires_tools: false\n"
        "        min_context: 2048\n"
        "        cooldown_seconds: 2\n"
        "        policy: auto\n"
        "        model_preference:\n"
        "          mode: allowlist\n"
        "          include:\n"
        "            - ollama/ornith-1.5:9b\n"
        "            - locala/local-model-a\n"
        "            - localb/local-model-b\n",
        encoding="utf-8")
    env = {
        "HERMES_DEV_ROOT": str(root),
        "HERMES_DEV_HOME": str(home),
        "HERMES_DEV_RUNTIME": str(runtime),
        "AUTOROUTER_SRC": str(AUTOROUTER_SRC),
        "SPAWN_PID_WAIT_S": "8",
        "PATH": os.environ.get("PATH", ""),
    }
    scratch = _Scratch()
    scratch.root, scratch.home, scratch.runtime, scratch.env = root, home, runtime, env
    return scratch


def _run(script: str, env: dict, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(LOCAL_DEV / script)],
        capture_output=True, text=True, env={**os.environ, **env}, timeout=timeout,
        check=False)


def _spawn(args: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, **kwargs)


def _lstart(pid: int) -> str:
    out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                         capture_output=True, text=True, check=False)
    return " ".join(out.stdout.split())


def _write_pid_file(path: Path, pid: int, name: str, port: int, model: str,
                    lstart: str | None = None) -> None:
    token = lstart if lstart is not None else _lstart(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "hermes-local-dev-provider v1\n"
        f"pid {pid}\n"
        f"started {token}\n"
        f"name {name}\n"
        f"port {port}\n"
        f"model {model}\n",
        encoding="utf-8")


@pytest.fixture
def foreign_server():
    """An unrelated process (python's http.server) on a port of our choosing."""
    proc = _spawn([sys.executable, "-m", "http.server", str(PORT_A),
                   "--bind", "127.0.0.1"])
    if not _wait_port(PORT_A, True, timeout=10):
        proc.terminate()
        pytest.skip("could not bind foreign http.server on 8401")
    yield proc
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_startup_refuses_foreign_process_and_signals_nothing(scratch, foreign_server):
    foreign_pid = foreign_server.pid
    result = _run("start-local.sh", scratch.env)
    assert result.returncode != 0
    assert _pid_alive(foreign_pid), "foreign process was signalled or died"
    # And no pid file was created adopting it.
    locala_pid = scratch.runtime / "pids" / "locala.pid"
    assert not locala_pid.exists() or locala_pid.read_text().split()[1] != str(foreign_pid)


def test_stop_never_kills_an_unrelated_process(scratch, foreign_server):
    foreign_pid = foreign_server.pid
    _write_pid_file(scratch.runtime / "pids" / "locala.pid",
                    foreign_pid, "locala", PORT_A, "local-model-a")
    result = _run("stop-local.sh", scratch.env)
    assert result.returncode == 3
    assert _pid_alive(foreign_pid), "stop-local killed an unrelated process"


def test_pid_file_with_recycled_start_identity_is_never_accepted(scratch):
    """A real synth provider on 8401, but the pid file carries the WRONG lstart
    (as it would for a recycled PID): refuse, never signal."""
    proc = _spawn([sys.executable, str(LOCAL_DEV / "synth_provider.py"),
                   "locala", str(PORT_A), "local-model-a"],
                  env={**os.environ, "PROVIDER_LOG_DIR": str(scratch.runtime / "provider-logs")})
    try:
        assert _wait_port(PORT_A, True)
        _write_pid_file(scratch.runtime / "pids" / "locala.pid",
                        proc.pid, "locala", PORT_A, "local-model-a",
                        lstart="Mon Jan  1 00:00:00 1990")
        result = _run("stop-local.sh", scratch.env)
        assert result.returncode == 3
        assert _pid_alive(proc.pid), "recycled-pid guard: provider was signalled"
        start = _run("start-local.sh", scratch.env)
        assert start.returncode != 0
        assert _pid_alive(proc.pid), "start-local signalled a process it could not verify"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_synth_provider_at_a_foreign_path_is_never_adopted(scratch):
    """A synth_provider.py at a different path with identical argv is NOT ours."""
    elsewhere = scratch.runtime / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(LOCAL_DEV / "synth_provider.py", elsewhere / "synth_provider.py")
    proc = _spawn([sys.executable, str(elsewhere / "synth_provider.py"),
                   "locala", str(PORT_A), "local-model-a"])
    try:
        assert _wait_port(PORT_A, True)
        _write_pid_file(scratch.runtime / "pids" / "locala.pid",
                        proc.pid, "locala", PORT_A, "local-model-a")
        result = _run("stop-local.sh", scratch.env)
        assert result.returncode == 3
        assert _pid_alive(proc.pid)
        start = _run("start-local.sh", scratch.env)
        assert start.returncode != 0
        assert _pid_alive(proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_stale_pid_file_is_removed_and_startup_proceeds(scratch):
    """A pid that no longer exists: the pid file is stale garbage — removed,
    nothing signalled, startup proceeds and the provider comes up."""
    _write_pid_file(scratch.runtime / "pids" / "locala.pid",
                    2**22 - 1, "locala", PORT_A, "local-model-a",
                    lstart="Mon Jan  1 00:00:00 1990")
    try:
        result = _run("start-local.sh", scratch.env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (scratch.runtime / "pids" / "locala.pid").exists() or \
            (scratch.runtime / "pids" / "locala.pid").read_text().count("hermes-local-dev-provider") == 1
    finally:
        _run("stop-local.sh", scratch.env)


def test_garbage_pid_file_signals_nothing(scratch, foreign_server):
    (scratch.runtime / "pids").mkdir(parents=True, exist_ok=True)
    (scratch.runtime / "pids" / "locala.pid").write_text("not a pid file\n", encoding="utf-8")
    result = _run("stop-local.sh", scratch.env)
    assert result.returncode == 3
    assert _pid_alive(foreign_server.pid)


def test_owned_happy_path_start_then_stop_frees_ports(scratch):
    try:
        start = _run("start-local.sh", scratch.env)
        assert start.returncode == 0, start.stdout + start.stderr
        for port in (PORT_A, PORT_B):
            assert _wait_port(port, True)
        # The pid files are ours and well-formed.
        for name in ("locala", "localb"):
            content = (scratch.runtime / "pids" / f"{name}.pid").read_text(encoding="utf-8")
            assert content.startswith("hermes-local-dev-provider v1\n")
        stop = _run("stop-local.sh", scratch.env)
        assert stop.returncode == 0, stop.stdout + stop.stderr
        for port in (PORT_A, PORT_B):
            assert _wait_port(port, False), f"port {port} still busy after verified stop"
    finally:
        _run("stop-local.sh", scratch.env)
