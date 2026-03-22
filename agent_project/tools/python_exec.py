"""Local Python execution tool with artifact support."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.tools import tool

from utils.persistence import get_artifacts_dir, persist_tool_result

SANDBOX_EXEC_TIMEOUT = 300


@tool
def execute_python(code: str, output_paths: list[str] | None = None) -> str:
    """Run Python code locally for computation, data fetching, and matplotlib visualizations.

    The code runs with the current Python interpreter. The artifacts directory is available
    as the ARTIFACTS_DIR environment variable — save any output files there so they are
    automatically picked up (e.g. plt.savefig(os.environ['ARTIFACTS_DIR'] + '/plot.png')).

    include paths (relative to ARTIFACTS_DIR) you saved in output_paths to confirm them.
    """
    output_paths = output_paths or []
    artifacts_dir = get_artifacts_dir()
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    script_path: str | None = None
    stdout = stderr = ""
    exit_code = -1
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            script_path = f.name

        mpl_cache = artifacts_dir / ".mplcache"
        mpl_cache.mkdir(exist_ok=True)
        env = {**os.environ, "ARTIFACTS_DIR": str(artifacts_dir), "MPLCONFIGDIR": str(mpl_cache)}
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=SANDBOX_EXEC_TIMEOUT,
            env=env,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        stderr = f"Execution timed out after {SANDBOX_EXEC_TIMEOUT}s"
    except Exception as exc:  # noqa: BLE001
        stderr = str(exc)
    finally:
        if script_path:
            try:
                os.unlink(script_path)
            except Exception:  # noqa: BLE001
                pass

    confirmed_artifacts: list[dict] = []
    for path_str in output_paths:
        p = Path(path_str)
        if not p.is_absolute():
            p = artifacts_dir / p.name
        confirmed_artifacts.append({"path": str(p), "exists": p.exists()})

    result_payload = {
        "exit_code": exit_code,
        "stdout": stdout[:4000],
        "stderr": stderr[:2000] if stderr else "",
        "local_artifacts_dir": str(artifacts_dir),
        "confirmed_artifacts": confirmed_artifacts,
    }
    ok = exit_code == 0
    if stderr and not ok:
        summary = f"Python execution failed (exit {exit_code}). stderr: {stderr[:300]}"
    else:
        summary = f"Python execution {'succeeded' if ok else 'finished with warnings'} (exit {exit_code}). stdout: {stdout[:300]}"

    return persist_tool_result(
        "execute_python",
        {"code": code, "output_paths": output_paths},
        json.dumps(result_payload, ensure_ascii=False),
        summary,
    )
