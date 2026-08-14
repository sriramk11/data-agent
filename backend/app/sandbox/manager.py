"""Host-side sandbox lifecycle: start an isolated process per session, talk
to it over stdin/stdout with a hard timeout, tear it down when the session
ends or it stops responding.

Two backends:
  - DockerSandbox (default, SANDBOX_MODE=docker): `docker run` with
    --network none, a read-only root filesystem, a small tmpfs, a
    read-only bind mount of the dataset, a read-write bind mount for chart
    output only, memory/cpu/pids limits, and a non-root user. This is the
    real security boundary.
  - LocalSandbox (SANDBOX_MODE=local): runs runner_server.py as a plain
    subprocess with resource limits (rlimit) instead of a container. Handy
    for local dev on a machine without Docker installed, but it shares the
    host kernel -- do not point it at untrusted data or treat it as a real
    isolation boundary. Logged loudly on startup so it's never silently
    used in place of the real thing.
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from app import config

logger = logging.getLogger("sandbox")

RUNNER_SCRIPT = Path(__file__).with_name("runner_server.py")


class SandboxTimeout(Exception):
    pass


class SandboxCrashed(Exception):
    pass


class SandboxSession:
    """Common interface used by the orchestrator regardless of backend."""

    def __init__(self, session_id: str, data_dir: Path, output_dir: Path):
        self.session_id = session_id
        self.data_dir = data_dir
        self.output_dir = output_dir
        self.proc: Optional[subprocess.Popen] = None
        self._out_q: "queue.Queue[str]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # -- paths the generated code should reference ----------------------
    # DockerSandbox has these mounted at a fixed, container-internal /work
    # path, identical for every session. LocalSandbox has no chroot, so it
    # has to tell the model the *real* host path instead -- that's the
    # unavoidable cost of the dev-only fallback not being a real sandbox.
    def data_path(self, filename: str) -> str:
        raise NotImplementedError

    @property
    def output_dir_path(self) -> str:
        raise NotImplementedError

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        cmd = self._build_cmd()
        logger.info("sandbox[%s] starting: %s", self.session_id, " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._reader_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader_thread.start()

    def _build_cmd(self) -> list[str]:
        raise NotImplementedError

    def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._out_q.put(line)
        self._out_q.put("")  # sentinel: stdout closed, process exited

    def exec(self, code: str, timeout: int = config.SANDBOX_EXEC_TIMEOUT_S) -> dict:
        """Send code to the running sandbox and block for a result.

        Raises SandboxTimeout if nothing comes back in time, or
        SandboxCrashed if the underlying process has died. In either case
        the caller (orchestrator) is expected to treat this exactly like a
        code error: report it to the model as a failed tool_result rather
        than crashing the query.
        """
        if not self.proc or self.proc.poll() is not None:
            raise SandboxCrashed("sandbox process is not running")

        with self._lock:
            req_id = str(uuid.uuid4())
            payload = json.dumps({"id": req_id, "code": code, "timeout": timeout})
            assert self.proc.stdin
            try:
                self.proc.stdin.write(payload + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError) as e:
                raise SandboxCrashed(str(e))

            deadline = time.monotonic() + timeout + 5  # grace period over the in-container alarm
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._force_restart()
                    raise SandboxTimeout(f"no response from sandbox within {timeout + 5}s")
                try:
                    line = self._out_q.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line == "":
                    raise SandboxCrashed("sandbox process exited unexpectedly")
                try:
                    result = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if result.get("id") == req_id:
                    return result
                # stray output from a previous timed-out call; discard and keep waiting

    def _force_restart(self) -> None:
        logger.warning("sandbox[%s] unresponsive, killing and restarting", self.session_id)
        self.stop()
        self.start()

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


class DockerSandbox(SandboxSession):
    def data_path(self, filename: str) -> str:
        return f"/work/data/{filename}"

    @property
    def output_dir_path(self) -> str:
        return "/work/output"

    def _build_cmd(self) -> list[str]:
        container_name = f"data-agent-sbx-{self.session_id[:8]}"
        return [
            "docker", "run", "-i", "--rm",
            "--name", container_name,
            "--network", "none",
            "--memory", config.SANDBOX_MEMORY,
            "--cpus", config.SANDBOX_CPUS,
            "--pids-limit", config.SANDBOX_PIDS_LIMIT,
            "--read-only",
            "--tmpfs", "/tmp:size=64m",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "-v", f"{self.data_dir}:/work/data:ro",
            "-v", f"{self.output_dir}:/work/output:rw",
            "--user", "1000:1000",
            config.SANDBOX_IMAGE,
            "python3", "-u", "/app/runner_server.py",
        ]


class LocalSandbox(SandboxSession):
    """Dev-only fallback -- NOT an isolation boundary. See module docstring."""

    def data_path(self, filename: str) -> str:
        return str(self.data_dir / filename)

    @property
    def output_dir_path(self) -> str:
        return str(self.output_dir)

    def start(self) -> None:
        logger.warning(
            "sandbox[%s] running in SANDBOX_MODE=local -- generated code runs "
            "as a plain subprocess with rlimits, NOT inside Docker. Do not "
            "use with untrusted data.",
            self.session_id,
        )
        super().start()

    def _build_cmd(self) -> list[str]:
        return [
            sys.executable, "-u", str(_local_preexec_wrapper()),
            str(RUNNER_SCRIPT), str(self.data_dir), str(self.output_dir),
        ]


def _local_preexec_wrapper() -> Path:
    """A tiny bootstrap that sets rlimits before exec'ing runner_server.py,
    so the local fallback at least gets CPU-time / memory / process-count
    caps even without a container.
    """
    wrapper = Path(config.RUNTIME_DIR) / "_local_sandbox_bootstrap.py"
    wrapper.write_text(
        "import resource, sys, os\n"
        "# Best-effort only: e.g. RLIMIT_AS is notoriously unreliable on macOS\n"
        "# (the dyld shared cache alone can report more virtual address space\n"
        "# than any sane limit, so setrlimit raises 'current limit exceeds\n"
        "# maximum limit' before a single line of user code has run). A failed\n"
        "# rlimit must not take the whole sandbox down with it -- this mode is\n"
        "# a dev convenience, not the security boundary; skip what the host OS\n"
        "# won't allow rather than crashing on startup.\n"
        "for _lim, _val in (\n"
        "    (getattr(resource, 'RLIMIT_CPU', None), (30, 30)),\n"
        "    (getattr(resource, 'RLIMIT_AS', None), (1536 * 1024 * 1024, 1536 * 1024 * 1024)),\n"
        "    (getattr(resource, 'RLIMIT_NPROC', None), (64, 64)),\n"
        "):\n"
        "    if _lim is None:\n"
        "        continue\n"
        "    try:\n"
        "        resource.setrlimit(_lim, _val)\n"
        "    except (ValueError, OSError) as _e:\n"
        "        print(f'[local-sandbox] skipping rlimit {_lim}: {_e}', file=sys.stderr)\n"
        "runner, data_dir, output_dir = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "os.environ['SANDBOX_DATA_DIR'] = data_dir\n"
        "os.environ['SANDBOX_OUTPUT_DIR'] = output_dir\n"
        "import runpy\n"
        "sys.path.insert(0, os.path.dirname(runner))\n"
        "runpy.run_path(runner, run_name='__main__')\n"
    )
    return wrapper


def make_sandbox(session_id: str, data_dir: Path, output_dir: Path) -> SandboxSession:
    if config.SANDBOX_MODE == "docker":
        return DockerSandbox(session_id, data_dir, output_dir)
    return LocalSandbox(session_id, data_dir, output_dir)
