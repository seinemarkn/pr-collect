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


# ---------------------------------------------------------------------------
# Identity: the branch, the ticket number, the pull request.
# ---------------------------------------------------------------------------


@dataclass
class Ticket:
    number: int
    key: str | None = None  # the full key, letters and number, when the letters were seen
    source: str = ""  # where the number came from, in words

    @property
    def label(self) -> str:
        return self.key or str(self.number)


# A ticket key as JIRA writes it: upper-case project letters, a dash, the number.
KEY_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]+)-(\d+)(?!\d)")
TYPED_KEY_RE = re.compile(r"([A-Za-z][A-Za-z0-9]+)-(\d+)")
DIGITS_RE = re.compile(r"\d+")


def read_ticket(text: str, source: str) -> Ticket | None:
    """The ticket number in a branch name or a title: the key shape first (upper-case
    letters, a dash, digits; any project's letters), else the first run of digits greater
    than 100."""
    m = KEY_SHAPE_RE.search(text)
    if m:
        return Ticket(int(m.group(2)), m.group(0), f"the {source}, as the key {m.group(0)}")
    for d in DIGITS_RE.finditer(text):
        if int(d.group(0)) > MIN_TICKET_NUMBER:
            return Ticket(int(d.group(0)), None, f"the {source}, as the first number greater than {MIN_TICKET_NUMBER}")
    return None


def ticket_from_answer(answer: str, source: str) -> Ticket:
    """A ticket typed by the user: a number, or a full key."""
    answer = answer.strip()
    m = TYPED_KEY_RE.fullmatch(answer)
    if m:
        return Ticket(int(m.group(2)), f"{m.group(1).upper()}-{m.group(2)}", source)
    if answer.isdigit():
        return Ticket(int(answer), None, source)
    raise CollectError(f"not a ticket number: {answer!r}. Give the number (10800) or the full key (ABC-10800).")


def explain_branch(branch: str) -> str:
    """Why the branch name gave no ticket number."""
    if not DIGITS_RE.search(branch):
        return f"the branch name {branch!r} has no digits in it"
    return (
        f"the branch name {branch!r} has no run of digits greater than {MIN_TICKET_NUMBER}, "
        f"and no ticket key (upper-case letters, a dash, digits)"
    )


def current_branch(ctx: Context) -> str:
    r = ctx.shell.run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = r.stdout.strip()
    if not r.ok or not branch or branch == "HEAD":
        raise CollectError("no branch is checked out (detached HEAD). Check out the branch of the pull request.")
    return branch


def identify_ticket(ctx: Context, branch: str) -> Ticket:
    """The ticket number: -t wins and asks nothing; else the branch name, confirmed."""
    term = ctx.terminal
    if ctx.options.ticket:
        ticket = ticket_from_answer(ctx.options.ticket, "the -t option")
        term.say(f"ticket {ticket.label}, from the -t option")
        return ticket
    ticket = read_ticket(branch, "branch name")
    if ticket is None:
        reason = explain_branch(branch)
        if not term.interactive:
            raise CollectError(f"{reason}. Give the ticket with -t <number>, or rename the branch.")
        term.warn(f"{reason}. Give the ticket with -t <number>, or rename the branch.")
        answer = term.ask("Ticket number (empty to stop): ")
        if not answer.strip():
            raise CollectError(f"{reason}. No ticket number given; nothing collected.")
        ticket = ticket_from_answer(answer, "typed at the prompt")
        term.say(f"ticket {ticket.label}, typed at the prompt")
        return ticket
    term.say(f"ticket {ticket.label}, read from {ticket.source}")
    if ctx.options.yes or not term.interactive:
        if not ctx.options.yes:
            ctx.notes.append("The ticket number was taken from the branch name without confirmation (no terminal to ask on).")
        return ticket
    answer = term.ask(f"Is ticket {ticket.label} correct? [Y/n, or type the number] ").strip()
    if answer == "" or answer.lower() in ("y", "yes"):
        return ticket
    if answer.lower() in ("n", "no"):
        answer = term.ask("Ticket number (empty to stop): ")
        if not answer.strip():
            raise CollectError("no ticket number given; nothing collected.")
    ticket = ticket_from_answer(answer, "typed at the prompt")
    term.say(f"ticket {ticket.label}, typed at the prompt")
    return ticket


def check_ticket_against(ctx: Context, ticket: Ticket, text: str, where: str) -> Ticket:
    """Read the pull request's title or description the same way; say a difference, fill
    in the key's letters when only the text had them. The branch's number stands."""
    seen = read_ticket(text or "", where)
    if seen is None:
        return ticket
    if seen.number != ticket.number:
        ctx.notes.append(
            f"The {where} names ticket {seen.label}, not {ticket.label}; the branch's stands."
        )
        ctx.terminal.warn(f"the {where} names ticket {seen.label}, not {ticket.label}; the branch's stands")
        return ticket
    if seen.key and not ticket.key:
        return Ticket(ticket.number, seen.key, ticket.source + f"; the letters from the {where}")
    return ticket


def find_pull_request(ctx: Context, repo: Repo, branch: str) -> int:
    """The pull request number: the argument wins; else the open pull request for the branch."""
    if ctx.options.number is not None:
        return ctx.options.number
    r = gh_json(ctx, ["pr", "list", "-R", f"{repo.host}/{repo.full_name}", "--head", branch,
                      "--state", "open", "--json", "number,title,url"])
    if not isinstance(r, list) or not r:
        raise CollectError(
            f"no open pull request for the branch {branch!r} on {repo.full_name}. "
            "Open one, or give the pull request number: pr-collect <number>"
        )
    if len(r) > 1:
        numbers = ", ".join(str(p["number"]) for p in r)
        raise CollectError(f"the branch {branch!r} has more than one open pull request ({numbers}); give the number: pr-collect <number>")
    ctx.terminal.say(f"pull request #{r[0]['number']} for the branch {branch}: {r[0]['title']}")
    return int(r[0]["number"])


# ---------------------------------------------------------------------------
# gh: JSON out of gh, with pagination and a wait on a rate limit.
# ---------------------------------------------------------------------------


def _decode_concatenated(text: str):
    """gh --paginate writes one JSON document per page, back to back. Join them: lists
    are concatenated, objects merge their list-valued keys."""
    decoder = json.JSONDecoder()
    docs = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        doc, pos = decoder.raw_decode(text, pos)
        docs.append(doc)
        while pos < len(text) and text[pos].isspace():
            pos += 1
    if not docs:
        return None
    if all(isinstance(d, list) for d in docs):
        return [item for d in docs for item in d]
    if all(isinstance(d, dict) for d in docs):
        merged: dict = {}
        for d in docs:
            for k, v in d.items():
                if isinstance(v, list) and isinstance(merged.get(k), list):
                    merged[k].extend(v)
                else:
                    merged[k] = v
        return merged
    return docs[0]


RATE_LIMIT_RE = re.compile(r"rate limit|HTTP 403|HTTP 429|abuse", re.IGNORECASE)
RETRY_AFTER_RE = re.compile(r"retry[- ]after[:= ]+(\d+)", re.IGNORECASE)


def gh_run(ctx: Context, args: list[str]) -> CommandResult:
    """Run gh, waiting and trying again on a rate limit."""
    for attempt in range(RETRY_LIMIT + 1):
        r = ctx.shell.run(["gh", *args])
        if r.ok or not RATE_LIMIT_RE.search(r.stderr) or attempt == RETRY_LIMIT:
            return r
        m = RETRY_AFTER_RE.search(r.stderr)
        wait = int(m.group(1)) if m else DEFAULT_RETRY_WAIT
        ctx.terminal.warn(f"GitHub asked to wait ({wait}s) before trying again")
        ctx.shell.sleep(wait)
    return r  # pragma: no cover


def gh_json(ctx: Context, args: list[str], *, optional: bool = False):
    """gh's JSON, or None when optional and gh failed (the failure is recorded)."""
    r = gh_run(ctx, args)
    if not r.ok:
        if optional:
            ctx.missing.append(f"`gh {' '.join(args[:2])}`: {r.stderr.strip() or 'failed'}")
            return None
        raise CollectError(f"gh {' '.join(args[:2])} failed: {r.stderr.strip() or 'no output'}")
    return _decode_concatenated(r.stdout)


def gh_api(ctx: Context, path: str, *, paginate: bool = True, optional: bool = False, host: str | None = None):
    args = ["api"]
    if host:
        args += ["--hostname", host]
    if paginate:
        args.append("--paginate")
    args.append(path)
    return gh_json(ctx, args, optional=optional)


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
    branch = current_branch(ctx)
    ticket = identify_ticket(ctx, branch)
    number = find_pull_request(ctx, repo, branch)
    raise CollectError(f"pull request #{number}, ticket {ticket.label}: nothing past identity is built yet")


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
