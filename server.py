import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

CONFIG_FILENAME = ".safe-dev.toml"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_OUTPUT_CHARS = 20_000


@dataclass(frozen=True)
class Target:
    name: str
    path_prefix: str
    cwd: str
    lint: list[str]
    test: list[str]
    timeout_seconds: int
    max_output_chars: int

    def run_dir(self) -> Path:
        return REPO_ROOT / self.cwd

    def owns(self, rel_path: str) -> bool:
        if not self.path_prefix:
            return True
        return rel_path == self.path_prefix or rel_path.startswith(self.path_prefix + "/")

    def label(self) -> str:
        return f"{self.name} ({self.path_prefix or 'whole repo'})"


def _log(message: str) -> None:
    print(f"[safe-dev-tools] {message}", file=sys.stderr, flush=True)


def _discover_repo_root() -> Path:
    for var in ("SAFE_DEV_REPO_ROOT", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(var)
        if value:
            return Path(value).resolve()
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists() or (candidate / CONFIG_FILENAME).exists():
            return candidate
    return cwd


def _load_config(root: Path) -> dict[str, Any]:
    path = root / CONFIG_FILENAME
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        _log(f"ignoring {path}: {e}")
        return {}


def _command(table: dict[str, Any], key: str, where: str) -> list[str]:
    value = table.get(key)
    if value is None:
        return []
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return value
    _log(f"ignoring '{key}' in {where}: expected a non-empty list of strings")
    return []


def _positive_int(table: dict[str, Any], key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    _log(f"ignoring '{key}' in {where}: expected a positive integer")
    return default


def _run_cwd(table: dict[str, Any], where: str) -> str:
    value = table.get("cwd", "")
    if not isinstance(value, str):
        _log(f"ignoring 'cwd' in {where}: expected a string")
        return ""
    value = value.strip("/")
    if value and not (REPO_ROOT / value).is_dir():
        _log(f"ignoring 'cwd' in {where}: '{value}' is not a directory in the repo")
        return ""
    return value


def _target(table: dict[str, Any], name: str, prefix: str, timeout: int, max_chars: int) -> Target:
    where = f"target '{name}'"
    return Target(
        name=name,
        path_prefix=prefix,
        cwd=_run_cwd(table, where),
        lint=_command(table, "lint", where),
        test=_command(table, "test", where),
        timeout_seconds=_positive_int(table, "timeout_seconds", timeout, where),
        max_output_chars=_positive_int(table, "max_output_chars", max_chars, where),
    )


def _load_targets(config: dict[str, Any], timeout: int, max_chars: int) -> list[Target]:
    targets: list[Target] = []
    flat = {k: config[k] for k in ("lint", "test") if k in config}
    if flat:
        targets.append(_target(flat, "repo", "", timeout, max_chars))
    raw = config.get("targets", [])
    if not isinstance(raw, list):
        _log(f"ignoring 'targets' in {CONFIG_FILENAME}: expected an array of tables")
        raw = []
    for i, table in enumerate(raw):
        if not isinstance(table, dict):
            _log(f"ignoring targets[{i}]: expected a table")
            continue
        name = table.get("name")
        if not isinstance(name, str) or not name:
            _log(f"ignoring targets[{i}]: 'name' must be a non-empty string")
            continue
        prefix = table.get("path_prefix", "")
        if not isinstance(prefix, str):
            _log(f"ignoring 'path_prefix' in target '{name}': expected a string")
            prefix = ""
        targets.append(_target(table, name, prefix.strip("/"), timeout, max_chars))
    return targets


REPO_ROOT = _discover_repo_root()
CONFIG = _load_config(REPO_ROOT)
TIMEOUT_SECONDS = _positive_int(CONFIG, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS, CONFIG_FILENAME)
MAX_OUTPUT_CHARS = _positive_int(CONFIG, "max_output_chars", DEFAULT_MAX_OUTPUT_CHARS, CONFIG_FILENAME)
TARGETS = _load_targets(CONFIG, TIMEOUT_SECONDS, MAX_OUTPUT_CHARS)

mcp = MCPServer("safe-dev-tools")


def _run(
    cmd: list[str],
    timeout: int = TIMEOUT_SECONDS,
    max_chars: int = MAX_OUTPUT_CHARS,
    cwd: Path = REPO_ROOT,
) -> tuple[str, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        partial = _text(e.stdout) + _text(e.stderr)
        header = f"Command: {' '.join(cmd)}\nOutput captured before the timeout:\n"
        return f"timed out after {timeout}s", header + _truncate(partial, max_chars)
    except FileNotFoundError:
        return "command not found", cmd[0]

    output = (result.stdout or "") + (result.stderr or "")
    status = "OK" if result.returncode == 0 else f"exit code {result.returncode}"
    return status, _truncate(output, max_chars)


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else data


def _truncate(output: str, max_chars: int) -> str:
    output = output.strip()
    if len(output) <= max_chars:
        return output
    head = max_chars // 4
    tail = max_chars - head
    dropped = len(output) - max_chars
    return f"{output[:head]}\n...[{dropped} chars truncated]...\n{output[-tail:]}"


def _report(status: str, output: str) -> str:
    return f"[{status}]\n{output}"


def _run_target(target: Target, cmd: list[str]) -> tuple[str, str]:
    return _run(cmd, target.timeout_seconds, target.max_output_chars, target.run_dir())


def _run_each(jobs: list[tuple[Target, list[str]]]) -> str:
    if len(jobs) == 1:
        return _report(*_run_target(*jobs[0]))
    results = [(t, *_run_target(t, cmd)) for t, cmd in jobs]
    summary = ", ".join(f"{t.name}: {status}" for t, status, _ in results)
    sections = [f"=== {t.name} ===\n{_report(status, output)}" for t, status, output in results]
    return f"Summary: {summary}\n\n" + "\n\n".join(sections)


def _safe_relative_path(user_path: str) -> str:
    candidate = (REPO_ROOT / user_path).resolve()
    if not candidate.is_relative_to(REPO_ROOT):
        raise ValueError("Path escapes the repository root; refusing.")
    rel = candidate.relative_to(REPO_ROOT).as_posix()
    return "" if rel == "." else rel


def _route_tests(rel_path: str) -> Target | None:
    owners = [t for t in TARGETS if t.test and t.owns(rel_path)]
    if not owners:
        return None
    return max(owners, key=lambda t: len(t.path_prefix))


def _targets_with(key: str) -> list[Target]:
    return [t for t in TARGETS if getattr(t, key)]


def run_lint() -> str:
    return _run_each([(t, t.lint) for t in _targets_with("lint")])


def run_tests(path: str = "") -> str:
    rel = _safe_relative_path(path) if path else ""
    if not rel:
        return _run_each([(t, t.test) for t in _targets_with("test")])
    target = _route_tests(rel)
    if target is None:
        owned = "; ".join(t.label() for t in _targets_with("test"))
        return f"[error]\nNo test target owns '{rel}'. Test targets: {owned}"
    scoped = Path(os.path.relpath(REPO_ROOT / rel, target.run_dir())).as_posix()
    cmd = target.test if scoped == "." else [*target.test, scoped]
    return _run_each([(target, cmd)])


def git_status() -> str:
    """Show the working tree status (read-only)."""
    return _report(*_run(["git", "status", "--short", "--branch"]))


def git_diff(path: str = "") -> str:
    """Show unstaged changes (read-only), optionally scoped to one file."""
    cmd = ["git", "diff"]
    if path:
        rel = _safe_relative_path(path)
        if rel:
            cmd.append(rel)
    return _report(*_run(cmd))


def git_log(max_count: int = 10) -> str:
    """Show recent commit history (read-only). max_count is capped at 50."""
    n = max(1, min(max_count, 50))
    return _report(*_run(["git", "log", f"-{n}", "--oneline"]))


def git_show(ref: str) -> str:
    """Show a specific commit's diff by hash/ref (read-only)."""
    if not all(c.isalnum() or c in "._-/~^" for c in ref):
        return "Invalid ref."
    return _report(*_run(["git", "show", ref]))


def _lint_description() -> str:
    labels = "; ".join(t.label() for t in _targets_with("lint"))
    return (
        "Run every configured linter with its fixed command and report per-target "
        f"pass/fail. Lint targets: {labels}."
    )


def _tests_description() -> str:
    labels = "; ".join(t.label() for t in _targets_with("test"))
    return (
        "Run the test suite. With no path, runs every test target and reports "
        "per-target results. With a repo-relative file or directory path, runs only "
        "the target whose path_prefix owns that path, with the path appended to its "
        f"command. Test targets: {labels}."
    )


def _register_tools() -> list[str]:
    names: list[str] = []
    if _targets_with("lint"):
        mcp.add_tool(run_lint, description=_lint_description())
        names.append("run_lint")
    if _targets_with("test"):
        mcp.add_tool(run_tests, description=_tests_description())
        names.append("run_tests")
    if (REPO_ROOT / ".git").exists():
        for fn in (git_status, git_diff, git_log, git_show):
            mcp.add_tool(fn)
            names.append(fn.__name__)
    return names


REGISTERED_TOOLS = _register_tools()
_log(f"repo root: {REPO_ROOT}")
_log(f"targets: {', '.join(t.label() for t in TARGETS) or 'none'}")
_log(f"tools: {', '.join(REGISTERED_TOOLS) or 'none'}")


if __name__ == "__main__":
    mcp.run()
