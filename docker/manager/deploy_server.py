#!/usr/bin/env python3
"""Internal-only HTTP server: pulls/runs strategy images, and exposes status
+ live logs for everything in the stack.

Not published to the host — only reachable from other containers on the
compose network (i.e. the webapp). This is the one place in the stack that
turns "an image reference from the webapp" into an actual running
container, so every deploy request is validated against ALLOWED_IMAGE_PREFIX
before touching docker.sock.

Endpoints:
    GET  /deployed        -> list custom-deployed containers
    POST /deploy           {"name": str, "image": str, "env": {...}}
    POST /stop/<name>
    GET  /status           -> every compose service + custom-deployed container
    GET  /logs/<name>      -> SSE stream of `docker logs -f` (compose service or
                               custom-deployed container, resolved by name)
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess  # nosec B404 - this whole file exists to run docker commands
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_DIR = "/state/custom"
COMPOSE_DIR = "/workspace"
ALLOWED_IMAGE_PREFIX = os.environ.get(
    "ALLOWED_IMAGE_PREFIX", "ghcr.io/eth-quant-research-group/"
).lower()
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "infra-net")
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
MODES = ("backtest", "paper", "live")
_DEFAULT_MODE = "paper"  # safer default than "live" if the field is omitted

# Passed through to every deployed container so it can reach the rest of
# the stack the same way the built-in strategy-* services do.
_PASSTHROUGH_ENV = ["NATS_URL", "BYBIT_API_KEY", "BYBIT_API_SECRET", "BYBIT_DEMO"]


def _state_path(name: str) -> str:
    return os.path.join(STATE_DIR, f"{name}.json")


def _container_name(name: str) -> str:
    return f"infra-custom-{name}"


def _run(
    cmd: list[str], timeout: int = 120, cwd: str | None = None
) -> subprocess.CompletedProcess:
    # shell=False (the default) + a list of args, never a shell string built
    # from request input — image/name are already regex-validated by callers.
    return subprocess.run(  # nosec B603
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
    )


def _compose(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", *args], timeout=timeout, cwd=COMPOSE_DIR)


def _known_services() -> set[str]:
    result = _compose("config", "--services")
    if result.returncode != 0:
        return set()
    return {s.strip() for s in result.stdout.splitlines() if s.strip()}


def _custom_deployments() -> list[dict]:
    os.makedirs(STATE_DIR, exist_ok=True)
    deployed = []
    for fname in sorted(os.listdir(STATE_DIR)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(STATE_DIR, fname)) as f:
            deployed.append(json.load(f))
    return deployed


def _validate_deploy(
    body: dict,
) -> tuple[str, str, dict, str] | tuple[None, None, None, None]:
    """Returns (name, image, env, mode) or all-None if invalid.

    `mode` (backtest/paper/live) is a label only — nothing downstream
    branches on it yet. Stored and passed through as TRADING_MODE so a
    strategy image can read it later, once that distinction is actually
    implemented.
    """
    name = str(body.get("name", "")).strip()
    image = str(body.get("image", "")).strip()
    env = body.get("env") or {}
    mode = str(body.get("mode", "")).strip().lower() or _DEFAULT_MODE

    if not NAME_RE.match(name):
        return None, None, None, None
    if not image.lower().startswith(ALLOWED_IMAGE_PREFIX):
        return None, None, None, None
    if mode not in MODES:
        return None, None, None, None
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        return None, None, None, None
    return name, image, env, mode


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        print(f"[deploy_server] {self.address_string()} {fmt % args}")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/deployed":
            deployed = []
            for record in _custom_deployments():
                inspect = _run(
                    [
                        "docker",
                        "inspect",
                        "-f",
                        "{{.State.Status}}",
                        record["container"],
                    ]
                )
                record["status"] = (
                    inspect.stdout.strip() if inspect.returncode == 0 else "missing"
                )
                deployed.append(record)
            self._send_json(200, {"deployed": deployed})
            return
        if self.path == "/status":
            self._handle_status()
            return
        if self.path.startswith("/logs/"):
            self._handle_logs(self.path.removeprefix("/logs/"))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/deploy":
            self._handle_deploy()
            return
        if self.path.startswith("/stop/"):
            self._handle_stop(self.path.removeprefix("/stop/"))
            return
        self._send_json(404, {"error": "not found"})

    def _handle_status(self) -> None:
        services = []

        result = _compose("ps", "--all", "--format", "json")
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                if not line.strip():
                    continue
                try:
                    c = json.loads(line)
                except json.JSONDecodeError:
                    continue
                services.append(
                    {
                        "name": c.get("Service", ""),
                        "container": c.get("Name", ""),
                        "state": c.get("State", ""),
                        "status": c.get("Status", ""),
                        "image": c.get("Image", ""),
                        "kind": "compose",
                    }
                )

        for record in _custom_deployments():
            inspect = _run(
                ["docker", "inspect", "-f", "{{.State.Status}}", record["container"]]
            )
            state = inspect.stdout.strip() if inspect.returncode == 0 else "missing"
            services.append(
                {
                    "name": record["name"],
                    "container": record["container"],
                    "state": state,
                    "status": state,
                    "image": record["image"],
                    "kind": "custom",
                }
            )

        self._send_json(200, {"services": services})

    def _write_sse_chunk(self, line: str) -> None:
        data = f"data: {line}\n\n".encode()
        self.wfile.write(f"{len(data):X}\r\n".encode())
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _end_sse(self) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(b"0\r\n\r\n")

    def _emit_event(self, event: str, **fields: object) -> None:
        """Structured line, distinguished from raw command output by prefix
        so the frontend can special-case error/running transitions."""
        self._write_sse_chunk("__EVENT__" + json.dumps({"event": event, **fields}))

    def _stream_command(self, cmd: list[str], cwd: str | None = None) -> int:
        """Runs cmd, relaying stdout/stderr line-by-line as SSE chunks.
        Returns the exit code."""
        self._write_sse_chunk(f"$ {' '.join(cmd)}")
        proc = subprocess.Popen(  # nosec B603 - shell=False, list args, see _run() above
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd
        )
        for line in iter(proc.stdout.readline, ""):
            self._write_sse_chunk(line.rstrip("\n"))
        proc.wait()
        return proc.returncode

    def _handle_logs(self, name: str) -> None:
        if not NAME_RE.match(name):
            self._send_json(400, {"error": "invalid name"})
            return

        custom_path = _state_path(name)
        if os.path.exists(custom_path):
            with open(custom_path) as f:
                container = json.load(f)["container"]
            cmd = ["docker", "logs", "-f", "--tail", "200", container]
            cwd = None
        elif name in _known_services():
            cmd = [
                "docker",
                "compose",
                "logs",
                "-f",
                "--tail",
                "200",
                "--no-color",
                name,
            ]
            cwd = COMPOSE_DIR
        else:
            self._send_json(404, {"error": "unknown service"})
            return

        self._start_sse()

        proc = subprocess.Popen(  # nosec B603 - shell=False, list args, see _run() above
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=cwd
        )
        try:
            for line in iter(proc.stdout.readline, ""):
                self._write_sse_chunk(line.rstrip("\n"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            self._end_sse()

    def _handle_deploy(self) -> None:
        try:
            body = self._read_json()
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid JSON body"})
            return

        name, image, env, mode = _validate_deploy(body)
        if name is None:
            self._send_json(
                400,
                {
                    "error": "invalid name, image, or mode",
                    "detail": f"image must start with {ALLOWED_IMAGE_PREFIX!r}; "
                    "name must be alnum/dash/underscore, max 64 chars; "
                    f"mode must be one of {MODES}",
                },
            )
            return

        container = _container_name(name)
        os.makedirs(STATE_DIR, exist_ok=True)

        # From here on this is a live progress stream, not a single JSON
        # response — docker pull on a several-hundred-MB image can take a
        # while, and the caller (the /internal Deploy page) wants to show
        # that happening rather than a spinner with zero information.
        self._start_sse()
        try:
            # Redeploy semantics: stop+remove any previous container for this name.
            self._stream_command(["docker", "stop", container])
            self._stream_command(["docker", "rm", container])

            if self._stream_command(["docker", "pull", image]) != 0:
                self._emit_event("error", detail="docker pull failed")
                return

            run_cmd = [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--network",
                DOCKER_NETWORK,
                "--restart",
                "unless-stopped",
                "--label",
                "infra.managed=custom",
                "--label",
                f"infra.deploy.name={name}",
                "--label",
                f"infra.deploy.mode={mode}",
            ]
            for key in _PASSTHROUGH_ENV:
                val = os.environ.get(key)
                if val:
                    run_cmd += ["-e", f"{key}={val}"]
            run_cmd += ["-e", f"TRADING_MODE={mode}"]
            # Ties the strategy's own NATS identity to the deploy name the
            # admin chose here, so the consolidator's guard config (which is
            # keyed by this same name, see StrategyGuardRegistry) always
            # lines up with whatever strategy_id this container actually
            # publishes under — see workers/strategy_worker.py.
            run_cmd += ["-e", f"STRATEGY_ID={name}"]
            for key, val in env.items():
                run_cmd += ["-e", f"{key}={val}"]
            run_cmd.append(image)

            if self._stream_command(run_cmd) != 0:
                self._emit_event("error", detail="docker run failed — see output above")
                return

            record = {
                "name": name,
                "image": image,
                "container": container,
                "env": env,
                "mode": mode,
                "created_at": datetime.now(UTC).isoformat(),
            }
            with open(_state_path(name), "w") as f:
                json.dump(record, f)

            self._emit_event("running", **record)

            # Seamlessly continue into the container's own startup output —
            # same live feed, no reconnect needed on the frontend side.
            log_proc = subprocess.Popen(  # nosec B603 B607
                ["docker", "logs", "-f", container],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                for line in iter(log_proc.stdout.readline, ""):
                    self._write_sse_chunk(line.rstrip("\n"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                log_proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    log_proc.wait(timeout=5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._end_sse()

    def _handle_stop(self, name: str) -> None:
        if not NAME_RE.match(name) or not os.path.exists(_state_path(name)):
            self._send_json(404, {"error": "unknown deployment"})
            return

        container = _container_name(name)
        _run(["docker", "stop", container])
        _run(["docker", "rm", container])
        os.remove(_state_path(name))
        self._send_json(200, {"status": "stopped", "name": name})


def main() -> None:
    port = int(os.environ.get("DEPLOY_SERVER_PORT", "9000"))
    os.makedirs(STATE_DIR, exist_ok=True)
    # Binding all interfaces is intentional: this container is never
    # published to the host (see docker-compose.yml) — only reachable from
    # other containers on the compose network, which requires 0.0.0.0.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # nosec B104
    print(
        f"[deploy_server] listening on :{port}, allowed prefix={ALLOWED_IMAGE_PREFIX!r}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
