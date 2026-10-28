"""What the agent can do besides talk.

Everything it touches on disk is inside one folder, the workspace (./workspace unless
AGENT_WORKSPACE says otherwise): a path that leaves it is refused. Python runs in that folder
too, with a time limit; AGENT_ALLOW_PYTHON=0 takes the tool away.
"""

import datetime
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from langchain_core.tools import tool

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE") or ROOT / "workspace").resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)
LIMIT = 8000          # characters of any one result handed back to the model
PYTHON_TIMEOUT_S = 30


def _inside(path: str) -> Path:
    """A path in the workspace, or an error: the agent never reads or writes outside it."""
    p = (WORKSPACE / path).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError(f"{path!r} is outside the workspace")
    return p


def _cut(text: str) -> str:
    return text if len(text) <= LIMIT else text[:LIMIT] + f"\n… ({len(text) - LIMIT} more characters not shown)"


@tool
def list_files(path: str = ".") -> str:
    """List the files and folders at a path in the workspace ("." is the workspace itself)."""
    try:
        p = _inside(path)
        if not p.exists():
            return f"There is nothing at {path}."
        if p.is_file():
            return f"{path} is a file of {p.stat().st_size} bytes."
        rows = [f"{c.relative_to(WORKSPACE)}/" if c.is_dir() else f"{c.relative_to(WORKSPACE)}  ({c.stat().st_size} bytes)"
                for c in sorted(p.iterdir())]
        return "\n".join(rows) or "The folder is empty."
    except ValueError as exc:
        return f"Refused: {exc}."


@tool
def read_file(path: str) -> str:
    """Read a text file from the workspace."""
    try:
        return _cut(_inside(path).read_text(errors="replace"))
    except ValueError as exc:
        return f"Refused: {exc}."
    except FileNotFoundError:
        return f"There is no file {path}."
    except IsADirectoryError:
        return f"{path} is a folder; use list_files."


@tool
def write_file(path: str, content: str) -> str:
    """Write a text file in the workspace, creating folders as needed. Replaces a file that is there."""
    try:
        p = _inside(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} characters to {p.relative_to(WORKSPACE)}."
    except ValueError as exc:
        return f"Refused: {exc}."


# On macOS, Python runs in a sandbox: it reads and writes only the workspace (and what an
# interpreter needs to start), cannot read the home folder or /etc, and has no network. Found by
# LLMSim's lg-guard-python-escape flow: without it, the model could have the agent read
# /etc/passwd or anything in the user's home. AGENT_PYTHON_SANDBOX=0 turns it off.
SANDBOX = """(version 1)
(allow default)
(deny network*)
(deny file-write* (require-not (subpath "{ws}")))
(deny file-read* (subpath "/Users") (subpath "/private/etc") (subpath "/private/var/root") (subpath "/Volumes"))
(allow file-read* (subpath "{ws}"))
"""


def _python_command(code: str) -> list[str]:
    # The base interpreter, not the virtualenv's: that one lives under the home folder.
    python = getattr(sys, "_base_executable", None) or sys.executable
    cmd = [python, "-I", "-c", code]
    if sys.platform == "darwin" and os.environ.get("AGENT_PYTHON_SANDBOX", "1") != "0" and Path("/usr/bin/sandbox-exec").exists():
        cmd = ["/usr/bin/sandbox-exec", "-p", SANDBOX.format(ws=str(WORKSPACE).replace('"', '')), *cmd]
    return cmd


@tool
def run_python(code: str) -> str:
    """Run a Python 3 script in the workspace folder and return what it printed. Use print() for results.
    It has 30 seconds, no input and no network; it can read and write only files in the workspace."""
    try:
        done = subprocess.run(_python_command(code), cwd=WORKSPACE, capture_output=True, text=True,
                              timeout=PYTHON_TIMEOUT_S, env={"PATH": os.environ.get("PATH", ""), "HOME": str(WORKSPACE)})
    except subprocess.TimeoutExpired:
        return f"Stopped after {PYTHON_TIMEOUT_S} seconds."
    out = done.stdout + (("\n[stderr]\n" + done.stderr) if done.stderr else "")
    return _cut(f"exit code {done.returncode}\n{out}".rstrip())


@tool
def fetch_url(url: str) -> str:
    """Fetch a web page or API over HTTP(S) and return its text."""
    if not url.startswith(("http://", "https://")):
        return "Refused: only http:// and https:// addresses."
    req = urllib.request.Request(url, headers={"User-Agent": "LangGraphAgent/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(400_000).decode(r.headers.get_content_charset() or "utf-8", errors="replace")
            return _cut(f"HTTP {r.status} {r.headers.get('Content-Type', '')}\n{body}")
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001 — anything else is the model's to read, not a crash
        return f"Could not fetch it: {exc}"


@tool
def current_time() -> str:
    """The current local date and time."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def available():
    tools = [list_files, read_file, write_file, fetch_url, current_time]
    if os.environ.get("AGENT_ALLOW_PYTHON", "1") != "0":
        tools.append(run_python)
    return tools
