"""--- KAN-287: facts about a shell command, read from its whole text before its event is clipped.

An event keeps a 200-character preview of a tool call's input (`events.BRIEF_MAX`); about half of real agent calls are
longer. A labeller reading the preview can't see a write or a test run past the cut, so the parser records these facts
from the whole command (decision KAN-271):

- `writes`: files the command writes: redirects (`>`, `>>`, `&>`), `tee`, `sed -i`, `perl -i`, `cp`, `mv`, `install`
  and `touch` targets, `patch`'s file and the files of a diff fed to `git apply`, `patch` or `apply_patch` in a
  heredoc, and files Python opens for writing (`open(..., "w")`, `.write_text`, `.write_bytes`) in `python -c` or a
  heredoc.
- `runs`: the programs run at a command position, in order: the program, with its subcommand for tools that have one
  (`git status`, `npm run build`, `uv sync`), and `python -m mod` or `python script.py` for scripts. Quoted text,
  heredoc bodies and the wrappers' own flags (`uv run --project svc`) are not programs.
- `probe`: true when every test runner in the command was asked for its version, its help or its test list
  (`--version`, `--help`, `--collect-only`), so no test ran.

They are read from the command's words, never run, and are hints like `ops`: the command is in the event too. Only
the first MAX_INPUT characters are read, and every step is linear in them, so a huge command can't stall the parser.
Each list keeps at most 32 items of at most 256 characters; nothing else of the command is stored.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from collections.abc import Set as AbstractSet
from typing import Any

MAX_INPUT = 65536  # characters of a command read
MAX_ITEMS = 32
MAX_LEN = 256
MAX_BYTES = 2048  # of a list's text in UTF-8, so a command adds at most 4 KiB to its event
SEP = "\n"  # event data keeps scalars: a list is stored as its items, one per line

# Operators, longest first. `$(`, `<(` and `>(` open a nested command, closed by `)`.
_OPERATORS = ("<<<", "<<-", "&>>", "$(", "<(", ">(", "&&", "||", ";;", "|&", "&>", ">>", ">|", ">&", "<<", "<&", "<>",
              ";", "&", "|", "(", ")", ">", "<")
_SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;"}
_NESTED = {"$(", "<(", ">("}
_WRITE_REDIRECTS = {">", ">>", "&>", "&>>", ">|", ">&"}
_READ_REDIRECTS = {"<", "<<<", "<&", "<>"}
_NOT_FILES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "-"}
_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{"}
_SKIP_SEGMENT = {"for", "select", "case", "function", "done", "fi", "esac", "}", "in"}
_WORD_END = set(" \t\n;&|()<>")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
# Wrappers run the command after their own flags; these flags take a value (`uv run --project svc pytest`)
_WRAPPERS: dict[str, tuple[str | None, set[str]]] = {
    "uv": ("run", {"--project", "--directory", "--with", "--with-requirements", "--python", "-p", "--package",
                   "--group", "--extra", "--env-file", "--index", "--only-group"}),
    "poetry": ("run", {"-C", "--directory", "-P", "--project"}),
    "pipenv": ("run", set()),
    "hatch": ("run", {"-e", "--env"}),
    "pdm": ("run", {"-p", "--project"}),
    "bundle": ("exec", set()),
    "pnpm": ("exec", {"-C", "--dir", "--filter", "-F"}),
    "npx": (None, {"-p", "--package"}),
    "bunx": (None, {"-p", "--package"}),
    "sudo": (None, {"-u", "-g", "-C", "-D", "-h", "-p", "-U"}),
    "time": (None, {"-f", "-o"}),
    "env": (None, {"-u", "-C", "-S"}),
    "nice": (None, {"-n"}),
    "timeout": (None, {"-s", "-k", "--signal"}),
    "xvfb-run": (None, {"-a", "-n", "-s", "--server-args"}),
    "nohup": (None, set()),
    "exec": (None, set()),
    "command": (None, set()),
    "stdbuf": (None, {"-i", "-o", "-e"}),
}
# Tools whose first word after the program is what they do (`git status`, `npm install`, `make test`), with the global
# flags that take a value before it (`kubectl -n ns get`, `npm --prefix web test`)
_COMMON_VALUED = frozenset({"-C", "-f", "--file", "--directory", "--dir", "--cwd", "--prefix", "--project",
                            "--manifest-path", "--context", "--namespace", "-w", "--workspace", "--filter", "-F"})
_SUBCOMMAND: dict[str, AbstractSet[str]] = {
    "git": {"-C", "-c", "--git-dir", "--work-tree", "--namespace"},
    "gh": {"-R", "--repo"},
    "npm": _COMMON_VALUED,
    "pnpm": _COMMON_VALUED,
    "yarn": _COMMON_VALUED,
    "bun": _COMMON_VALUED,
    "go": {"-C"},
    "cargo": _COMMON_VALUED,
    "uv": _COMMON_VALUED,
    "pip": _COMMON_VALUED,
    "pip3": _COMMON_VALUED,
    "poetry": {"-C", "--directory", "-P", "--project"},
    "pipx": set(),
    "make": {"-C", "-f", "--file", "-j", "-I"},
    "docker": {"-H", "--host", "--context", "-c", "--config"},
    "kubectl": {"-n", "--namespace", "--context", "--kubeconfig", "-l", "--selector"},
    "dotnet": set(),
    "mvn": {"-f", "--file", "-pl", "-P", "-s"},
    "gradle": {"-p", "--project-dir"},
    "./gradlew": {"-p", "--project-dir"},
    "apt": set(),
    "apt-get": set(),
    "brew": set(),
    "terraform": set(),
    "helm": {"-n", "--namespace", "--kube-context"},
    "bazel": set(),
    "deno": set(),
    "mix": set(),
    "swift": set(),
    "conda": {"-n", "--name", "-p", "--prefix"},
    "gcloud": {"--project", "--account"},
    "hangar": set(),
    "rustup": set(),
    "ruff": set(),
    "pre-commit": {"-c", "--config"},
}
# Interpreters: the flags that take a value, and the flags whose value is the program itself (`python -c CODE`)
_SCRIPT: dict[str, tuple[set[str], set[str]]] = {
    "python": ({"-W", "-X", "-Q"}, {"-c"}),
    "node": ({"-r", "--require", "--import", "--loader", "--conditions", "-C"}, {"-e", "--eval", "-p", "--print"}),
    "ruby": ({"-I", "-r", "-C", "-E"}, {"-e"}),
    "perl": ({"-I", "-M", "-m"}, {"-e", "-E"}),
    "php": ({"-c", "-d", "-z"}, {"-r"}),
}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_TEST_PROGRAMS = {"pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha", "ava", "rspec", "phpunit", "ctest",
                  "gotestsum", "playwright"}
_TEST_SUBCOMMANDS = {"go": {"test"}, "cargo": {"test", "nextest"}, "npm": {"test", "t"}, "pnpm": {"test", "t"},
                     "yarn": {"test"}, "bun": {"test"}, "mvn": {"test", "verify"}, "gradle": {"test", "check"},
                     "./gradlew": {"test", "check"}, "make": {"test", "check"}, "dotnet": {"test"},
                     "bazel": {"test"}, "deno": {"test"}, "mix": {"test"}, "swift": {"test"}}
# A runner given one of these prints and exits without running a test, whatever else it is given. The same set
# KAN-279 (hangar 491) uses to keep a probe out of the `test` op.
_GH_GROUPS = {"pr", "issue", "run", "repo", "release", "workflow", "gist", "label", "secret", "variable", "cache",
              "codespace", "project", "ruleset", "auth", "extension", "attestation", "org", "ssh-key", "gpg-key"}
_GH_VALUED = {"-R", "--repo", "-H", "--header", "-X", "--method", "-f", "-F", "--field", "--raw-field", "-q", "--jq",
              "-t", "--title", "-b", "--body", "-B", "--base", "--head", "-l", "--label", "-a", "--assignee", "-L",
              "--limit", "-s", "--state", "-w", "--workflow", "-e", "--env"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")  # a line break of any kind, and other controls
_PROBE_FLAGS = {"--version", "--help", "-h", "--collect-only", "--co", "--listTests", "--list"}
_DELIM = re.compile(r"""-?[ \t]*(?:'([^'\n]*)'|"([^"\n]*)"|\\?([A-Za-z_][\w.-]*))""")
_PY_STR = r"""[rbuRBU]{0,2}(['"])([^'"\n{}]+)\1"""
_PY_OPEN = re.compile(r"\bopen\(\s*" + _PY_STR + r"""\s*,(?:[^()\n]*?,)?\s*(?:mode\s*=\s*)?"""
                      + r"""(['"])[rbt+]*[wax][rbt+]*\3""")
_PY_WRITE = re.compile(r"\bPath\(\s*" + _PY_STR + r"\s*\)\.write_(?:text|bytes)\(")
_DIFF_FILE = re.compile(r"^(?:\+\+\+ (?!/dev/null)(?:[ab]/)?([^\t\n]+)|\*\*\* (?:Update|Add) File: (.+))$", re.M)

Segment = tuple[list[str], list[str], str | None]


def facts(command: str | None) -> dict[str, Any]:
    """`{"writes", "runs", "probe"}` for event data: lists as text (one item per line), None when empty."""
    writes, runs, tests = _read((command or "")[:MAX_INPUT], depth=0)
    probe = True if tests and all(tests) else None
    return {"writes": join(writes), "runs": join(runs), "probe": probe}


def split(value: Any) -> list[str]:
    """A list stored by `facts` (or recorded before KAN-287: none) back as a list."""
    return [v for v in value.split(SEP) if v] if isinstance(value, str) else []


def join(items: list[Any]) -> str | None:
    """A list for event data: text, one item per line, at most MAX_ITEMS items of at most MAX_LEN characters and
    MAX_BYTES in all. A line break or other control character in an item is a space, so it stays one item."""
    out: list[str] = []
    size = 0
    for raw in items:
        if not isinstance(raw, str):
            continue
        item = _CONTROL.sub(" ", raw).strip()[:MAX_LEN]
        if not item or item in out:
            continue
        size += len(item.encode()) + 1
        if size > MAX_BYTES + 1:
            break
        out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return SEP.join(out) or None


def _read(command: str, depth: int) -> tuple[list[str], list[str], list[bool]]:
    """(writes, runs, one bool per test runner: was it only a probe)."""
    writes: list[str] = []
    runs: list[str] = []
    tests: list[bool] = []
    for line, bodies in _lines(command):
        for words, targets, body in _segments(_tokens(line), bodies):
            writes += targets
            _command(words, body, writes, runs, tests, depth)
    return writes, runs, tests


def _lines(command: str) -> list[tuple[str, list[str]]]:
    """The command's logical lines, each with the bodies of the heredocs it opens. A newline inside quotes or after a
    backslash doesn't end a line, and a comment is dropped. A heredoc takes the lines after its line up to its
    delimiter, read as they are; one whose delimiter never comes takes the rest of the command, as bash does. A command
    substitution inside double quotes (`"$(cat <<'EOF' ... EOF)"`, the usual commit message) is read for its heredocs
    and then left out of the line: its commands are not counted, and its quotes can't upset the line's."""
    out: list[tuple[str, list[str]]] = []
    cur: list[str] = []
    pending: list[tuple[str, bool]] = []  # this line's heredocs: (delimiter, strip leading tabs)
    ends: dict[tuple[str, bool], list[int]] | None = None  # (a line's text, tabs stripped) -> where those lines start
    stack: list[tuple[str, int]] = []  # per `$(` opened inside double quotes: (the quote to go back to, its depth)
    depth = 0  # parentheses open inside the innermost such `$(`
    quote = ""
    prev = "\n"  # the last character read, for a comment's start
    n = len(command)
    i = 0

    def keep(text: str) -> None:
        if not stack:
            cur.append(text)

    while i < n:
        c = command[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                keep(command[i : i + 2])  # an escaped character, in double quotes or out
            prev = command[i + 1]
            i += 2  # (a backslash and a newline join two lines)
            continue
        elif quote == '"' and command.startswith("$((", i):
            j = _arithmetic_end(command, i + 1)
            keep(command[i:j])
            prev, i = ")", j
            continue
        elif quote == '"' and command.startswith("$(", i):
            keep("$(")
            stack.append((quote, depth))
            quote, depth, prev = "", 0, "("
            i += 2
            continue
        elif quote == '"':
            quote = "" if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif command.startswith("$((", i) or (command.startswith("((", i) and prev in _WORD_END):
            j = _arithmetic_end(command, i + (c == "$"))  # arithmetic: its `<<` is a shift
            keep(command[i:j])
            prev, i = ")", j
            continue
        elif c == "#" and prev in _WORD_END:  # a comment, to the end of the line
            while i < n and command[i] != "\n":
                i += 1
            continue
        elif command.startswith("<<", i) and not command.startswith("<<<", i) and prev != "<":
            if m := _DELIM.match(command, i + 2):
                delim = next(g for g in m.groups() if g is not None)
                pending.append((delim, command.startswith("-", i + 2)))
        elif c == "(" and stack:
            depth += 1
        elif c == ")" and stack:
            if depth == 0:
                quote, depth = stack.pop()
            else:
                depth -= 1
        elif c == "\n":
            i += 1
            bodies: list[str] = []
            for delim, strip in pending:
                ends = _line_index(command) if ends is None else ends
                at = ends.get((delim, strip), [])
                k = bisect.bisect_left(at, i)
                if k < len(at):  # the body runs to the delimiter's line
                    bodies.append(command[i : max(i, at[k] - 1)])
                    end = command.find("\n", at[k])
                    i = n if end < 0 else end + 1
                else:  # no delimiter: the rest of the command is the body
                    bodies.append(command[i:])
                    i = n
            pending, prev = [], "\n"
            if stack:  # inside a quoted `$(`: the line goes on, and its heredocs are its own
                continue
            out.append(("".join(cur), bodies))
            cur = []
            continue
        keep(c)
        prev = c
        i += 1
    out.append(("".join(cur), []))
    return out


def _arithmetic_end(command: str, i: int) -> int:
    """Where the `((...))` that starts at `i` ends (or the command, when it doesn't)."""
    depth = 0
    for j in range(i, len(command)):
        depth += {"(": 1, ")": -1}.get(command[j], 0)
        if depth == 0:
            return j + 1
    return len(command)


def _line_index(command: str) -> dict[tuple[str, bool], list[int]]:
    """Where each physical line starts, by its text and by its text without leading tabs (a `<<-` delimiter), so
    finding a heredoc's end is a lookup, not a scan of the rest of the command. A CRLF line ends at its CR."""
    out: dict[tuple[str, bool], list[int]] = {}
    j = 0
    for line in command.split("\n"):
        text = line.removesuffix("\r")
        out.setdefault((text, False), []).append(j)
        out.setdefault((text.lstrip("\t"), True), []).append(j)
        j += len(line) + 1
    return out


def _quoted(line: str, i: int) -> tuple[str, int]:
    """The text of the quoted string that starts at `i` (without its quotes) and the index after it. An unclosed quote
    runs to the end of the line."""
    q = line[i]
    if q == "'":
        end = line.find("'", i + 1)
        end = len(line) if end < 0 else end
        return line[i + 1 : end], end + 1
    text: list[str] = []
    j = i + 1
    while j < len(line) and line[j] != '"':
        if line[j] == "\\" and j + 1 < len(line) and line[j + 1] in '"\\$`':
            j += 1
        text.append(line[j])
        j += 1
    return "".join(text), j + 1


def _tokens(line: str) -> list[tuple[str, bool]]:
    """(token, is it an operator) for a logical line, with quotes taken off words. A descriptor before a redirect
    (`2>file`) is not a word, and `$((...))` arithmetic stays inside its word."""
    out: list[tuple[str, bool]] = []
    word: list[str] = []
    started = tick = False
    n = len(line)
    i = 0
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            word.append(line[i + 1])
            started = True
            i += 2
        elif c == "`":  # an old-style command substitution: read like `$(...)`
            if started:
                out.append(("".join(word), False))
            word, started = [], False
            out.append(("$(", True) if not tick else (")", True))
            tick = not tick
            i += 1
        elif c in "'\"":
            text, i = _quoted(line, i)
            word.append(text)
            started = True
        elif line.startswith("$((", i) or (not started and line.startswith("((", i)):
            depth, j = 0, i + (c == "$")
            while j < n:
                depth += {"(": 1, ")": -1}.get(line[j], 0)
                j += 1
                if depth == 0:
                    break
            word.append(line[i:j])
            started = True
            i = j
        elif c in " \t\n":
            if started:
                out.append(("".join(word), False))
            word, started = [], False
            i += 1
        elif op := next((o for o in _OPERATORS if line.startswith(o, i)), None):
            if started:
                text = "".join(word)
                if not (text.isdigit() and op[0] in "<>&"):  # 2>file: a descriptor
                    out.append((text, False))
            word, started = [], False
            out.append((op, True))
            i += len(op)
        else:
            word.append(c)
            started = True
            i += 1
    if started:
        out.append(("".join(word), False))
    return out


def _segments(toks: list[tuple[str, bool]], bodies: list[str]) -> list[Segment]:
    """(words, redirect targets, the heredoc body it reads) per `;`/`&&`/`||`/`|`/`&` segment of a line. A nested
    command (`$(...)`, `<(...)`, `>(...)`) is a segment of its own and stands in its segment as a pipe's path."""
    out: list[Segment] = []
    stack: list[Segment] = []
    words: list[str] = []
    targets: list[str] = []
    body: str | None = None
    i = 0
    while i < len(toks):
        t, op = toks[i]
        nxt = toks[i + 1][0] if i + 1 < len(toks) and not toks[i + 1][1] else None
        if not op:
            words.append(t)
        elif t in _NESTED:
            stack.append((words, targets, body))
            words, targets, body = [], [], None
        elif t == ")" and stack:
            out.append((words, targets, body))
            words, targets, body = stack.pop()
            words.append("/dev/fd/63")
        elif t in _SEPARATORS:
            out.append((words, targets, body))
            words, targets, body = [], [], None
        elif t in _WRITE_REDIRECTS:
            if nxt is not None:
                descriptor = t == ">&" and (nxt.isdigit() or nxt == "-")  # 2>&1, >&2
                if not descriptor and nxt not in _NOT_FILES and not nxt.startswith("/dev/fd/"):
                    targets.append(nxt)
                i += 1
        elif t in _READ_REDIRECTS:
            i += 1 if nxt is not None else 0
        elif t in ("<<", "<<-"):
            body = bodies.pop(0) if bodies else body
            i += 1 if nxt is not None else 0
        i += 1
    out.append((words, targets, body))
    out += reversed(stack)  # an unclosed nested command
    return [s for s in out if s[0] or s[1]]


def _unwrap(words: list[str]) -> list[str]:
    """The command a segment runs: without `VAR=value`, shell keywords and wrappers with their own flags."""
    k = 0
    for _ in range(6):
        while k < len(words) and (words[k] in _KEYWORDS or _ASSIGNMENT.fullmatch(words[k])):
            k += 1
        if k >= len(words):
            return []
        head = _name(words[k])
        if head not in _WRAPPERS or k + 1 >= len(words):
            break
        if head == "command" and words[k + 1] in ("-v", "-V"):
            break  # `command -v pytest` looks a program up, it doesn't run it
        sub, valued = _WRAPPERS[head]
        j = k + 1
        if sub is not None:
            if words[j] != sub:
                break  # `uv sync`, `poetry install`: the tool itself
            j += 1
        while j < len(words) and (words[j].startswith("-")
                                  or (head == "timeout" and re.fullmatch(r"\d+(\.\d+)?[smhd]?", words[j]))
                                  or (head == "env" and "=" in words[j])):
            j += 2 if words[j] in valued else 1
        k = j
    return words[k:]


def _name(word: str) -> str:
    """A program's name: an installed program by its base name (`/usr/bin/git` is `git`); a script run by a relative
    path keeps it (`./gradlew`, `scripts/check.sh`)."""
    return word.rsplit("/", 1)[-1] if word.startswith("/") else word


def _positionals(args: list[str], valued: AbstractSet[str] = frozenset()) -> list[str]:
    out: list[str] = []
    skip = ended = False
    for a in args:
        if skip:
            skip = False
        elif a == "--" and not ended:
            ended = True
        elif a.startswith("-") and a != "-" and not ended:
            skip = a in valued
        else:
            out.append(a)
    return out


def _sub(key: str, args: list[str]) -> tuple[str | None, list[str]]:
    """A subcommand tool's subcommand and the arguments after it. Global flags and their values (`kubectl -n ns`), a
    toolchain (`cargo +nightly`) and `KEY=value` (`make CC=gcc`) are skipped."""
    valued = _SUBCOMMAND.get(key, frozenset())
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in valued
        elif a and not a.startswith("+") and "=" not in a:
            return a, args[i + 1 :]
    return None, []


def _script(base: str) -> str | None:
    """The interpreter a program is (`python3.12` is `python`), or None."""
    if re.fullmatch(r"python[\d.]*", base):
        return "python"
    return base if base in _SCRIPT else None


def _script_target(kind: str, args: list[str]) -> tuple[str | None, int]:
    """(what an interpreter runs: `-m mod`, `-c`, `-e`, `-`, a script; where the arguments after it start)."""
    valued, inline = _SCRIPT[kind]
    i = 0
    while i < len(args):
        a = args[i]
        if a in valued:
            i += 2
        elif a == "-m" and kind == "python":
            return (f"-m {args[i + 1]}" if i + 1 < len(args) else "-m"), i + 2
        elif a in inline:
            return a, i + 2
        elif kind in ("perl", "ruby") and re.fullmatch(r"-[a-zA-Z]*[eE]", a):  # perl -pe SCRIPT
            return "-e", i + 2
        elif a == "-" or not a.startswith("-"):
            return a, i + 1
        else:
            i += 1
    return None, len(args)


def _command(words: list[str], body: str | None, writes: list[str], runs: list[str], tests: list[bool],
             depth: int) -> None:
    if not words or words[0] in _SKIP_SEGMENT or words[0].startswith("(("):  # (( arithmetic ))
        return
    words = _unwrap(words)
    if not words:
        return
    head = _name(words[0])
    base = posixpath.basename(head)
    args = words[1:]
    runs.append(_run(head, args))
    _test(head, args, tests)
    if base == "tee":
        writes += [a for a in _positionals(args) if not a.startswith("/dev/fd/")]
    elif base == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in args):
        args = [a for i, a in enumerate(args) if not (a == "" and i and args[i - 1] == "-i")]  # BSD: -i ''
        scripted = any(a in ("-e", "-f", "--expression", "--file") or a.startswith(("--expression=", "--file="))
                       for a in args)
        files = _positionals(args, {"-e", "-f", "--expression", "--file"})
        writes += files if scripted else files[1:]
    elif base == "perl" and any(re.fullmatch(r"-[a-zA-Z]*i.*", a) for a in args):
        writes += _positionals(args[_script_target("perl", args)[1] :])
    elif base in ("cp", "mv", "install", "ln"):
        target = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-t", "--target-directory")), None)
        pos = _positionals(args, {"-t", "--target-directory", "-m", "--mode", "-o", "-g", "-S", "--suffix"})
        if target:
            writes.append(target)
        elif base == "install" and any(a in ("-d", "--directory") for a in args):
            writes += pos  # install -d: every name is a directory it makes
        elif len(pos) >= 2:
            writes.append(pos[-1])
    elif base == "touch":
        writes += _positionals(args, {"-d", "-r", "-t", "--date", "--reference"})
    elif base == "patch":
        out = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-o", "--output")), None)
        pos = _positionals(args, {"-p", "-i", "-o", "--input", "--output", "-d", "--directory", "-B", "-r"})
        writes += [out] if out else pos[:1]
        if body and not out and not pos:
            writes += _diff_files(body)
    elif ((base == "git" and _sub("git", args)[0] == "apply") or base == "apply_patch") and body:
        writes += _diff_files(body)
    elif _script(base) == "python":
        code = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "-c"), None)
        target, _ = _script_target("python", args)
        for text in (code, body if target in (None, "-") else None):
            if text:
                found = [*_PY_OPEN.finditer(text), *_PY_WRITE.finditer(text)]
                writes += [m.group(2) for m in sorted(found, key=lambda m: m.start())]
    elif base in _SHELLS and depth < 2:
        code = next((args[i + 1] for i, a in enumerate(args[:-1]) if re.fullmatch(r"-[a-z]*c", a)), None)
        inner = code if code is not None else (body if not _positionals(args) else None)
        if inner:
            w, r, t = _read(inner, depth + 1)
            writes += w
            runs += r
            tests += t


def _diff_files(body: str) -> list[str]:
    return [(m.group(1) or m.group(2)).strip() for m in _DIFF_FILE.finditer(body)]


def _run(head: str, args: list[str]) -> str:
    base = posixpath.basename(head)
    key = head if head == "./gradlew" else base
    if key in _SUBCOMMAND:
        sub, rest = _sub(key, args)
        if key in ("npm", "pnpm", "yarn", "bun") and sub == "run":
            script = _positionals(rest)[:1]
            return f"{base} run {script[0]}" if script else f"{base} run"
        if key == "gh" and sub in _GH_GROUPS:
            nxt = _positionals(rest, _GH_VALUED)[:1]
            return f"gh {sub} {nxt[0]}" if nxt else f"gh {sub}"
        return f"{head} {sub}" if sub else head
    if kind := _script(base):
        target, _ = _script_target(kind, args)
        return f"{base} {target}" if target else base
    if base in _SHELLS:
        if any(re.fullmatch(r"-[a-z]*c", a) for a in args):
            return f"{base} -c"
        pos = _positionals(args)
        return f"{base} {pos[0]}" if pos else base
    return head


def _test(head: str, args: list[str], tests: list[bool]) -> None:
    """A test runner: is it asked for its version, help or test list."""
    base = posixpath.basename(head)
    key = head if head == "./gradlew" else base
    rest: list[str] | None = None
    if base in _TEST_PROGRAMS:
        rest = args
    elif _script(base) == "python":
        target, start = _script_target("python", args)
        if target in ("-m pytest", "-m unittest"):
            rest = args[start:]
    elif key in _TEST_SUBCOMMANDS:
        sub, after = _sub(key, args)
        if sub in _TEST_SUBCOMMANDS[key]:
            rest = after
        elif key in ("npm", "pnpm", "yarn", "bun") and sub == "run":
            script = _positionals(after)[:1]
            if script and script[0].split(":")[0] in ("test", "tests"):
                rest = after[after.index(script[0]) + 1 :]
    if rest is not None:
        tests.append(any(a in _PROBE_FLAGS for a in rest))
