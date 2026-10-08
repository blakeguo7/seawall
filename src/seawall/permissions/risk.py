"""Static risk labels for tool calls.

``assess_tool_call`` looks at what a call is about to do and labels it ``low``,
``medium``, ``high`` or ``critical``. The label has two uses:

* ``critical`` calls are refused by the permission checker in every mode, even
  ``full_auto`` and even for tools on the allow list. These are the commands no
  coding task needs: wiping the disk or the home directory, reading credential
  stores, reverse shells, fork bombs, shutting the machine down.
* every other label is information. It is shown to whoever approves the call and
  written to the audit log, so approvals and logs say "recursive delete" rather
  than only "bash".

This is a tripwire, not a sandbox. It reads the command text with a small shell
tokenizer and matches known-dangerous shapes. A determined or merely creative
caller gets around it (an encoded payload, a script that does the deleting, a
variable holding the path). The boundary that actually contains a command is the
sandbox, not this module.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable

from seawall.permissions.sensitive import match_sensitive_path


class RiskLevel(IntEnum):
    """How much damage a tool call could do. Ordered, so levels compare."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: str) -> RiskLevel:
        try:
            return cls[value.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown risk level: {value!r}") from None


@dataclass(frozen=True)
class RiskAssessment:
    """The risk label of one tool call and the reasons behind it."""

    level: RiskLevel = RiskLevel.LOW
    reasons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def assess_command(command: str, *, home: str | None = None) -> RiskAssessment:
    """Label a shell command line."""
    ctx = _Context(home=home or str(Path.home()))
    _assess_shell_text(command, ctx, depth=0)
    return ctx.result()


def assess_tool_call(
    tool_name: str,
    *,
    is_read_only: bool,
    file_path: str | None = None,
    command: str | None = None,
    cwd: str | Path | None = None,
    home: str | None = None,
) -> RiskAssessment:
    """Label one tool call from the facts the permission checker already has."""
    ctx = _Context(home=home or str(Path.home()))
    if command:
        _assess_shell_text(command, ctx, depth=0)
    if file_path:
        _assess_file_path(file_path, is_read_only=is_read_only, cwd=cwd, ctx=ctx)
    name_risk = _TOOL_NAME_RISK.get(tool_name)
    if name_risk is not None:
        ctx.add(*name_risk)
    return ctx.result()


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

_TOOL_NAME_RISK: dict[str, tuple[RiskLevel, str]] = {
    "agent": (
        RiskLevel.MEDIUM,
        "starts a sub-agent that runs on its own and cannot ask for approval",
    ),
    "cron_create": (RiskLevel.HIGH, "schedules commands that run later without anyone watching"),
    "remote_trigger": (RiskLevel.HIGH, "triggers a remote job"),
}


@dataclass
class _Context:
    home: str
    findings: dict[str, RiskLevel] = field(default_factory=dict)
    downloads_in_substitution: bool = False

    def add(self, level: RiskLevel, reason: str) -> None:
        if level is RiskLevel.LOW:
            return
        if self.findings.get(reason, RiskLevel.LOW) < level:
            self.findings[reason] = level

    def result(self) -> RiskAssessment:
        if not self.findings:
            return RiskAssessment()
        ordered = sorted(self.findings.items(), key=lambda item: -item[1])
        return RiskAssessment(
            level=max(self.findings.values()),
            reasons=tuple(reason for reason, _ in ordered),
        )


# ---------------------------------------------------------------------------
# A small shell tokenizer
# ---------------------------------------------------------------------------

_MAX_DEPTH = 4
_PLACEHOLDER = "\x00"  # stands in for a command substitution inside a word


@dataclass
class _Simple:
    """One simple command: words, redirections and any text fed to its stdin."""

    words: list[str]
    redirects: list[tuple[str, str]]  # ("in" | "out", target)
    pipe_next: bool = False
    stdin_text: str = ""


def _parse(text: str) -> tuple[list[_Simple], list[str]]:
    """Split a command line into simple commands.

    Returns the commands plus the text of every command substitution
    (``$(...)``, backticks, ``<(...)``) found along the way. Quoting, escapes,
    here-documents and the ``; && || | &`` separators are understood; anything
    fancier degrades to "looks like a word", which only makes the label less
    precise, never wrong in the dangerous direction for the shapes we look for.
    """
    commands: list[_Simple] = []
    nested: list[str] = []
    words: list[str] = []
    redirects: list[tuple[str, str]] = []
    stdin_parts: list[str] = []
    buf: list[str] = []
    in_word = False
    redirect_next = ""  # "in" / "out" while the next word is a redirect target
    herestring_next = False
    heredoc: tuple[str, bool, int] | None = None  # delimiter, strip tabs, owner index
    quote = ""
    i, n = 0, len(text)

    def flush_word() -> None:
        nonlocal buf, in_word, redirect_next, herestring_next
        if in_word:
            word = "".join(buf)
            if redirect_next:
                redirects.append((redirect_next, word))
            elif herestring_next:
                stdin_parts.append(word)
            else:
                words.append(word)
            redirect_next = ""
            herestring_next = False
        buf, in_word = [], False

    def flush_command(pipe: bool = False) -> None:
        nonlocal words, redirects, stdin_parts, redirect_next, herestring_next
        flush_word()
        if words or redirects or stdin_parts:
            commands.append(
                _Simple(words, redirects, pipe_next=pipe, stdin_text="\n".join(stdin_parts))
            )
        words, redirects, stdin_parts = [], [], []
        redirect_next = ""
        herestring_next = False

    while i < n:
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = ""
            else:
                buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "\n" and not quote:
                i += 2
                continue
            buf.append(text[i + 1])
            in_word = True
            i += 2
            continue
        if quote == '"' and c == '"':
            quote = ""
            i += 1
            continue
        if c == "`" or (c == "$" and text.startswith("$(", i)):
            inner, i = _read_substitution(text, i)
            nested.append(inner)
            buf.append(_PLACEHOLDER)
            in_word = True
            continue
        if quote == '"':
            buf.append(c)
            i += 1
            continue

        # --- unquoted text ---
        if c in "'\"":
            quote = c
            in_word = True
            i += 1
        elif c in "<>" and text.startswith(("<(", ">("), i):
            inner, i = _read_substitution(text, i + 1)
            nested.append(inner)
            buf.append(_PLACEHOLDER)
            in_word = True
        elif c in " \t":
            flush_word()
            i += 1
        elif c == "\n":
            flush_command()
            i += 1
            if heredoc is not None:
                delimiter, strip_tabs, owner = heredoc
                heredoc = None
                body, i = _read_heredoc(text, i, delimiter, strip_tabs)
                if owner < len(commands):
                    commands[owner].stdin_text = (commands[owner].stdin_text + "\n" + body).strip("\n")
        elif c in ";()":
            flush_command()
            i += 1
        elif c == "&":
            if text.startswith("&&", i):
                flush_command()
                i += 2
            elif text.startswith("&>", i):
                flush_word()
                i += 3 if text.startswith("&>>", i) else 2
                redirect_next = "out"
            else:
                flush_command()
                i += 1
        elif c == "|":
            if text.startswith("||", i):
                flush_command()
                i += 2
            else:
                flush_command(pipe=True)
                i += 2 if text.startswith("|&", i) else 1
        elif c in "<>":
            # A file-descriptor prefix such as the 2 in "2>&1" is not a word.
            if in_word and "".join(buf).isdigit():
                buf, in_word = [], False
            else:
                flush_word()
            if text.startswith("<<<", i):
                herestring_next = True
                i += 3
            elif text.startswith("<<", i):
                i += 2
                strip_tabs = text.startswith("-", i)
                if strip_tabs:
                    i += 1
                delimiter, i = _read_delimiter(text, i)
                heredoc = (delimiter, strip_tabs, len(commands))
            elif c == "<":
                redirect_next = "in"
                i += 1
            else:
                i += 1
                if text.startswith(">", i) or text.startswith("|", i):
                    i += 1
                elif text.startswith("&", i):
                    if i + 1 < n and (text[i + 1].isdigit() or text[i + 1] == "-"):
                        i += 2  # ">&2": duplicates a descriptor, no file involved
                        continue
                    i += 1
                redirect_next = "out"
        else:
            buf.append(c)
            in_word = True
            i += 1

    flush_command()
    return commands, nested


def _read_substitution(text: str, i: int) -> tuple[str, int]:
    """Return the inside of a substitution starting at ``i`` and the index after it."""
    if text[i] == "`":
        j = i + 1
        while j < len(text) and text[j] != "`":
            j += 2 if text[j] == "\\" else 1
        return text[i + 1 : j], min(j + 1, len(text))
    start = text.index("(", i) + 1
    depth, j = 1, start
    while j < len(text) and depth:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        j += 1
    return text[start : j - 1 if depth == 0 else j], j


def _read_delimiter(text: str, i: int) -> tuple[str, int]:
    while i < len(text) and text[i] in " \t":
        i += 1
    chars: list[str] = []
    while i < len(text) and text[i] not in " \t\n;|&<>()":
        if text[i] not in "'\"\\":
            chars.append(text[i])
        i += 1
    return "".join(chars), i


def _read_heredoc(text: str, i: int, delimiter: str, strip_tabs: bool) -> tuple[str, int]:
    lines: list[str] = []
    n = len(text)
    while i < n:
        j = text.find("\n", i)
        line = text[i:] if j == -1 else text[i:j]
        i = n if j == -1 else j + 1
        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
            break
        lines.append(line)
    return "\n".join(lines), i


# ---------------------------------------------------------------------------
# Command analysis
# ---------------------------------------------------------------------------

_FORK_BOMB = re.compile(r"(\w+|:)\s*\(\s*\)\s*\{[^}]*\1\s*\|\s*\1\s*&")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DOWNLOAD_RE = re.compile(r"\b(curl|wget|fetch|aria2c)\b")

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
_INTERPRETER_RE = re.compile(r"^(python[0-9.]*|perl|ruby|node|deno|bun|php|lua)$")
_DOWNLOADERS = {"curl", "wget", "fetch", "aria2c", "http", "https", "nc", "ncat", "netcat"}
_KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{", "}", "fi", "done"}

# Wrappers that run the command that follows; the flags that take a value are listed so
# the value is not mistaken for the command.
_WRAPPERS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "-C", "-S"}),
    "command": frozenset(),
    "builtin": frozenset(),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "time": frozenset({"-f", "-o"}),
    "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "setsid": frozenset(),
    "caffeinate": frozenset({"-t", "-w"}),
    "timeout": frozenset({"-s", "-k"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-s", "-E", "-d", "-a"}),
    "watch": frozenset({"-n", "-d"}),
}
_PRIVILEGE_WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-T", "-U", "-R"}),
    "doas": frozenset({"-u", "-C"}),
}

_PROTECTED_DIRS = frozenset(
    {
        "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/mnt",
        "/media", "/opt", "/proc", "/root", "/sbin", "/srv", "/sys", "/usr", "/var",
        "/Users", "/System", "/Library", "/Applications", "/Volumes", "/private",
    }
)
_SYSTEM_PREFIXES = (
    "/etc/", "/usr/", "/bin/", "/sbin/", "/boot/", "/lib/", "/lib64/", "/opt/",
    "/System/", "/Library/", "/private/etc/",
)

_DEVICE_RE = re.compile(r"^/dev/(sd|hd|vd|xvd|nvme|disk|rdisk|mmcblk|loop|dm-|md\d|mapper/|sr\d)")
_PERSISTENCE_RE = re.compile(
    r"(^|/)(\.bashrc|\.bash_profile|\.bash_login|\.bash_logout|\.profile|\.zshrc|\.zshenv"
    r"|\.zprofile|\.zlogin|\.zlogout|\.cshrc|\.tcshrc|\.inputrc|\.gitconfig|\.xinitrc"
    r"|\.xprofile|crontab|config\.fish)$"
    r"|/Library/Launch(Agents|Daemons)/|/etc/(cron|profile|bash|zsh|environment|ld\.so|systemd/|init)"
    r"|/systemd/(system|user)/|/\.git/(hooks/|config$)"
)
_SECRET_FILE_RE = re.compile(
    r"(^|/)(\.env(\.[\w.-]+)?|\.npmrc|id_(rsa|dsa|ecdsa|ed25519)|[^/]*\.(pem|p12|pfx|keystore|jks)"
    r"|[^/]*_(secret|token)s?(\.\w+)?|(secrets?|credentials?|token)\.(json|ya?ml|toml))$",
    re.IGNORECASE,
)
_METADATA_HOSTS = ("169.254.169.254", "169.254.170.2", "metadata.google.internal", "100.100.100.200")
_UPLOAD_FLAGS = {
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F", "--form",
    "-T", "--upload-file", "--post-data", "--post-file", "--body-data", "--body-file",
}
_SYSTEM_PACKAGE_MANAGERS = {"apt", "apt-get", "yum", "dnf", "pacman", "apk", "zypper", "snap", "port"}
_PACKAGE_MANAGERS = {"pip", "pip3", "uv", "npm", "pnpm", "yarn", "bun", "cargo", "gem", "brew", "poetry"}
_INSTALL_VERBS = {"install", "i", "ci", "add", "remove", "rm", "uninstall", "update", "upgrade", "sync"}
_WRITE_COMMANDS = {
    "cp", "mv", "ln", "install", "tee", "touch", "truncate", "rsync", "chmod", "chown", "chgrp", "dd",
}
_SHUTDOWN = {"shutdown", "reboot", "halt", "poweroff"}


def _assess_shell_text(text: str, ctx: _Context, *, depth: int) -> None:
    if depth > _MAX_DEPTH:
        ctx.add(RiskLevel.HIGH, "shell commands nested too deeply to inspect")
        return
    if _FORK_BOMB.search(text):
        ctx.add(RiskLevel.CRITICAL, "fork bomb")
    commands, nested = _parse(text)
    previous = ctx.downloads_in_substitution
    ctx.downloads_in_substitution = any(_DOWNLOAD_RE.search(inner) for inner in nested)
    for inner in nested:
        _assess_shell_text(inner, ctx, depth=depth + 1)
    pipeline: list[_Simple] = []
    for command in commands:
        pipeline.append(command)
        if not command.pipe_next:
            _assess_pipeline(pipeline, ctx, depth)
            pipeline = []
    if pipeline:
        _assess_pipeline(pipeline, ctx, depth)
    ctx.downloads_in_substitution = previous


def _assess_pipeline(pipeline: list[_Simple], ctx: _Context, depth: int) -> None:
    for command in pipeline:
        _assess_simple(command, ctx, depth)
    if len(pipeline) < 2:
        return
    last_words = _strip_prefixes(pipeline[-1].words, _Context(home=ctx.home))
    if not last_words or not _reads_script_from_stdin(last_words):
        return
    producers = pipeline[:-1]
    if any(_command_name(_strip_prefixes(p.words, _Context(home=ctx.home))) in _DOWNLOADERS for p in producers):
        ctx.add(RiskLevel.HIGH, "runs downloaded code")
        return
    if all(_is_literal_producer(p, ctx) for p in producers):
        # "echo 'rm -rf /' | sh": the text being piped is the command.
        for producer in producers:
            payload = " ".join(producer.words[1:]) + "\n" + producer.stdin_text
            _assess_shell_text(payload, ctx, depth=depth + 1)
    else:
        ctx.add(RiskLevel.HIGH, "pipes data into an interpreter")


def _is_literal_producer(command: _Simple, ctx: _Context) -> bool:
    """True for ``echo``/``printf`` and for ``cat`` fed by a here-document."""
    words = _strip_prefixes(command.words, _Context(home=ctx.home))
    name = _command_name(words)
    if name in {"echo", "printf"}:
        return True
    return name == "cat" and bool(command.stdin_text) and all(w.startswith("-") for w in words[1:])


def _reads_script_from_stdin(words: list[str]) -> bool:
    name = _command_name(words)
    if name not in _SHELLS and not _INTERPRETER_RE.match(name):
        return False
    operands = [w for w in words[1:] if not w.startswith("-")]
    return not operands or words[1:] in (["-"], ["-s"])


def _command_name(words: list[str]) -> str:
    return os.path.basename(words[0]).lower() if words else ""


def _strip_prefixes(words: list[str], ctx: _Context) -> list[str]:
    """Drop assignments, shell keywords and wrapper commands in front of the real command."""
    i = 0
    while i < len(words):
        word = words[i]
        if _ASSIGNMENT.match(word):
            i += 1
            continue
        name = os.path.basename(word).lower()
        if name in _KEYWORDS:
            i += 1
            continue
        if name in _PRIVILEGE_WRAPPERS:
            ctx.add(RiskLevel.HIGH, "runs with elevated privileges")
            i = _skip_flags(words, i + 1, _PRIVILEGE_WRAPPERS[name])
            continue
        if name in _WRAPPERS:
            if name == "env" and all(
                _ASSIGNMENT.match(w) or w.startswith("-") for w in words[i + 1 :]
            ):
                ctx.add(RiskLevel.MEDIUM, "prints the environment, which may hold secrets")
            i = _skip_flags(words, i + 1, _WRAPPERS[name])
            if name == "timeout" and i < len(words) and re.match(r"^\d", words[i]):
                i += 1
            continue
        break
    return words[i:]


def _skip_flags(words: list[str], i: int, flags_with_value: frozenset[str]) -> int:
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        i += 2 if words[i] in flags_with_value else 1
    return i


def _assess_simple(command: _Simple, ctx: _Context, depth: int) -> None:
    words = _strip_prefixes(command.words, ctx)

    for kind, target in command.redirects:
        _assess_word_path(target, ctx, write=(kind == "out"))
        if kind == "out" and target.startswith("/dev/tcp/"):
            ctx.add(RiskLevel.CRITICAL, "opens a raw network socket through /dev/tcp")

    if not words:
        return
    name = _command_name(words)
    args = words[1:]

    if any(w.startswith(("/dev/tcp/", "/dev/udp/")) for w in args):
        ctx.add(RiskLevel.CRITICAL, "opens a raw network socket through /dev/tcp")

    # Credential paths and likely secret files, wherever they appear in the arguments.
    write_like = name in _WRITE_COMMANDS or (name == "sed" and any(a.startswith("-i") for a in args))
    for arg in args:
        _assess_word_path(arg, ctx, write=write_like and not arg.startswith("-"))

    if name in _SHELLS:
        _assess_shell_invocation(name, args, command, ctx, depth)
    elif name in {"eval", "source", "."}:
        script = " ".join(args)
        if _PLACEHOLDER in script and ctx.downloads_in_substitution:
            ctx.add(RiskLevel.HIGH, "runs downloaded code")
        if name == "eval":
            _assess_shell_text(script, ctx, depth=depth + 1)
    elif _INTERPRETER_RE.match(name) or name == "osascript":
        _assess_interpreter(name, args, ctx, depth)

    handler = _HANDLERS.get(name)
    if handler is not None:
        handler(args, ctx)
    elif name.startswith("mkfs"):
        ctx.add(RiskLevel.CRITICAL, "formats a filesystem")


def _assess_shell_invocation(
    name: str, args: list[str], command: _Simple, ctx: _Context, depth: int
) -> None:
    if ctx.downloads_in_substitution and any(_PLACEHOLDER in a for a in args):
        ctx.add(RiskLevel.HIGH, "runs downloaded code")
    script: str | None = None
    for index, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            if index + 1 < len(args):
                script = args[index + 1]
            break
    if script is not None:
        _assess_shell_text(script, ctx, depth=depth + 1)
    elif command.stdin_text and not [a for a in args if not a.startswith("-")]:
        _assess_shell_text(command.stdin_text, ctx, depth=depth + 1)


_STRING_LITERAL = re.compile(r"""(['"])((?:\\.|(?!\1).)*)\1""", re.DOTALL)
_PY_RMTREE_ROOT = re.compile(r"""rmtree\(\s*['"](/|~|/home|/Users|/etc|/usr)/?['"]""")


def _assess_interpreter(name: str, args: list[str], ctx: _Context, depth: int) -> None:
    """Scan inline code (``python -c ...``) for string literals that are shell commands."""
    for index, arg in enumerate(args):
        if arg in {"-c", "-e", "--eval", "-r"} and index + 1 < len(args):
            code = args[index + 1]
            if _PY_RMTREE_ROOT.search(code):
                ctx.add(RiskLevel.CRITICAL, "recursively deletes a system or home directory")
            for _, literal in _STRING_LITERAL.findall(code):
                if " " in literal.strip():
                    _assess_shell_text(literal, ctx, depth=depth + 1)


# --- paths -----------------------------------------------------------------


def _expand_home(word: str, home: str) -> str:
    if word == "~" or word.startswith("~/"):
        word = home + word[1:]
    return word.replace("${HOME}", home).replace("$HOME", home)


def _is_protected_target(word: str, home: str) -> bool:
    path = _expand_home(word, home)
    if any(ch in path for ch in "$`" + _PLACEHOLDER):
        return False  # a variable we cannot resolve
    path = re.sub(r"(/\.?\*+)+$", "", path) or "/"
    path = os.path.normpath(path)
    return path in _PROTECTED_DIRS or path == home or path == os.path.dirname(home)


def _assess_word_path(word: str, ctx: _Context, *, write: bool) -> None:
    """Label a command argument that may be a path."""
    if not word or word.startswith("-") and "=" not in word:
        return
    candidates = [word]
    if "=" in word:
        candidates.append(word.split("=", 1)[1])
    for candidate in candidates:
        if not candidate or any(ch.isspace() for ch in candidate) or _PLACEHOLDER in candidate:
            continue
        path = _expand_home(candidate, ctx.home)
        forms = [path]
        if not path.startswith("/") and "/" in path:
            forms.append("/" + path)  # ".ssh/id_rsa" relative to the home directory
        for form in forms:
            if match_sensitive_path(form) is not None:
                ctx.add(RiskLevel.CRITICAL, f"touches a credential path ({candidate})")
                return
        if _SECRET_FILE_RE.search(path):
            ctx.add(RiskLevel.MEDIUM, f"touches a likely secret file ({os.path.basename(path)})")
        if write:
            _assess_write_target(path, ctx)


def _assess_write_target(path: str, ctx: _Context) -> None:
    if _DEVICE_RE.match(path):
        ctx.add(RiskLevel.CRITICAL, f"writes to a disk device ({path})")
    elif _PERSISTENCE_RE.search(path):
        ctx.add(RiskLevel.HIGH, "changes a startup, scheduling or git-hook file")
    elif path.startswith(_SYSTEM_PREFIXES):
        ctx.add(RiskLevel.HIGH, "writes to a system location")


def _assess_file_path(
    file_path: str, *, is_read_only: bool, cwd: str | Path | None, ctx: _Context
) -> None:
    if _SECRET_FILE_RE.search(file_path):
        ctx.add(RiskLevel.MEDIUM, f"touches a likely secret file ({os.path.basename(file_path)})")
    if is_read_only:
        return
    if _PERSISTENCE_RE.search(file_path):
        ctx.add(RiskLevel.HIGH, "changes a startup, scheduling or git-hook file")
    elif file_path.startswith(_SYSTEM_PREFIXES):
        ctx.add(RiskLevel.HIGH, "writes to a system location")
    elif cwd is not None and not _is_inside(file_path, cwd) and not _is_temp(file_path):
        ctx.add(RiskLevel.MEDIUM, "writes outside the working directory")
    if "/.github/workflows/" in file_path:
        ctx.add(RiskLevel.MEDIUM, "changes a CI workflow")


def _is_inside(path: str, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def _is_temp(path: str) -> bool:
    return path.startswith(("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/"))


# --- per-command rules ------------------------------------------------------


def _split_flags(args: list[str]) -> tuple[set[str], list[str]]:
    """Return (short and long flags seen, operands). Short flag clusters are split."""
    flags: set[str] = set()
    operands: list[str] = []
    options_done = False
    for arg in args:
        if options_done or not arg.startswith("-") or arg == "-":
            operands.append(arg)
        elif arg == "--":
            options_done = True
        elif arg.startswith("--"):
            flags.add(arg.split("=", 1)[0])
        else:
            flags.update("-" + letter for letter in arg[1:])
    return flags, operands


def _rm(args: list[str], ctx: _Context) -> None:
    flags, targets = _split_flags(args)
    recursive = bool(flags & {"-r", "-R", "--recursive"})
    if "--no-preserve-root" in flags:
        ctx.add(RiskLevel.CRITICAL, "disables the protection of the root directory")
    if recursive:
        for target in targets:
            if _is_protected_target(target, ctx.home):
                ctx.add(RiskLevel.CRITICAL, f"recursively deletes {target}")
        ctx.add(RiskLevel.HIGH, "deletes recursively")
    elif flags & {"-f", "--force"}:
        ctx.add(RiskLevel.MEDIUM, "force-deletes files")
    else:
        ctx.add(RiskLevel.MEDIUM, "deletes files")


def _find(args: list[str], ctx: _Context) -> None:
    starts: list[str] = []
    for arg in args:
        if arg.startswith("-") or arg in {"(", "!"}:
            break
        starts.append(arg)
    if "-delete" in args:
        if any(_is_protected_target(s, ctx.home) for s in starts):
            ctx.add(RiskLevel.CRITICAL, "deletes everything under a system or home directory")
        ctx.add(RiskLevel.HIGH, "deletes every file it finds")
    for index, arg in enumerate(args):
        if arg in {"-exec", "-execdir", "-ok", "-okdir"}:
            inner: list[str] = []
            for token in args[index + 1 :]:
                if token in {";", "\\;", "+"}:
                    break
                inner.append(token)
            _assess_simple(_Simple(inner, []), ctx, depth=_MAX_DEPTH)


def _chmod_like(args: list[str], ctx: _Context) -> None:
    flags, operands = _split_flags(args)
    if flags & {"-R", "--recursive"}:
        if any(_is_protected_target(t, ctx.home) for t in operands):
            ctx.add(RiskLevel.CRITICAL, "changes permissions or ownership across a system or home directory")
        ctx.add(RiskLevel.HIGH, "changes permissions or ownership recursively")
    if any(_world_writable(o) for o in operands):
        ctx.add(RiskLevel.MEDIUM, "makes files world-writable")


def _world_writable(mode: str) -> bool:
    if re.fullmatch(r"[0-7]{3,4}", mode):
        return int(mode[-1]) & 2 == 2
    return bool(re.fullmatch(r"[ao][+=][rwxXst]*w[rwxXst]*", mode))


def _dd(args: list[str], ctx: _Context) -> None:
    for arg in args:
        if arg.startswith("of=") and _DEVICE_RE.match(arg[3:]):
            ctx.add(RiskLevel.CRITICAL, "writes to a disk device")
    ctx.add(RiskLevel.MEDIUM, "copies raw data with dd")


def _git(args: list[str], ctx: _Context) -> None:
    rest = list(args)
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in {"-C", "-c", "--git-dir", "--work-tree"} else rest[1:]
    if not rest:
        return
    sub, flags = rest[0], set(rest[1:])
    if sub == "push":
        if flags & {"--force", "-f", "--force-with-lease", "--delete", "-d", "--mirror"} or any(
            a.startswith("+") for a in rest[1:]
        ):
            ctx.add(RiskLevel.HIGH, "force-pushes or deletes remote history")
        else:
            ctx.add(RiskLevel.MEDIUM, "publishes commits")
    elif sub == "reset" and "--hard" in flags:
        ctx.add(RiskLevel.HIGH, "discards uncommitted changes")
    elif sub == "clean" and any(f.startswith("-") and "f" in f for f in flags):
        ctx.add(RiskLevel.HIGH, "deletes untracked files")
    elif sub in {"filter-branch", "filter-repo"}:
        ctx.add(RiskLevel.HIGH, "rewrites repository history")
    elif sub in {"checkout", "restore", "switch"} and flags & {"--force", "-f", ".", "--"}:
        ctx.add(RiskLevel.MEDIUM, "discards local changes")
    elif (
        sub == "rebase"
        or (sub == "branch" and flags & {"-d", "-D", "-m", "-M", "-f", "--delete", "--move", "--force"})
        or (sub == "tag" and flags & {"-d", "-f", "--delete", "--force"})
        or (sub == "stash" and flags & {"drop", "clear"})
    ):
        ctx.add(RiskLevel.MEDIUM, "changes branch or history state")
    elif sub == "config" and flags & {"--global", "--system"}:
        ctx.add(RiskLevel.MEDIUM, "changes global git configuration")
    elif sub in {"clone", "fetch", "pull"}:
        ctx.add(RiskLevel.MEDIUM, "network access")


def _package_manager(name: str) -> Callable[[list[str], _Context], None]:
    def handler(args: list[str], ctx: _Context) -> None:
        flags, operands = _split_flags(args)
        verbs = set(operands[:2])
        changes_packages = bool(verbs & (_INSTALL_VERBS | {"dist-upgrade"}))
        if name in _SYSTEM_PACKAGE_MANAGERS and changes_packages:
            ctx.add(RiskLevel.HIGH, "changes system packages")
        elif changes_packages:
            ctx.add(RiskLevel.MEDIUM, "installs or changes packages")
        if verbs & {"publish", "upload", "push"} or (name == "uv" and "publish" in verbs):
            ctx.add(RiskLevel.HIGH, "publishes a package")
        if "--break-system-packages" in flags:
            ctx.add(RiskLevel.HIGH, "overrides the system Python protection")

    return handler


def _network(args: list[str], ctx: _Context, *, short_upload_flags: tuple[str, ...] = ()) -> None:
    flags, _ = _split_flags(args)
    ctx.add(RiskLevel.MEDIUM, "network access")
    uploads = any(
        a.split("=", 1)[0] in _UPLOAD_FLAGS or (short_upload_flags and a.startswith(short_upload_flags))
        for a in args
        if a.startswith("-")
    )
    method = bool(flags & {"-X", "--request", "--method"}) and any(
        a.upper() in {"POST", "PUT", "PATCH", "DELETE"} for a in args if not a.startswith("-")
    )
    if uploads or method:
        ctx.add(RiskLevel.HIGH, "sends data to a remote host")
    if any(host in a for a in args for host in _METADATA_HOSTS):
        ctx.add(RiskLevel.HIGH, "requests a cloud metadata endpoint")


def _curl(args: list[str], ctx: _Context) -> None:
    _network(args, ctx, short_upload_flags=("-d", "-F", "-T"))


def _remote_copy(args: list[str], ctx: _Context) -> None:
    if any(re.match(r"^[\w.@-]+:", a) for a in args if not a.startswith("-")):
        ctx.add(RiskLevel.HIGH, "copies data to or from a remote host")
    else:
        ctx.add(RiskLevel.MEDIUM, "copies files in bulk")


def _ssh(args: list[str], ctx: _Context) -> None:
    ctx.add(RiskLevel.HIGH, "opens a remote connection")


def _netcat(args: list[str], ctx: _Context) -> None:
    flags, _ = _split_flags(args)
    if flags & {"-e", "-c"} or any(a.lower().startswith(("exec:", "system:")) for a in args):
        ctx.add(RiskLevel.CRITICAL, "binds or connects a shell to the network")
    else:
        ctx.add(RiskLevel.HIGH, "opens a raw network connection")


def _shutdown(args: list[str], ctx: _Context) -> None:
    ctx.add(RiskLevel.CRITICAL, "shuts down or reboots the machine")


def _kill(args: list[str], ctx: _Context) -> None:
    if len(args) >= 2 and args[-1] == "-1":
        ctx.add(RiskLevel.CRITICAL, "kills every process the user owns")
    else:
        ctx.add(RiskLevel.MEDIUM, "terminates processes")


def _systemctl(args: list[str], ctx: _Context) -> None:
    _, operands = _split_flags(args)
    verb = operands[0] if operands else ""
    if verb in {"poweroff", "reboot", "halt", "kexec", "suspend", "hibernate"}:
        ctx.add(RiskLevel.CRITICAL, "shuts down or reboots the machine")
    elif verb in {"start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "edit", "daemon-reload"}:
        ctx.add(RiskLevel.HIGH, "changes system services")


def _launchctl(args: list[str], ctx: _Context) -> None:
    _, operands = _split_flags(args)
    if operands and operands[0] in {"load", "unload", "bootstrap", "bootout", "enable", "disable", "kickstart", "submit", "remove"}:
        ctx.add(RiskLevel.HIGH, "changes launchd jobs")


def _crontab(args: list[str], ctx: _Context) -> None:
    flags, _ = _split_flags(args)
    if "-l" not in flags:
        ctx.add(RiskLevel.HIGH, "changes the scheduled jobs")


def _diskutil(args: list[str], ctx: _Context) -> None:
    verb = (args[0] if args else "").lower()
    if verb.startswith(("erase", "zero", "random", "secureerase", "partition", "repartition", "reformat")) or (
        verb == "apfs" and len(args) > 1 and args[1].lower().startswith("delete")
    ):
        ctx.add(RiskLevel.CRITICAL, "erases or repartitions a disk")


def _security(args: list[str], ctx: _Context) -> None:
    verb = args[0] if args else ""
    if verb.startswith(("find-", "dump-", "export", "unlock-")):
        ctx.add(RiskLevel.CRITICAL, "reads the system keychain")
    elif verb.startswith(("delete-", "add-", "set-", "create-")) or verb == "import":
        ctx.add(RiskLevel.HIGH, "changes the system keychain")


def _partition_tool(args: list[str], ctx: _Context) -> None:
    flags, _ = _split_flags(args)
    if "-l" not in flags and "--list" not in flags:
        ctx.add(RiskLevel.HIGH, "edits disk partitions")


def _docker(args: list[str], ctx: _Context) -> None:
    flags, operands = _split_flags(args)
    verb = operands[0] if operands else ""
    if verb == "push":
        ctx.add(RiskLevel.HIGH, "publishes an image")
    elif verb in {"run", "create"} and (
        "--privileged" in flags
        or any(a in {"--pid=host", "--net=host", "--network=host"} for a in args)
        or any(a.startswith(("/:", "/var/run/docker.sock")) or a.endswith(":/host") for a in args)
    ):
        ctx.add(RiskLevel.HIGH, "starts a container with access to the host")
    elif verb in {"rm", "rmi", "kill", "stop"}:
        ctx.add(RiskLevel.MEDIUM, "removes containers or images")
    elif verb == "system" and "prune" in operands:
        ctx.add(RiskLevel.HIGH, "deletes unused Docker data")


def _privileged_admin(args: list[str], ctx: _Context) -> None:
    ctx.add(RiskLevel.HIGH, "changes accounts or passwords")


def _firewall(args: list[str], ctx: _Context) -> None:
    ctx.add(RiskLevel.HIGH, "changes firewall or network configuration")


def _osascript(args: list[str], ctx: _Context) -> None:
    ctx.add(RiskLevel.HIGH, "scripts other applications")


def _recursive_search(args: list[str], ctx: _Context, *, always_recursive: bool = False) -> None:
    """grep -r, rg and friends over a whole home or system directory walk through credential files."""
    flags, operands = _split_flags(args)
    if not always_recursive and not flags & {"-r", "-R", "--recursive"}:
        return
    if any(_is_protected_target(operand, ctx.home) for operand in operands):
        ctx.add(RiskLevel.HIGH, "searches a whole home or system directory, which holds credential files")


def _printenv(args: list[str], ctx: _Context) -> None:
    ctx.add(RiskLevel.MEDIUM, "prints the environment, which may hold secrets")


def _shred(args: list[str], ctx: _Context) -> None:
    flags, targets = _split_flags(args)
    if any(_DEVICE_RE.match(t) for t in targets):
        ctx.add(RiskLevel.CRITICAL, "overwrites a disk device")
    ctx.add(RiskLevel.HIGH, "overwrites files irrecoverably")


_HANDLERS = {
    "rm": _rm,
    "rmdir": _rm,
    "unlink": _rm,
    "find": _find,
    "chmod": _chmod_like,
    "chown": _chmod_like,
    "chgrp": _chmod_like,
    "dd": _dd,
    "git": _git,
    "curl": _curl,
    "wget": _network,
    "http": _network,
    "https": _network,
    "aria2c": _network,
    "scp": _remote_copy,
    "rsync": _remote_copy,
    "sftp": _ssh,
    "ssh": _ssh,
    "mosh": _ssh,
    "telnet": _ssh,
    "ftp": _ssh,
    "nc": _netcat,
    "ncat": _netcat,
    "netcat": _netcat,
    "socat": _netcat,
    "kill": _kill,
    "killall": _kill,
    "pkill": _kill,
    "systemctl": _systemctl,
    "launchctl": _launchctl,
    "crontab": _crontab,
    "diskutil": _diskutil,
    "security": _security,
    "docker": _docker,
    "podman": _docker,
    "fdisk": _partition_tool,
    "parted": _partition_tool,
    "gdisk": _partition_tool,
    "sfdisk": _partition_tool,
    "mkswap": lambda args, ctx: ctx.add(RiskLevel.CRITICAL, "formats a swap device"),
    "wipefs": lambda args, ctx: ctx.add(RiskLevel.CRITICAL, "wipes filesystem signatures"),
    "blkdiscard": lambda args, ctx: ctx.add(RiskLevel.CRITICAL, "discards every block of a device"),
    "shred": _shred,
    "passwd": _privileged_admin,
    "chpasswd": _privileged_admin,
    "useradd": _privileged_admin,
    "userdel": _privileged_admin,
    "usermod": _privileged_admin,
    "groupadd": _privileged_admin,
    "visudo": _privileged_admin,
    "iptables": _firewall,
    "ip6tables": _firewall,
    "nft": _firewall,
    "ufw": _firewall,
    "pfctl": _firewall,
    "osascript": _osascript,
    "printenv": _printenv,
    "grep": _recursive_search,
    "egrep": _recursive_search,
    "fgrep": _recursive_search,
    "rg": lambda args, ctx: _recursive_search(args, ctx, always_recursive=True),
    "ag": lambda args, ctx: _recursive_search(args, ctx, always_recursive=True),
    "ack": lambda args, ctx: _recursive_search(args, ctx, always_recursive=True),
    **{shutdown: _shutdown for shutdown in _SHUTDOWN},
    **{pm: _package_manager(pm) for pm in _PACKAGE_MANAGERS | _SYSTEM_PACKAGE_MANAGERS},
}
