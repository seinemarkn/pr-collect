#!/usr/bin/env python3
"""pr-collect — collect a GitHub pull request's review data into one file.

Run at the root of a clone, on the branch you are working on:

    pr-collect

It reads the branch, finds the ticket number and the pull request, gathers the
pull request's record, conversation, commits, checks, and diff from git and the
GitHub CLI, and writes pr-data.txt at the root of the clone. Nothing else is
touched. Requires Python 3.11+, git, and the GitHub CLI (gh) logged in.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__version__ = "0.1.0"

OUTPUT_NAME = "pr-data.txt"
MIN_TICKET_NUMBER = 100
RETRY_LIMIT = 3
DEFAULT_RETRY_WAIT = 60


class CollectError(Exception):
    """A problem the user has to fix. The message says what and how."""


# ---------------------------------------------------------------------------
# The two seams: every external command and every question go through these.
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    argv: list[str]
    stdout: str
    stderr: str
    code: int

    @property
    def ok(self) -> bool:
        return self.code == 0


class Shell:
    """Runs gh and git. The one place the tool shells out; tests replace it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> CommandResult:
        self.calls.append(list(argv))
        try:
            done = subprocess.run(argv, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return CommandResult(list(argv), "", f"command not found: {argv[0]}", 127)
        return CommandResult(list(argv), done.stdout, done.stderr, done.returncode)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    @property
    def gh_calls(self) -> int:
        return sum(1 for argv in self.calls if argv and argv[0] == "gh")


class Terminal:
    """Talks to the user. The one place the tool reads the terminal; tests replace it."""

    def __init__(self, out=None, err=None, stdin=None) -> None:
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.stdin = stdin or sys.stdin

    def say(self, line: str) -> None:
        print(line, file=self.out)

    def warn(self, line: str) -> None:
        print(f"pr-collect: {line}", file=self.err)

    @property
    def interactive(self) -> bool:
        isatty = getattr(self.stdin, "isatty", None)
        return bool(isatty and isatty())

    def ask(self, prompt: str) -> str:
        print(prompt, end="", file=self.out, flush=True)
        line = self.stdin.readline()
        return line.strip()


@dataclass
class Options:
    number: int | None = None
    ticket: str | None = None
    yes: bool = False
    no_files: bool = False
    full_logs: bool = False
    related: bool = False


@dataclass
class Context:
    shell: Shell
    terminal: Terminal
    options: Options
    missing: list[str] = field(default_factory=list)  # parts not collected, with why
    notes: list[str] = field(default_factory=list)  # things said in the header


# ---------------------------------------------------------------------------
# Preflight: gh present and logged in, at the root of a clone with a GitHub remote.
# ---------------------------------------------------------------------------


@dataclass
class Repo:
    host: str
    owner: str
    name: str
    root: Path

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


REMOTE_RE = re.compile(
    r"^(?:(?:ssh://)?git@(?P<h1>[^:/]+)[:/]|https?://(?:[^@/]+@)?(?P<h2>[^/]+)/)"
    r"(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)


def parse_remote(url: str) -> tuple[str, str, str] | None:
    """Host, owner, and name from an ssh or https remote URL; None when it is neither."""
    m = REMOTE_RE.match(url.strip())
    if not m:
        return None
    host = m.group("h1") or m.group("h2")
    # An ssh config alias like github.com-personal still names the host before the dash.
    host = host.split("-")[0] if host.startswith("github.com-") else host
    return host, m.group("owner"), m.group("name")


def preflight(ctx: Context) -> Repo:
    sh = ctx.shell
    if not sh.run(["gh", "--version"]).ok:
        raise CollectError(
            "the GitHub CLI, gh, is not installed or not on the PATH. "
            "Install it and log in with: gh auth login"
        )
    top = sh.run(["git", "rev-parse", "--show-toplevel"])
    if not top.ok:
        raise CollectError("not inside a git repository. Run pr-collect at the root of a clone.")
    root = Path(top.stdout.strip()).resolve()
    if root != Path.cwd().resolve():
        raise CollectError(f"run pr-collect at the root of the clone: {root}")
    remote = sh.run(["git", "remote", "get-url", "origin"])
    if not remote.ok:
        raise CollectError("this clone has no remote named origin; pr-collect needs one on GitHub.")
    parsed = parse_remote(remote.stdout)
    if parsed is None:
        raise CollectError(f"the origin remote is not a GitHub repository URL: {remote.stdout.strip()}")
    host, owner, name = parsed
    if not sh.run(["gh", "auth", "status", "--hostname", host]).ok:
        raise CollectError(f"gh is not logged in to {host}. Run: gh auth login --hostname {host}")
    return Repo(host, owner, name, root)


# ==== cli ====


def parse_args(argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(
        prog="pr-collect",
        description=(
            "Collect a GitHub pull request's review data into one file, pr-data.txt, "
            "at the root of the clone you run it in."
        ),
        epilog=(
            "Run it at the root of a clone, on the branch of the pull request. The ticket "
            "number is read from the branch name and confirmed; -t gives it instead."
        ),
    )
    parser.add_argument(
        "number", nargs="?", type=int,
        help="the pull request number, when the branch has more than one or you are not on it",
    )
    parser.add_argument(
        "-t", "--ticket", metavar="TICKET",
        help="the ticket number (or full key) when the branch name does not carry it",
    )
    parser.add_argument("--yes", action="store_true", help="take the ticket number read from the branch without asking")
    parser.add_argument("--no-files", action="store_true", help="leave out the whole touched files at the head and at the base")
    parser.add_argument("--full-logs", action="store_true", help="keep every failing step's log whole instead of its last 200 lines")
    parser.add_argument("--related", action="store_true", help="add files at the head that name an identifier the diff adds or changes")
    parser.add_argument("--version", action="version", version=f"pr-collect {__version__}")
    ns = parser.parse_args(argv)
    return Options(
        number=ns.number, ticket=ns.ticket, yes=ns.yes,
        no_files=ns.no_files, full_logs=ns.full_logs, related=ns.related,
    )


def collect(ctx: Context) -> Path:
    """Run every part in order and write the file. Returns the file's path."""
    repo = preflight(ctx)
    ctx.terminal.say(f"pr-collect: {repo.full_name} on {repo.host}")
    raise CollectError("nothing past the preflight is built yet")


def main(argv: list[str] | None = None, shell: Shell | None = None, terminal: Terminal | None = None) -> int:
    options = parse_args(argv)
    ctx = Context(shell or Shell(), terminal or Terminal(), options)
    try:
        collect(ctx)
    except CollectError as err:
        ctx.terminal.warn(str(err))
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
