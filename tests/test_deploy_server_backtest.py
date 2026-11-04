"""The Deploy page's backtest stream: a page closed mid-run must not leave the
backtest container running."""

from __future__ import annotations

import importlib.util
import io
import subprocess
from pathlib import Path
from typing import Any

import pytest

_PATH = Path(__file__).resolve().parents[1] / "docker" / "manager" / "deploy_server.py"
_spec = importlib.util.spec_from_file_location("deploy_server", _PATH)
assert _spec is not None and _spec.loader is not None
deploy_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_server)


class _Proc:
    """A `docker run` that prints *lines*, then keeps running until stopped."""

    def __init__(self, lines: list[str], exits: bool) -> None:
        self.stdout = io.StringIO("".join(lines))
        self.returncode: int | None = 0 if exits else None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("docker", timeout or 0)
        return self.returncode


class _Handler:
    """Just enough of the request handler for _stream_backtest."""

    _stream_backtest = deploy_server.Handler._stream_backtest

    def __init__(self, fail_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.fail_after = fail_after

    def _write_sse_chunk(self, line: str) -> None:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise BrokenPipeError
        self.sent.append(line)


def _patch(monkeypatch: pytest.MonkeyPatch, proc: _Proc) -> list[list[str]]:
    ran: list[list[str]] = []

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess:
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(deploy_server.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(deploy_server, "_run", run)
    return ran


def test_closed_page_removes_the_backtest_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _Proc(["step 1\n", "step 2\n", "step 3\n"], exits=False)
    ran = _patch(monkeypatch, proc)
    handler = _Handler(fail_after=2)  # the "$ docker run" line + one log line

    with pytest.raises(BrokenPipeError):
        handler._stream_backtest(["docker", "run"], "backtest-abc")

    assert ["docker", "rm", "-f", "backtest-abc"] in ran
    assert proc.terminated


def test_finished_backtest_keeps_the_result_and_removes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_line = f'{deploy_server._RESULT_PREFIX}{{"net_pnl": 1}}\n'
    proc = _Proc(["step 1\n", result_line], exits=True)
    ran = _patch(monkeypatch, proc)

    result, code = _Handler()._stream_backtest(["docker", "run"], "backtest-abc")

    assert (result, code) == ({"net_pnl": 1}, 0)
    assert ran == []
    assert not proc.terminated


def test_backtest_container_is_named() -> None:
    cmd = deploy_server._backtest_cmd("img", {}, ["--offline"], "backtest-abc")
    assert cmd[cmd.index("--name") + 1] == "backtest-abc"
