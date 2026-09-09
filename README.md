# safe-dev-mcp

A minimal MCP server that gives an editor LLM a fixed set of abilities:
lint, test, `git status`, `git diff`, `git log`, `git show`.

## Requirements

Python 3.11 or newer (the config parser is the standard library `tomllib`).

On macOS the built-in `python3` is 3.9, which is too old for both this server
and the `mcp` package. If `pip install mcp` reports "No matching distribution
found", that is the cause. Install a current Python with Homebrew:

```bash
brew install python@3.12
```

## One-time setup

Create a virtualenv inside this clone from the newer interpreter and install
the MCP SDK:

```bash
cd /path/to/safe-dev-mcp
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install mcp
```

Register the server once at Claude Code user scope. The Claude agent in Zed
is Claude Code running over the Agent Client Protocol, so it reads this
config:

```bash
claude mcp add --scope user safe-dev-tools -- \
  /path/to/safe-dev-mcp/.venv/bin/python \
  /path/to/safe-dev-mcp/server.py
```

If you prefer to configure it in Zed itself, add the same command to
`~/.config/zed/settings.json`. Zed forwards these to external agents:

```json
{
  "context_servers": {
    "safe-dev-tools": {
      "command": "/path/to/safe-dev-mcp/.venv/bin/python",
      "args": ["/path/to/safe-dev-mcp/server.py"]
    }
  }
}
```

Then, in the agent's permissions, allow this server's tools and leave
general terminal access denied.

## Per-repo configuration

In each repo where you want lint or test tools:

```bash
cp /path/to/safe-dev-mcp/safe-dev.example.toml .safe-dev.toml
echo .safe-dev.toml >> .git/info/exclude
```

Edit `.safe-dev.toml` for that repo. Commands are grouped into targets. A
target is a part of the repo with its own lint and test commands. Every key
is optional, and a tool is only offered when at least one target defines it.

### Single-surface repos

Top-level `lint` and `test` define one target that owns every path:

```toml
lint = ["ruff", "check", "."]
test = ["pytest", "-q"]
```

### Monorepos

Use one `[[targets]]` table per surface instead:

```toml
[[targets]]
name = "frontend"
path_prefix = "apps/frontend"
lint = ["yarn", "workspace", "frontend", "lint"]
test = ["yarn", "workspace", "frontend", "test"]

[[targets]]
name = "backend"
path_prefix = "apps/backend"
test = ["yarn", "workspace", "backend", "test"]

[[targets]]
name = "e2e"
path_prefix = "e2e"
test = ["yarn", "test:e2e"]
timeout_seconds = 600
```

| Key | Where | Effect |
|---|---|---|
| `name` | target | Label used in results. Required |
| `path_prefix` | target | Repo-relative directory this target owns. Omit to own every path |
| `lint` | top level or target | Command for `run_lint` |
| `test` | top level or target | Command for `run_tests`. A scoped path is appended to it |
| `timeout_seconds` | top level or target | Per-command timeout. Default 60. Target value overrides top level |
| `max_output_chars` | top level or target | Output truncation limit. Default 20000. Target value overrides top level |

Top-level keys must appear before the first `[[targets]]` table, which is a
TOML rule.

How the tools behave with targets:

- `run_tests(path)` runs only the target whose `path_prefix` owns that path.
  When several match, the longest prefix wins. If none match, the tool
  returns an error naming the configured targets rather than guessing.
- `run_tests()` with no path runs every target that has a `test` command and
  returns a summary line followed by a section per target.
- `run_lint()` runs every target that has a `lint` command, skips the rest,
  and reports per target the same way.
- The tool descriptions shown to the model list the target names and
  prefixes, so it knows which paths route where.

Repos that only need the read-only git tools need no config file at all.

The tool list is fixed when the server starts, so restart the agent thread
after editing the config.

## How the server finds the repo

First match wins:

1. `SAFE_DEV_REPO_ROOT` environment variable, for manual testing.
2. `CLAUDE_PROJECT_DIR`, which Claude Code sets for every MCP server it
   launches. User-scope servers run with `~/.claude` as their working
   directory, so this is what makes a global registration work per repo.
3. Walk up from the current working directory until a directory containing
   `.git` or `.safe-dev.toml` is found.
4. The current working directory.

## Testing it standalone

The MCP Inspector lets you see the tool list and call tools by hand. Launch
it from inside the repo you want to test so the server resolves that repo as
its root:

```bash
cd /path/to/some/repo
npx @modelcontextprotocol/inspector \
  /path/to/safe-dev-mcp/.venv/bin/python \
  /path/to/safe-dev-mcp/server.py
```

The SDK's `mcp dev` shortcut is not used here because it always launches the
server through `uv`, which fails if `uv` is not installed.

## Troubleshooting

On startup the server writes two lines to stderr: the repo root it resolved
and the tools it registered. Run it directly from inside a repo to see them,
then press Ctrl-C:

```bash
cd /path/to/some/repo
/path/to/safe-dev-mcp/.venv/bin/python /path/to/safe-dev-mcp/server.py
```

A malformed `lint` or `test` value (for example a plain string instead of a
list) is reported on stderr and that tool is left out. The server still
starts.

## Extending it

To add another safe capability (`git blame`, a formatter check, a type
checker), add a plain function following the same pattern and register it in
`_register_tools`, conditionally if it depends on config. Fixed command list,
validated and bounded parameters, no shell string concatenation. Resist the
temptation to add a generic "run this git subcommand" tool. Enumerate the ones
you actually want.
