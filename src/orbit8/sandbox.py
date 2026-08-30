"""Sandboxed execution of agent-generated code.

Two independent walls:

1. **Containment** — the script runs as a separate process (`python -I -S`:
   isolated mode, no site-packages, no user site), in a scratch directory
   holding only a COPY of the input file, with an empty environment, a hard
   wall-clock timeout, and POSIX resource limits (CPU, address space, file
   size, no core dumps). Process-level limits stop accidents; deployments
   that ingest adversarial files should wrap this in a container
   (`docker run --network none --read-only`) — documented, not pretended.

2. **Distrust of output** — callers never look at the sandbox's side
   effects. Only stdout crosses back, and the caller must validate it
   against a schema before anything downstream sees it (codegen.py).
"""
from __future__ import annotations

import resource
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_BYTES = 20_000_000        # refuse runaway stdout


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _child_limits() -> None:
    """Applied in the child just before exec. Each limit is best-effort —
    macOS enforces some of these loosely; the container is the real wall."""
    for limit, value in (
            (resource.RLIMIT_CPU, 10),                   # seconds of CPU
            (resource.RLIMIT_AS, 512 * 1024 * 1024),     # 512 MB address space
            (resource.RLIMIT_FSIZE, 10 * 1024 * 1024),   # 10 MB file writes
            (resource.RLIMIT_CORE, 0),                   # no core dumps
            (resource.RLIMIT_NPROC, 16)):                # no fork bombs
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass


def run_sandboxed(script: str, input_file, *,
                  timeout: float = 15.0) -> SandboxResult:
    """Run ``script`` against a copy of the input file(s) in a scratch dir.

    ``input_file`` is one path or a sequence of them; the copies are passed
    as argv[1:] in the order given. Nothing else the script does is trusted
    or kept.

    Multiple inputs exist because a converter frequently needs to SEE more
    than one file to do its job: game exports commonly ship one file per
    locale, so the source text and the translation live apart and neither
    file is bilingual on its own. Handing the adapter one file at a time
    made that layout unreadable — it could only ever find a source with no
    target, and produced correctly-shaped, entirely useless output.

    Every wall is unchanged: separate ``python -I -S`` process, empty env,
    POSIX rlimits, scratch copies, and only stdout crossing back.
    """
    files = ([input_file] if isinstance(input_file, (str, Path))
             else list(input_file))
    if not files:
        raise ValueError("no input file given")
    with tempfile.TemporaryDirectory(prefix="orbit8-sbx-") as scratch:
        scratch_dir = Path(scratch)
        names = []
        for index, source in enumerate(files):
            # Numbered copies: the real names may collide, contain spaces,
            # or leak a client path into the sandbox.
            target = scratch_dir / (
                f"input{index}{Path(source).suffix or '.dat'}")
            shutil.copyfile(source, target)
            names.append(target.name)
        adapter = scratch_dir / "adapter.py"
        adapter.write_text(script, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", str(adapter), *names],
                cwd=scratch_dir,
                env={},                       # no keys, no proxies, no HOME
                capture_output=True,
                timeout=timeout,
                preexec_fn=_child_limits,
            )
        except subprocess.TimeoutExpired as err:
            return SandboxResult(
                returncode=-1,
                stdout=(err.stdout or b"")[:2000].decode("utf-8", "replace"),
                stderr=f"timed out after {timeout}s",
                timed_out=True)
        stdout = proc.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")
        stderr = proc.stderr[:20_000].decode("utf-8", "replace")
        if len(proc.stdout) > MAX_OUTPUT_BYTES:
            return SandboxResult(returncode=1, stdout="",
                                 stderr="stdout exceeded 20MB limit")
        return SandboxResult(returncode=proc.returncode,
                             stdout=stdout, stderr=stderr)
