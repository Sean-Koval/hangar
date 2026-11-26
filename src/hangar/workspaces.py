"""--- KAN-192: a task's workspace, its files given in the experiment spec, as a git commit hangar keeps.

A task with no hosted repo then runs like one with: hangar commits the files once (one fixed author, date and
message, so the same files always give the same commit), keeps the commit as a git bundle under
`<data_dir>/workspaces/`, pins the commit as the task's base, and each trial and grader sandbox clones it from the
bundle, with no remote. Nothing else about the trial changes: its patch, its base check and its isolation work on the
clone as on any other.

The commit is built with `git fast-import` in a bare repository, from the files' bytes: no work tree is ever written,
so no path can reach the repository's own files and no ignore, attributes or config file (the host's or the
workspace's own) changes what goes in. Every file is a plain, non-executable blob.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

from hangar.harness_repo import safe_git

if TYPE_CHECKING:
    from hangar.config import Settings

SHA = re.compile(r"^[0-9a-f]{40}$")
BRANCH = "main"
_WHO = "hangar workspace <workspace@hangar.invalid> 946684800 +0000"  # 2000-01-01, so the commit never moves
_MESSAGE = b"workspace\n"
# names a file system could take for `.git` (git's own verify_path: any case, its 8.3 name, trailing dots or spaces)
_GITDIR = re.compile(r"^(\.git|git~\d+)[. ]*$", re.IGNORECASE)


class WorkspaceError(Exception):
    pass


def path_error(path: str) -> str | None:
    """Why `path` can't be a workspace file (None when it can): a relative, NFC-normalised path of plain parts, none
    of them `.`, `..` or a name a file system could take for `.git`, with no control character."""
    parts = path.split("/")
    if not path or path.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return "must be a relative path, without '.', '..' or empty parts"
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "must not hold a control character"
    if unicodedata.normalize("NFC", path) != path:
        return "must be NFC-normalised"
    if any(_GITDIR.match(p) for p in parts):
        return "must not name a '.git' directory"
    return None


def clash(paths: list[str]) -> str | None:
    """A path that is also another's folder (`a` and `a/b`), else None."""
    names = set(paths)
    for p in paths:
        parts = p.split("/")
        for i in range(1, len(parts)):
            if (d := "/".join(parts[:i])) in names:
                return d
    return None


def _dir(settings: Settings) -> Path:
    return Path(settings.data_dir) / "workspaces"


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    home = cwd.parent / "home"  # an empty HOME: no user config, ignore or attributes file of the host's
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in ("HOME", "XDG_CONFIG_HOME")}
    env |= {"HOME": str(home), "XDG_CONFIG_HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    try:
        res = subprocess.run(safe_git(["git", "-C", str(cwd), *args]), env=env, input=stdin, capture_output=True,
                             timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkspaceError(f"git {args[0]} failed: {e}") from e
    if res.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {(res.stderr or res.stdout).decode(errors='replace')[-300:]}")
    return res.stdout.decode().strip()


def _quote(path: str) -> bytes:
    return b'"' + path.replace("\\", "\\\\").replace('"', '\\"').encode() + b'"'


def _stream(files: dict[str, str]) -> bytes:
    """fast-import's input: one commit holding every file, in path order."""
    out = [f"commit refs/heads/{BRANCH}\nauthor {_WHO}\ncommitter {_WHO}\n".encode(),
           b"data %d\n" % len(_MESSAGE), _MESSAGE]
    for path in sorted(files):
        data = files[path].encode()
        out += [b"M 100644 inline ", _quote(path), b"\n", b"data %d\n" % len(data), data, b"\n"]
    out.append(b"done\n")
    return b"".join(out)


def commit(settings: Settings, files: dict[str, str]) -> str:
    """The commit of `files` (path -> text), kept as `<data_dir>/workspaces/<sha>.bundle`: the same files give the
    same commit, on any hangar."""
    for p in files:
        if why := path_error(p):
            raise WorkspaceError(f"workspace file '{p[:60]}' {why}")
    if d := clash(list(files)):
        raise WorkspaceError(f"workspace path '{d[:60]}' is both a file and a folder")
    with tempfile.TemporaryDirectory(prefix="hangar-ws-") as tmp:
        root = Path(tmp) / "w.git"
        root.mkdir()
        _git(root, "init", "-q", "--bare")
        _git(root, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
        _git(root, "fast-import", "--quiet", "--done", stdin=_stream(files))
        sha = _git(root, "rev-parse", f"refs/heads/{BRANCH}")
        out = _dir(settings) / f"{sha}.bundle"
        if not out.is_file():
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                fd, part = tempfile.mkstemp(prefix=".part-", suffix=".bundle", dir=out.parent)  # same file system
                os.close(fd)
                try:
                    _git(root, "bundle", "create", "-q", part, "HEAD", BRANCH)
                    os.replace(part, out)  # whole or not at all: another experiment may read it
                finally:
                    Path(part).unlink(missing_ok=True)
            except OSError as e:
                raise WorkspaceError(f"could not keep workspace {sha[:12]}: {e}") from e
    return sha


def bundle(settings: Settings, sha: str) -> bytes:
    """The bundle of a commit `commit` made; a WorkspaceError when hangar doesn't have it."""
    if not SHA.match(sha):
        raise WorkspaceError(f"'{sha[:60]}' isn't a workspace commit")
    try:
        return (_dir(settings) / f"{sha}.bundle").read_bytes()
    except OSError as e:
        raise WorkspaceError(f"hangar doesn't have workspace {sha[:12]} (it is made when an experiment starts)") from e
