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
NETWORK_RETRY_LIMIT = 1
NETWORK_RETRY_WAIT = 5


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
            done = subprocess.run(argv, capture_output=True, check=False)
        except FileNotFoundError:
            return CommandResult(list(argv), "", f"command not found: {argv[0]}", 127)
        return CommandResult(
            list(argv), done.stdout.decode("utf-8", errors="replace"),
            done.stderr.decode("utf-8", errors="replace"), done.returncode,
        )

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
    plain_diff: bool = False
    uses: bool = False
    related: bool = False
    redact: list[str] = field(default_factory=list)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Context:
    shell: Shell
    terminal: Terminal
    options: Options
    missing: list[str] = field(default_factory=list)  # parts not collected, with why
    notes: list[str] = field(default_factory=list)  # things said in the header
    clock: object = utc_now


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
    docs = _decode_documents(text)
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
NETWORK_ERROR_RE = re.compile(
    r"connection reset|connection refused|broken pipe|i/o timeout|tls handshake|unexpected eof|"
    r"no such host|network is unreachable|dial tcp|temporary failure|server closed",
    re.IGNORECASE,
)


def gh_run(ctx: Context, args: list[str]) -> CommandResult:
    """Run gh, waiting and trying again on a rate limit, and once more after a dropped
    connection."""
    rate_tries = network_tries = 0
    while True:
        r = ctx.shell.run(["gh", *args])
        if r.ok:
            return r
        if RATE_LIMIT_RE.search(r.stderr) and rate_tries < RETRY_LIMIT:
            rate_tries += 1
            m = RETRY_AFTER_RE.search(r.stderr)
            wait = int(m.group(1)) if m else DEFAULT_RETRY_WAIT
            ctx.terminal.warn(f"GitHub asked to wait ({wait}s) before trying again")
            ctx.shell.sleep(wait)
            continue
        if NETWORK_ERROR_RE.search(r.stderr) and network_tries < NETWORK_RETRY_LIMIT:
            network_tries += 1
            reason = r.stderr.strip().splitlines()[0] if r.stderr.strip() else "no output"
            ctx.terminal.warn(f"the connection to GitHub dropped ({reason}); trying again in {NETWORK_RETRY_WAIT}s")
            ctx.shell.sleep(NETWORK_RETRY_WAIT)
            continue
        return r


def gh_json(ctx: Context, args: list[str], *, optional: bool = False):
    """gh's JSON, or None when optional and gh failed (the failure is recorded)."""
    r = gh_run(ctx, args)
    if not r.ok:
        if optional:
            ctx.missing.append(f"`gh {' '.join(args[:2])}`: {r.stderr.strip() or 'failed'}")
            return None
        raise CollectError(f"gh {' '.join(args[:2])} failed: {r.stderr.strip() or 'no output'}")
    return _decode_concatenated(r.stdout)


def gh_api(ctx: Context, repo: Repo, path: str, *, paginate: bool = True, optional: bool = False):
    args = ["api", "--hostname", repo.host]
    if paginate:
        args.append("--paginate")
    args.append(path)
    return gh_json(ctx, args, optional=optional)


def _decode_documents(text: str) -> list:
    """Every JSON document in gh's output, in order (one per page with --paginate)."""
    decoder = json.JSONDecoder()
    docs = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        doc, pos = decoder.raw_decode(text, pos)
        docs.append(doc)
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return docs


# ---------------------------------------------------------------------------
# What is collected, and the file it becomes.
# ---------------------------------------------------------------------------


@dataclass
class Collected:
    repo: Repo
    branch: str
    ticket: Ticket
    number: int
    pr: dict
    taken_at: datetime
    taken_by: str = ""
    base_sha: str | None = None
    head_sha: str | None = None
    merge_base: str | None = None
    counts: dict = field(default_factory=dict)
    status_line: str = ""
    comments: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    events: list = field(default_factory=list)
    threads: list = field(default_factory=list)
    commits: list = field(default_factory=list)
    check_runs: list = field(default_factory=list)
    statuses: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    jobs: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    logs: dict = field(default_factory=dict)
    deployments: list = field(default_factory=list)
    options_full_logs: bool = False
    patch: str = ""
    files: list = field(default_factory=list)
    base_files: dict = field(default_factory=dict)  # path -> text as it stood at the merge base
    added_paths: list = field(default_factory=list)  # added by the pull request: not at the merge base; whole in the annotated diff
    removed_paths: list = field(default_factory=list)  # removed by it: whole in the annotated diff, not repeated
    related_files: dict = field(default_factory=dict)
    uses_names: list = field(default_factory=list)  # the identifiers the diff uses, looked for
    uses_files: dict = field(default_factory=dict)  # path -> text, files at the head defining one of them
    uses_defines: dict = field(default_factory=dict)  # path -> the names it defines

    @property
    def head_paths(self) -> set:
        """Every touched path that exists at the head, whether or not it is in the head section."""
        return {ch.path for ch in self.files if ch.at_head}


@dataclass
class Section:
    name: str
    body: str


HEADING_PREFIX = "# pr-data: "
SECTION_ORDER = [
    "Header", "Pull request", "Conversation", "Annotated diff", "Commits",
    "Checks", "Files at base",
]
# Every section that can appear, in the order it appears: the seven above, plus "Diff"
# (--plain-diff) between Checks and the files, then "Files the diff uses" (--uses) and
# "Related files" (--related) last.
SECTION_ORDER_ALL = [
    "Header", "Pull request", "Conversation", "Annotated diff", "Commits",
    "Checks", "Diff", "Files at base", "Files the diff uses", "Related files",
]


def fence_for(text: str) -> str:
    """A backtick fence longer than any run of backticks in the text, three at least."""
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def fenced(text: str, info: str = "") -> str:
    fence = fence_for(text)
    body = text if text.endswith("\n") else text + "\n"
    return f"{fence}{info}\n{body}{fence}\n"


def short(sha: str | None) -> str:
    return sha[:10] if sha else "?"


def user_label(user: dict | None) -> str:
    if not user:
        return "(unknown)"
    login = user.get("login") or ""
    name = user.get("name") or ""
    if name and login:
        return f"{name} (@{login})"
    return f"@{login}" if login else name or "(unknown)"


def pr_state(pr: dict) -> str:
    state = (pr.get("state") or "").lower()
    return f"{state}, draft" if pr.get("isDraft") else state


def fetch_pull_request(ctx: Context, repo: Repo, number: int) -> dict:
    pr = gh_json(ctx, ["pr", "view", str(number), "-R", f"{repo.host}/{repo.full_name}", "--json", PR_FIELDS])
    if not isinstance(pr, dict) or pr.get("number") != number:
        raise CollectError(f"pull request #{number} was not found on {repo.full_name}")
    return pr


PR_FIELDS = ",".join([
    "number", "title", "body", "author", "state", "isDraft", "url", "baseRefName",
    "headRefName", "headRefOid", "additions", "deletions", "changedFiles", "labels",
    "assignees", "reviewRequests", "reviewDecision", "mergeable", "mergeStateStatus",
    "createdAt", "updatedAt", "mergedAt", "mergedBy", "closingIssuesReferences",
    "isCrossRepository", "headRepositoryOwner", "milestone",
])


def render_pull_request(c: Collected) -> str:
    pr = c.pr
    reviewers = [f"@{r['login']}" if "login" in r else f"team {r.get('slug') or r.get('name')}" for r in pr.get("reviewRequests") or []]
    linked = [f"#{i['number']} {i.get('title', '')} ({i.get('url', '')})".strip() for i in pr.get("closingIssuesReferences") or []]
    lines = [
        f"- **Number:** #{pr['number']} [{pr.get('title', '')}]({pr.get('url', '')})",
        f"- **Author:** {user_label(pr.get('author'))}",
        f"- **State:** {pr_state(pr)}",
        f"- **Base:** {pr.get('baseRefName')} at {short(c.base_sha)} · **Head:** {pr.get('headRefName')} at {short(c.head_sha or pr.get('headRefOid'))} · **Merge base:** {short(c.merge_base)}",
        f"- **Size:** {pr.get('changedFiles', 0)} files, +{pr.get('additions', 0)} -{pr.get('deletions', 0)}",
        f"- **Labels:** {', '.join(l['name'] for l in pr.get('labels') or []) or 'none'}",
        f"- **Assignees:** {', '.join('@' + a['login'] for a in pr.get('assignees') or []) or 'none'}",
        f"- **Reviewers requested:** {', '.join(reviewers) or 'none'}",
        f"- **Review decision:** {pr.get('reviewDecision') or 'none yet'}",
        f"- **Mergeable:** {pr.get('mergeable') or 'unknown'}, merge state {pr.get('mergeStateStatus') or 'unknown'}",
        f"- **Created:** {pr.get('createdAt')} · **Updated:** {pr.get('updatedAt')} · **Merged:** {pr.get('mergedAt') or 'no'}"
        + (f" by {user_label(pr.get('mergedBy'))}" if pr.get("mergedBy") else ""),
        f"- **Linked issues:** {'; '.join(linked) or 'none'}",
        f"- **Milestone:** {(pr.get('milestone') or {}).get('title') or 'none'}",
        f"- **From a fork:** {'yes, ' + (pr.get('headRepositoryOwner') or {}).get('login', '?') if pr.get('isCrossRepository') else 'no'}",
        "",
        "## Description",
        "",
        (pr.get("body") or "(no description)").rstrip("\n"),
        "",
    ]
    return "\n".join(lines)


def render_header(ctx: Context, c: Collected, names: list[str]) -> str:
    pr = c.pr
    t = c.ticket
    ticket_line = f"- **Ticket:** {t.label}"
    if t.key:
        ticket_line += f" (number {t.number})"
    ticket_line += f", from {t.source}"
    counts = c.counts
    count_bits = [f"{counts[k]} {label}" for k, label in [
        ("comments", "comments"), ("threads", "review threads"), ("commits", "commits"),
        ("failed_jobs", "failed jobs"), ("files", "files changed"),
    ] if k in counts]
    if "unresolved" in counts and "threads" in counts:
        count_bits[1] = f"{counts['threads']} review threads ({counts['unresolved']} unresolved)"
    lines = [
        ticket_line,
        f"- **Pull request:** #{pr['number']} [{pr.get('title', '')}]({pr.get('url', '')}) on {c.repo.full_name} ({c.repo.host})",
        f"- **Branch:** {c.branch} into {pr.get('baseRefName')}",
        f"- **Head:** {c.head_sha or pr.get('headRefOid')} · **Base:** {c.base_sha or '?'} · **Merge base:** {c.merge_base or '?'}",
        f"- **Taken:** {c.taken_at.strftime('%Y-%m-%dT%H:%M:%SZ')} by {c.taken_by or '?'} with pr-collect {__version__}",
    ]
    if c.status_line:
        lines.append(f"- **Checks:** {c.status_line}")
    if count_bits:
        lines.append(f"- **Counts:** {', '.join(count_bits)}")
    lines.append("- **Sections:** " + " · ".join(f"{i}. {n}" for i, n in enumerate(names, 1)))
    if ctx.missing:
        lines.append("- **Not collected:**")
        lines += [f"  - {m}" for m in ctx.missing]
    if ctx.notes:
        lines.append("- **Notes:**")
        lines += [f"  - {n}" for n in ctx.notes]
    lines.append(f"- **Requests to GitHub:** {ctx.shell.gh_calls}")
    lines.append("")
    return "\n".join(lines)


def assemble(sections: list[Section]) -> str:
    """The file: every section under its fixed heading, in order."""
    parts = []
    for s in sections:
        parts.append(f"{HEADING_PREFIX}{s.name}\n\n{s.body.rstrip()}\n")
    return "\n".join(parts)


def write_output(root: Path, text: str) -> Path:
    path = root / OUTPUT_NAME
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# The conversation: comments, reviews, review threads, and the events between them.
# ---------------------------------------------------------------------------


@dataclass
class ThreadComment:
    id: int | None
    login: str
    body: str
    created_at: str
    url: str = ""
    original_commit: str | None = None


@dataclass
class Thread:
    id: str
    path: str
    line: int | None  # on the side given, in the diff as it is now; None when outdated
    original_line: int | None
    start_line: int | None
    side: str  # RIGHT (the new side) or LEFT (the old side)
    resolved: bool
    outdated: bool
    comments: list[ThreadComment]

    @property
    def created_at(self) -> str:
        return self.comments[0].created_at if self.comments else ""

    @property
    def where(self) -> str:
        side = "new side" if self.side == "RIGHT" else "old side"
        if self.line is None:
            return f"{self.path}, originally line {self.original_line} ({side}), outdated"
        span = f"lines {self.start_line}-{self.line}" if self.start_line and self.start_line != self.line else f"line {self.line}"
        return f"{self.path} {span} ({side})"


REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line originalLine startLine diffSide
          comments(first: 100) {
            nodes { databaseId author { login } body createdAt url originalCommit { oid } }
          }
        }
      }
    }
  }
}
""".strip()


def fetch_review_threads(ctx: Context, c: Collected) -> list[Thread]:
    r = gh_run(ctx, [
        "api", "--hostname", c.repo.host, "graphql", "--paginate",
        "-f", f"query={REVIEW_THREADS_QUERY}", "-F", f"owner={c.repo.owner}",
        "-F", f"name={c.repo.name}", "-F", f"number={c.number}",
    ])
    if not r.ok:
        ctx.missing.append(f"the review threads (gh api graphql): {r.stderr.strip() or 'failed'}")
        return []
    threads = []
    for doc in _decode_documents(r.stdout):
        page = (((doc.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {}
        for node in page.get("nodes") or []:
            comments = [
                ThreadComment(
                    cm.get("databaseId"), (cm.get("author") or {}).get("login") or "ghost",
                    cm.get("body") or "", cm.get("createdAt") or "", cm.get("url") or "",
                    (cm.get("originalCommit") or {}).get("oid"),
                )
                for cm in (node.get("comments") or {}).get("nodes") or []
            ]
            threads.append(Thread(
                node.get("id") or "", node.get("path") or "", node.get("line"), node.get("originalLine"),
                node.get("startLine"), node.get("diffSide") or "RIGHT",
                bool(node.get("isResolved")), bool(node.get("isOutdated")), comments,
            ))
    return threads


def fetch_conversation(ctx: Context, c: Collected) -> None:
    base = f"repos/{c.repo.full_name}"
    c.comments = gh_api(ctx, c.repo, f"{base}/issues/{c.number}/comments", optional=True) or []
    c.reviews = gh_api(ctx, c.repo, f"{base}/pulls/{c.number}/reviews", optional=True) or []
    c.events = gh_api(ctx, c.repo, f"{base}/issues/{c.number}/timeline", optional=True) or []
    c.threads = fetch_review_threads(ctx, c)
    c.counts["comments"] = len(c.comments) + sum(len(t.comments) for t in c.threads)
    c.counts["threads"] = len(c.threads)
    c.counts["unresolved"] = sum(1 for t in c.threads if not t.resolved)
    ctx.terminal.say(
        f"conversation        {len(c.comments)} comments, {len(c.reviews)} reviews, "
        f"{len(c.threads)} review threads ({c.counts['unresolved']} unresolved)"
    )


SUGGESTION_RE = re.compile(r"^```suggestion[ \t]*$", re.MULTILINE)


def mark_suggestions(body: str) -> str:
    """A GitHub suggestion block, kept as the code it suggests and said to be one."""
    return SUGGESTION_RE.sub("*(suggested replacement for the line(s) commented on)*\n```suggestion", body)


def quoted(body: str) -> str:
    body = mark_suggestions(body.rstrip("\n")) or "(empty)"
    return "\n".join(f"> {line}" if line else ">" for line in body.split("\n"))


def actor(event: dict, key: str = "actor") -> str:
    return "@" + ((event.get(key) or {}).get("login") or "ghost")


def describe_event(e: dict) -> str | None:
    """One line for a timeline event; None for the ones that come from another source
    or carry nothing."""
    kind = e.get("event")
    if kind == "committed":
        title = (e.get("message") or "").split("\n", 1)[0]
        return f"commit {short(e.get('sha'))} pushed: {title}"
    if kind == "head_ref_force_pushed":
        return f"{actor(e)} force-pushed the branch" + (f" to {short(e['commit_id'])}" if e.get("commit_id") else "")
    if kind == "ready_for_review":
        return f"{actor(e)} marked the pull request ready for review"
    if kind == "convert_to_draft":
        return f"{actor(e)} converted the pull request to a draft"
    if kind in ("labeled", "unlabeled"):
        return f"{actor(e)} {'added' if kind == 'labeled' else 'removed'} the label \"{(e.get('label') or {}).get('name', '')}\""
    if kind in ("review_requested", "review_request_removed"):
        who = f"team {(e.get('requested_team') or {}).get('name')}" if e.get("requested_team") else actor(e, "requested_reviewer")
        verb = "requested a review from" if kind == "review_requested" else "removed the review request for"
        return f"{actor(e)} {verb} {who}"
    if kind in ("assigned", "unassigned"):
        return f"{actor(e)} {kind} {actor(e, 'assignee')}"
    if kind == "renamed":
        r = e.get("rename") or {}
        return f"{actor(e)} renamed the pull request from \"{r.get('from', '')}\" to \"{r.get('to', '')}\""
    if kind in ("milestoned", "demilestoned"):
        return f"{actor(e)} {'set' if kind == 'milestoned' else 'removed'} the milestone \"{(e.get('milestone') or {}).get('title', '')}\""
    if kind == "merged":
        return f"{actor(e)} merged the pull request" + (f" as {short(e['commit_id'])}" if e.get("commit_id") else "")
    if kind in ("closed", "reopened"):
        return f"{actor(e)} {kind} the pull request"
    if kind == "review_dismissed":
        return f"{actor(e)} dismissed a review"
    if kind == "base_ref_changed":
        return f"{actor(e)} changed the base branch"
    if kind in ("head_ref_deleted", "head_ref_restored"):
        return f"{actor(e)} {'deleted' if kind == 'head_ref_deleted' else 'restored'} the branch"
    if kind == "cross-referenced":
        issue = ((e.get("source") or {}).get("issue") or {})
        return f"{actor(e)} referenced this from #{issue.get('number', '?')} {issue.get('title', '')}".rstrip()
    if kind in ("commented", "reviewed", "line-commented", "commit-commented", "mentioned", "subscribed", "unsubscribed"):
        return None
    return f"{actor(e)}: {kind}"


def event_time(e: dict) -> str:
    if e.get("event") == "committed":
        return (e.get("committer") or e.get("author") or {}).get("date") or ""
    return e.get("created_at") or ""


def review_verdict(state: str) -> str:
    return {
        "APPROVED": "approved", "CHANGES_REQUESTED": "requested changes", "COMMENTED": "commented",
        "DISMISSED": "review dismissed", "PENDING": "review pending",
    }.get((state or "").upper(), (state or "reviewed").lower())


def render_thread(t: Thread) -> str:
    state = "resolved" if t.resolved else "unresolved"
    lines = [f"**Review thread** on {t.where} · {state}", ""]
    for cm in t.comments:
        lines.append(f"**@{cm.login}** · {cm.created_at}" + (f" · {cm.url}" if cm.url else ""))
        lines.append(quoted(cm.body))
        lines.append("")
    return "\n".join(lines)


def render_conversation(c: Collected) -> str:
    pr = c.pr
    items: list[tuple[str, int, str]] = [(pr.get("createdAt") or "", 0,
        f"## {pr.get('createdAt')} · {user_label(pr.get('author'))} opened the pull request\n\n"
        f"The description is in the Pull request section.\n")]
    for e in c.events:
        line = describe_event(e)
        if line:
            items.append((event_time(e), 1, f"## {event_time(e)} · {line}\n"))
    for cm in c.comments:
        items.append((cm.get("created_at") or "", 2,
            f"## {cm.get('created_at')} · {actor(cm, 'user')} commented\n\n{quoted(cm.get('body') or '')}\n"))
    for rv in c.reviews:
        when = rv.get("submitted_at") or ""
        head = f"## {when} · {actor(rv, 'user')} reviewed: {review_verdict(rv.get('state'))}"
        if rv.get("commit_id"):
            head += f" (at {short(rv['commit_id'])})"
        body = f"\n\n{quoted(rv['body'])}" if rv.get("body") else ""
        items.append((when, 3, head + body + "\n"))
    for t in c.threads:
        items.append((t.created_at, 4, f"## {t.created_at} · review thread\n\n{render_thread(t)}"))
    items.sort(key=lambda i: (i[0], i[1]))
    return "\n".join(i[2] for i in items)


# ---------------------------------------------------------------------------
# git: the remote refs, the shas, and the commits.
# ---------------------------------------------------------------------------


def git_run(ctx: Context, args: list[str], *, what: str) -> str:
    r = ctx.shell.run(["git", *args])
    if not r.ok:
        raise CollectError(f"git {args[0]} failed while {what}: {r.stderr.strip() or 'no output'}")
    return r.stdout


def fetch_refs(ctx: Context, c: Collected) -> None:
    """Fetch the base branch and the pull request's head from the remote, so neither side
    depends on a local checkout; the head must be the sha GitHub reports."""
    base_branch = c.pr.get("baseRefName") or "main"
    base_ref = f"refs/remotes/origin/{base_branch}"
    head_ref = f"refs/remotes/origin/pr/{c.number}"
    git_run(ctx, ["fetch", "-q", "origin", f"+refs/heads/{base_branch}:{base_ref}", f"+refs/pull/{c.number}/head:{head_ref}"],
            what="fetching the base branch and the pull request's head")
    c.head_sha = git_run(ctx, ["rev-parse", head_ref], what="reading the head").strip()
    c.base_sha = git_run(ctx, ["rev-parse", base_ref], what="reading the base").strip()
    expected = c.pr.get("headRefOid")
    if expected and c.head_sha != expected:
        raise CollectError(
            f"the fetched head {short(c.head_sha)} is not the head GitHub reports for #{c.number}, "
            f"{short(expected)}. The pull request moved during the run, or the fetch did not reach it; run again."
        )
    c.merge_base = git_run(ctx, ["merge-base", c.base_sha, c.head_sha], what="finding the merge base").strip()
    ctx.terminal.say(f"refs                head {short(c.head_sha)}, base {short(c.base_sha)}, merge base {short(c.merge_base)}")


@dataclass
class FileStat:
    path: str
    added: int | None  # None for a binary file
    removed: int | None


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    date: str
    message: str
    files: list[FileStat]


RENAME_BRACES_RE = re.compile(r"^(.*)\{(.*) => (.*)\}(.*)$")


def numstat_path(raw: str) -> str:
    """The new path from a --numstat path, which git writes as `{old => new}` for a rename."""
    m = RENAME_BRACES_RE.match(raw)
    if m:
        return f"{m.group(1)}{m.group(3)}{m.group(4)}"
    if " => " in raw:
        return raw.split(" => ", 1)[1]
    return raw


def parse_numstat(text: str) -> list[FileStat]:
    stats = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, raw = parts
        stats.append(FileStat(numstat_path(raw), None if added == "-" else int(added), None if removed == "-" else int(removed)))
    return stats


LOG_FORMAT = "%x1e%H%x1f%an%x1f%ae%x1f%aI%x1f%B"


def parse_log(text: str) -> list[Commit]:
    commits = []
    for record in text.split("\x1e"):
        if not record.strip():
            continue
        sha, author, email, date, rest = record.split("\x1f", 4)
        # %B ends with a newline, then a blank line, then the numstat lines.
        message, _, numstat = rest.partition("\n\n\n")
        commits.append(Commit(sha, author, email, date, message.strip("\n"), parse_numstat(numstat)))
    return commits


def fetch_commits(ctx: Context, c: Collected) -> None:
    out = git_run(ctx, ["log", "--reverse", f"--format={LOG_FORMAT}", "--numstat", "-M", f"{c.merge_base}..{c.head_sha}"],
                  what="listing the commits")
    c.commits = parse_log(out)
    c.counts["commits"] = len(c.commits)
    ctx.terminal.say(f"commits             {len(c.commits)}")


def stat_label(f: FileStat) -> str:
    if f.added is None:
        return f"{f.path} (binary)"
    return f"{f.path} (+{f.added} -{f.removed})"


def render_commits(c: Collected) -> str:
    if not c.commits:
        return "No commits between the merge base and the head.\n"
    out = []
    for i, cm in enumerate(c.commits, 1):
        out.append(f"## {i}. {short(cm.sha)} · {cm.author} <{cm.email}> · {cm.date}")
        out.append("")
        out.append(f"Full sha: {cm.sha}")
        out.append("")
        out.append(cm.message)
        out.append("")
        out.append("Files: " + (", ".join(stat_label(f) for f in cm.files) or "none"))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The checks and the Actions runs, with the failing steps' logs.
# ---------------------------------------------------------------------------

LOG_TAIL = 200


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def duration(start: str | None, end: str | None) -> str:
    a, b = parse_time(start), parse_time(end)
    if not a or not b:
        return ""
    total = int((b - a).total_seconds())
    minutes, seconds = divmod(max(total, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def outcome(item: dict) -> str:
    """One word for a check run, status, run, job, or step: its conclusion, or its state
    while it is still going."""
    conclusion = item.get("conclusion")
    if conclusion:
        return str(conclusion).lower()
    state = item.get("state") or item.get("status") or "unknown"
    return str(state).lower().replace("_", " ")


FAILING = {"failure", "timed_out", "timed out", "action_required", "action required", "error", "cancelled", "startup_failure", "startup failure"}
SKIPPED = {"skipped", "neutral", "stale"}
GOING = {"queued", "in progress", "pending", "waiting", "requested", "expected"}


def mark(item: dict) -> str:
    o = outcome(item)
    if o == "success":
        return "PASS"
    if o in FAILING:
        return "FAIL"
    if o in SKIPPED:
        return "SKIP"
    if o in GOING:
        return "...."
    return o.upper()[:4].ljust(4)


def parse_failed_log(text: str) -> dict[tuple[str, str], list[str]]:
    """gh run view --log-failed writes `<job>\t<step>\t<line>`; group the lines by step."""
    steps: dict[tuple[str, str], list[str]] = {}
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        job, step, content = parts
        steps.setdefault((job, step), []).append(content)
    return steps


def trim_log(lines: list[str], keep: int | None) -> tuple[list[str], str]:
    """The last `keep` lines and a note; the whole log when it is shorter or keep is None."""
    if keep is None or len(lines) <= keep:
        return lines, f"{len(lines)} lines"
    return lines[-keep:], f"last {keep} of {len(lines)} lines"


def fetch_checks(ctx: Context, c: Collected) -> None:
    base = f"repos/{c.repo.full_name}"
    sha = c.head_sha
    runs_doc = gh_api(ctx, c.repo, f"{base}/commits/{sha}/check-runs", optional=True) or {}
    c.check_runs = runs_doc.get("check_runs") or []
    status_doc = gh_api(ctx, c.repo, f"{base}/commits/{sha}/status", paginate=False, optional=True) or {}
    c.statuses = status_doc.get("statuses") or []
    wf_doc = gh_api(ctx, c.repo, f"{base}/actions/runs?head_sha={sha}", optional=True) or {}
    c.runs = wf_doc.get("workflow_runs") or []
    c.jobs = {}
    c.annotations = {}
    c.logs = {}
    for run in c.runs:
        jobs_doc = gh_api(ctx, c.repo, f"{base}/actions/runs/{run['id']}/jobs", optional=True) or {}
        jobs = jobs_doc.get("jobs") or []
        c.jobs[run["id"]] = jobs
        failed = [j for j in jobs if outcome(j) in FAILING]
        for job in failed:
            c.annotations[job["id"]] = gh_api(ctx, c.repo, f"{base}/check-runs/{job['id']}/annotations", optional=True) or []
        if failed:
            r = gh_run(ctx, ["run", "view", str(run["id"]), "-R", f"{c.repo.host}/{c.repo.full_name}", "--log-failed"])
            if r.ok:
                c.logs[run["id"]] = parse_failed_log(r.stdout)
            else:
                ctx.missing.append(
                    f"the failing-step logs of run {run.get('name', '?')} #{run.get('run_number', '?')} "
                    f"(jobs {', '.join(j['name'] for j in failed)}): {r.stderr.strip() or 'gh run view failed'}"
                )
    deployments = gh_api(ctx, c.repo, f"{base}/deployments?sha={sha}", optional=True)
    c.deployments = deployments if isinstance(deployments, list) else []
    c.counts["failed_jobs"] = sum(1 for jobs in c.jobs.values() for j in jobs if outcome(j) in FAILING)
    c.status_line = status_line(c)
    ctx.terminal.say(f"checks              {c.status_line}")


def status_line(c: Collected) -> str:
    items = [(r.get("name") or "?", outcome(r)) for r in c.check_runs] + [(s.get("context") or "?", outcome(s)) for s in c.statuses]
    if not items:
        return "no checks reported at the head"
    groups = {"failing": [], "successful": [], "skipped": [], "in progress": [], "other": []}
    for name, o in items:
        key = ("successful" if o == "success" else "failing" if o in FAILING else "skipped" if o in SKIPPED
               else "in progress" if o in GOING else "other")
        groups[key].append(name)
    bits = [f"{len(names)} {key} ({', '.join(names)})" for key, names in groups.items() if names]
    deployed = "deployed to " + ", ".join(sorted({d.get("environment") or "?" for d in c.deployments})) if c.deployments else "not deployed"
    return "; ".join(bits) + f"; {deployed}"


def render_checks(c: Collected) -> str:
    out = [f"Status at {short(c.head_sha)}: {c.status_line}", ""]
    out.append("## Check runs")
    out.append("")
    if not c.check_runs:
        out.append("None.")
    for r in c.check_runs:
        app = (r.get("app") or {}).get("name") or "?"
        line = f"- {mark(r)} {r.get('name')} · {app} · {outcome(r)}"
        if r.get("status") != "completed" and r.get("started_at"):
            line += f" since {r['started_at']}"
        elif duration(r.get("started_at"), r.get("completed_at")):
            line += f" · {duration(r.get('started_at'), r.get('completed_at'))}"
        title = (r.get("output") or {}).get("title")
        if title and title != r.get("name"):
            line += f" · {title}"
        if r.get("html_url"):
            line += f" · {r['html_url']}"
        out.append(line)
    out += ["", "## Commit statuses", ""]
    if not c.statuses:
        out.append("None.")
    for st in c.statuses:
        line = f"- {mark(st)} {st.get('context')} · {outcome(st)}"
        if st.get("description"):
            line += f" · {st['description']}"
        if st.get("target_url"):
            line += f" · {st['target_url']}"
        out.append(line)
    out += ["", "## Actions runs", ""]
    if not c.runs:
        out.append("None.")
    keep = None if c.options_full_logs else LOG_TAIL
    for run in c.runs:
        head = f"### {run.get('name')} run #{run.get('run_number')} ({run.get('event')}) · {outcome(run)}"
        if run.get("status") != "completed" and run.get("created_at"):
            head += f" since {run['created_at']}"
        if run.get("html_url"):
            head += f" · {run['html_url']}"
        out += [head, ""]
        for job in c.jobs.get(run["id"], []):
            line = f"- {mark(job)} job {job.get('name')} · {outcome(job)}"
            if duration(job.get("started_at"), job.get("completed_at")):
                line += f" · {duration(job.get('started_at'), job.get('completed_at'))}"
            steps = job.get("steps") or []
            if outcome(job) in FAILING or outcome(job) in GOING:
                out.append(line)
                for step in steps:
                    out.append(f"  - {mark(step)} {step.get('number')}. {step.get('name')} · {outcome(step)}")
            else:
                # A job that passed or was skipped is one line; its steps say nothing a review needs.
                out.append(line + (f" · {len(steps)} steps" if steps else ""))
            if outcome(job) in FAILING:
                notes = c.annotations.get(job["id"]) or []
                if notes:
                    out.append("  - Annotations:")
                    for a in notes:
                        where = f"{a.get('path')}:{a.get('start_line')}" if a.get("path") else ""
                        title = f" {a['title']}:" if a.get("title") else ""
                        out.append(f"    - {a.get('annotation_level', '')} {where}{title} {a.get('message', '')}".rstrip())
                steps = c.logs.get(run["id"]) or {}
                for (job_name, step_name), lines in steps.items():
                    if job_name != job.get("name"):
                        continue
                    kept, note = trim_log(lines, keep)
                    out.append("")
                    out.append(f"  Log of step \"{step_name}\" ({note}):")
                    out.append("")
                    out.append(fenced("\n".join(kept), ""))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The diff, the file list, the reconciliation, and the touched files whole.
# ---------------------------------------------------------------------------


@dataclass
class FileChange:
    status: str  # added, modified, removed, renamed, copied, type changed
    path: str  # the path at the head (the old path for a removed file)
    old_path: str | None = None  # for a rename or copy
    added: int | None = None  # None for a binary file
    removed: int | None = None

    @property
    def at_head(self) -> bool:
        return self.status != "removed"

    @property
    def at_base(self) -> bool:
        return self.status not in ("added",)

    @property
    def base_path(self) -> str:
        return self.old_path or self.path


STATUS_WORDS = {"A": "added", "M": "modified", "D": "removed", "R": "renamed", "C": "copied", "T": "type changed"}


def parse_name_status(text: str) -> list[FileChange]:
    changes = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0][:1]
        word = STATUS_WORDS.get(code, code.lower())
        if code in ("R", "C") and len(parts) >= 3:
            changes.append(FileChange(word, parts[2], parts[1]))
        else:
            changes.append(FileChange(word, parts[1]))
    return changes


def fetch_diff(ctx: Context, c: Collected) -> None:
    rng = f"{c.merge_base}...{c.head_sha}"
    c.patch = git_run(ctx, ["diff", "--no-color", "--no-ext-diff", rng], what="taking the branch diff")
    c.files = parse_name_status(git_run(ctx, ["diff", "--name-status", "-M", rng], what="listing the changed files"))
    stats = {st.path: st for st in parse_numstat(git_run(ctx, ["diff", "--numstat", "-M", rng], what="counting the changed lines"))}
    for ch in c.files:
        st = stats.get(ch.path)
        if st:
            ch.added, ch.removed = st.added, st.removed
    c.counts["files"] = len(c.files)
    reconcile(ctx, c)
    ctx.terminal.say(f"diff                {len(c.files)} files, +{sum(f.added or 0 for f in c.files)} -{sum(f.removed or 0 for f in c.files)}")


def reconcile(ctx: Context, c: Collected) -> None:
    """The git diff against the pull request's own file list and size; a difference is
    said, so a stale or wrong diff never passes as a capture."""
    listed = gh_api(ctx, c.repo, f"repos/{c.repo.full_name}/pulls/{c.number}/files", optional=True)
    if not isinstance(listed, list):
        return
    git_paths = sorted(f.path for f in c.files)
    gh_paths = sorted(f.get("filename", "") for f in listed)
    git_added = sum(f.added or 0 for f in c.files)
    git_removed = sum(f.removed or 0 for f in c.files)
    gh_added = sum(int(f.get("additions") or 0) for f in listed)
    gh_removed = sum(int(f.get("deletions") or 0) for f in listed)
    problems = []
    if git_paths != gh_paths:
        only_git = sorted(set(git_paths) - set(gh_paths))
        only_gh = sorted(set(gh_paths) - set(git_paths))
        if only_git:
            problems.append(f"in the git diff but not on GitHub: {', '.join(only_git)}")
        if only_gh:
            problems.append(f"on GitHub but not in the git diff: {', '.join(only_gh)}")
    if (git_added, git_removed) != (gh_added, gh_removed):
        problems.append(f"git counts +{git_added} -{git_removed}, GitHub +{gh_added} -{gh_removed}")
    if problems:
        note = "The diff does NOT reconcile with GitHub's file list: " + "; ".join(problems) + ". Check the fetch and run again before trusting it."
        ctx.notes.append(note)
        ctx.terminal.warn(note)
    else:
        ctx.notes.append(f"The diff reconciles with GitHub's file list: {len(c.files)} files, +{git_added} -{git_removed}.")


def file_at(ctx: Context, sha: str, path: str) -> str | None:
    """A file's text at a commit; None when git cannot show it."""
    r = ctx.shell.run(["git", "show", f"{sha}:{path}"])
    if not r.ok:
        ctx.missing.append(f"{path} at {short(sha)}: {r.stderr.strip() or 'git show failed'}")
        return None
    return r.stdout


def fetch_files(ctx: Context, c: Collected) -> None:
    """Every touched file whole as it stood at the merge base. A file the pull request
    added was not there; a file it removed is already whole in the annotated diff; both
    are named, not repeated."""
    for ch in c.files:
        if ch.status == "added":
            c.added_paths.append(ch.path)
        elif ch.status == "removed":
            c.removed_paths.append(ch.base_path)
        else:
            c.base_files[ch.base_path] = file_at(ctx, c.merge_base, ch.base_path)
    line = f"files               {len(c.base_files)} at the base"
    skipped = [f"{len(c.added_paths)} added" if c.added_paths else "", f"{len(c.removed_paths)} removed" if c.removed_paths else ""]
    if any(skipped):
        line += f"; {' and '.join(x for x in skipped if x)} whole in the annotated diff only"
    ctx.terminal.say(line)


def change_label(ch: FileChange) -> str:
    where = f"{ch.old_path} -> {ch.path}" if ch.old_path else ch.path
    if ch.added is None and ch.removed is None:
        return f"{ch.status}, binary: {where}"
    return f"{ch.status} (+{ch.added or 0} -{ch.removed or 0}): {where}"


def render_file_list(c: Collected) -> str:
    if not c.files:
        return "No files changed.\n"
    return "\n".join(f"- {change_label(ch)}" for ch in c.files) + "\n"


def render_diff(c: Collected) -> str:
    intro = (f"`git diff {c.merge_base}...{c.head_sha}`, {len(c.patch.splitlines())} lines, as git wrote it; "
             "the file list is at the top of the Annotated diff section.\n\n")
    if not c.patch.strip():
        return intro + "(empty)\n"
    return intro + fenced(c.patch, "diff")


def render_files(files: dict[str, str | None], sha: str, label: str, skipped: list[str] = (), why: str = "") -> str:
    out = []
    if skipped:
        out += [f"Not repeated here, {why}; the Annotated diff section holds each whole: {', '.join(sorted(skipped))}.", ""]
    other = "other " if skipped else ""
    if not files:
        out.append(f"No {other}files {label}.")
        return "\n".join(out) + "\n"
    out += [f"Every {other}touched file whole {label}, at {sha}.", ""]
    for path in sorted(files):
        out += file_block(path, files[path])
    return "\n".join(out)


def file_block(path: str, text: str | None, extra: str | None = None) -> list[str]:
    """One file in a files section: its heading, an optional line about it, and its text."""
    out = [f"## {path}", ""]
    if extra:
        out += [extra, ""]
    if text is None:
        out.append("(not readable; see the header)")
    elif "\x00" in text:
        out.append(f"(binary, {len(text)} bytes, not inlined)")
    else:
        out.append(f"{len(text.splitlines())} lines.")
        out.append("")
        out.append(fenced(text))
    out.append("")
    return out


# ---------------------------------------------------------------------------
# The annotated diff: the patch parsed, a gutter on every line, the threads at their lines.
# ---------------------------------------------------------------------------


@dataclass
class DiffLine:
    kind: str  # context, added, removed
    old: int | None
    new: int | None
    text: str
    note: str = ""  # "no newline at end of file"


@dataclass
class Hunk:
    header: str
    lines: list[DiffLine]


@dataclass
class DiffFile:
    old_path: str
    new_path: str
    meta: list[str]
    hunks: list[Hunk]
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path if self.new_path != "/dev/null" else self.old_path


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_GIT_RE = re.compile(r'^diff --git "?a/(.*?)"? "?b/(.*?)"?$')


def parse_diff(patch: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current: DiffFile | None = None
    hunk: Hunk | None = None
    old = new = 0
    rows = patch.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    for raw in rows:
        m = DIFF_GIT_RE.match(raw)
        if m:
            current = DiffFile(m.group(1), m.group(2), [raw], [])
            files.append(current)
            hunk = None
            continue
        if current is None:
            continue
        hm = HUNK_RE.match(raw)
        if hm:
            old, new = int(hm.group(1)), int(hm.group(3))
            hunk = Hunk(raw, [])
            current.hunks.append(hunk)
            continue
        if hunk is None:
            current.meta.append(raw)
            if raw.startswith("Binary files"):
                current.binary = True
            elif raw.startswith("--- ") and raw[4:] == "/dev/null":
                current.old_path = "/dev/null"
            elif raw.startswith("+++ ") and raw[4:] == "/dev/null":
                current.new_path = "/dev/null"
            continue
        if raw.startswith("\\"):
            if hunk.lines:
                hunk.lines[-1].note = raw[2:].strip()
            continue
        if raw.startswith("+"):
            hunk.lines.append(DiffLine("added", None, new, raw[1:]))
            new += 1
        elif raw.startswith("-"):
            hunk.lines.append(DiffLine("removed", old, None, raw[1:]))
            old += 1
        elif raw.startswith(" ") or raw == "":
            # a blank context line is " "; some tools strip it to "", which means the same
            hunk.lines.append(DiffLine("context", old, new, raw[1:]))
            old += 1
            new += 1
    return files


def gutter(line: DiffLine) -> str:
    marker = {"added": "+", "removed": "-"}.get(line.kind, " ")
    o = "" if line.old is None else str(line.old)
    n = "" if line.new is None else str(line.new)
    text = f"{o:>6} {n:>6} {marker} {line.text}"
    return text + (f"   \\ {line.note}" if line.note else "")


def threads_by_file(threads: list[Thread]) -> dict[str, list[Thread]]:
    by: dict[str, list[Thread]] = {}
    for t in threads:
        by.setdefault(t.path, []).append(t)
    return by


def line_matches(t: Thread, line: DiffLine) -> bool:
    if t.line is None:
        return False
    return (t.side == "LEFT" and line.old == t.line) or (t.side != "LEFT" and line.new == t.line)


def original_line_text(ctx: Context, t: Thread, cache: dict) -> str | None:
    if not t.comments or not t.comments[0].original_commit or not t.original_line:
        return None
    sha = t.comments[0].original_commit
    key = (sha, t.path)
    if key not in cache:
        cache[key] = file_at(ctx, sha, t.path)
    text = cache[key]
    if text is None:
        return None
    lines = text.split("\n")
    return lines[t.original_line - 1] if 0 < t.original_line <= len(lines) else None


def render_annotated_file(ctx: Context, df: DiffFile, change: FileChange | None, threads: list[Thread], cache: dict) -> str:
    label = change_label(change) if change else df.path
    out = [f"## {label}", ""]
    if df.binary:
        out.append("(binary, no hunks)")
        out.append("")
    placed: set[str] = set()
    for h in df.hunks:
        fence = fence_for("\n".join(gutter(l) for l in h.lines))
        out.append(f"Hunk `{h.header}`:")
        out.append("")
        out.append(fence)
        open_fence = True
        for line in h.lines:
            if not open_fence:
                out.append(fence)
                open_fence = True
            out.append(gutter(line))
            for t in threads:
                if t.id not in placed and line_matches(t, line):
                    placed.add(t.id)
                    out.append(fence)
                    open_fence = False
                    out.append("")
                    out.append(render_thread(t).rstrip())
                    out.append("")
        if open_fence:
            out.append(fence)
        out.append("")
    rest = [t for t in threads if t.id not in placed]
    if rest:
        out.append("### Threads on lines not in this diff (outdated)")
        out.append("")
        for t in rest:
            original = original_line_text(ctx, t, cache)
            sha = t.comments[0].original_commit if t.comments else None
            if original is not None:
                out.append(f"Originally on line {t.original_line} at {short(sha)}: `{original.strip() or '(blank)'}`")
            else:
                out.append(f"Originally on line {t.original_line}; the line could not be read from {short(sha)}.")
            out.append("")
            out.append(render_thread(t).rstrip())
            out.append("")
    return "\n".join(out)


def render_annotated_diff(ctx: Context, c: Collected) -> str:
    out = ["Files changed:", "", render_file_list(c).rstrip(), "",
           "Every line carries its old-side and new-side line numbers; an added line has only a new one, a removed line only an old one. "
           "Review threads follow the line they were made on.", ""]
    files = parse_diff(c.patch)
    by_path = threads_by_file(c.threads)
    changes = {ch.path: ch for ch in c.files}
    cache: dict = {}
    seen_paths: set[str] = set()
    for df in files:
        seen_paths.update({df.path, df.old_path})
        threads = by_path.get(df.path, []) + ([] if df.old_path == df.path else by_path.get(df.old_path, []))
        out.append(render_annotated_file(ctx, df, changes.get(df.path), threads, cache))
    stray = [t for path, ts in by_path.items() if path not in seen_paths for t in ts]
    if stray:
        out.append("## Threads on files not in this diff")
        out.append("")
        for t in stray:
            out.append(render_thread(t).rstrip())
            out.append("")
    if not files:
        out.append("(no hunks: the diff is empty)")
    return "\n".join(out) + "\n"


def split_sections(text: str) -> list[tuple[str, str]]:
    """The sections of a pr-data.txt, by their headings: what a reader does to take it apart."""
    sections: list[tuple[str, str]] = []
    name, body = None, []
    for line in text.split("\n"):
        if line.startswith(HEADING_PREFIX):
            if name is not None:
                sections.append((name, "\n".join(body)))
            name, body = line[len(HEADING_PREFIX):], []
        elif name is not None:
            body.append(line)
    if name is not None:
        sections.append((name, "\n".join(body)))
    return sections


# ---------------------------------------------------------------------------
# Proposed extras: related files (--related) and a redaction list (--redact).
# ---------------------------------------------------------------------------

RELATED_CAP = 25
DEFINITION_RES = [
    re.compile(r"^\s*(?:def|class|module|function|interface|struct|enum|type)\s+([A-Za-z_][A-Za-z0-9_]{2,})"),
    re.compile(r"^\s*(?:const|let|var|val)\s+([A-Za-z_][A-Za-z0-9_]{2,})"),
    re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*="),
]


def identifiers_added(patch: str) -> list[str]:
    """Names the diff defines on its added lines: methods, classes, constants."""
    found: dict[str, None] = {}
    for df in parse_diff(patch):
        for h in df.hunks:
            for line in h.lines:
                if line.kind != "added":
                    continue
                for rx in DEFINITION_RES:
                    m = rx.match(line.text)
                    if m:
                        found.setdefault(m.group(1), None)
    return list(found)


def fetch_related(ctx: Context, c: Collected) -> None:
    names = identifiers_added(c.patch)
    if not names:
        ctx.notes.append("Related files (--related): the diff defines no identifier to look for.")
        return
    args = ["grep", "-l", "-w", "-I"]
    for n in names:
        args += ["-e", n]
    r = ctx.shell.run(["git", *args, c.head_sha])
    if r.code not in (0, 1):
        ctx.missing.append(f"related files (git grep): {r.stderr.strip() or 'failed'}")
        return
    paths = []
    for line in r.stdout.splitlines():
        path = line.split(":", 1)[1] if ":" in line else line
        if path and path not in c.head_paths and path not in paths:
            paths.append(path)
    chosen = paths[:RELATED_CAP]
    for path in chosen:
        c.related_files[path] = file_at(ctx, c.head_sha, path)
    note = f"Related files (--related): {len(names)} identifiers ({', '.join(names)}); {len(paths)} files name one of them"
    if len(paths) > RELATED_CAP:
        note += f", the first {RELATED_CAP} included"
    ctx.notes.append(note + ".")
    ctx.terminal.say(f"related             {len(chosen)} files at the head")


# ---------------------------------------------------------------------------
# Files the diff uses (--uses): the files at the head that define what the added lines
# name, the mirror of the related files.
# ---------------------------------------------------------------------------

USES_CAP = RELATED_CAP
PROSE_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc", ".html")
STRING_OR_COMMENT_RE = re.compile(r'''"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|#.*$|//.*$''')
USE_RES = [
    re.compile(r"\b([A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*)\b"),  # a class or module: CamelCase
    re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b"),  # a constant: ALL_CAPS
    re.compile(r"\b[A-Z][A-Za-z0-9]*\.([a-z_][A-Za-z0-9_]*)"),  # a method on a class: Thing.method
    re.compile(r"(?:create|build|create_list|build_list|attributes_for)\(\s*:([a-z_][a-z0-9_]*)"),  # a factory
    re.compile(r"\.([a-z][a-z0-9]*_[a-z0-9_]+)"),  # a method with an underscore in its name
    re.compile(r"(?<![.\w])([a-z][a-z0-9]*_[a-z0-9_]+)\("),  # a bare call with an underscore in its name
]
# Names that are the language's or a common framework's, never worth looking for.
USE_STOP = {
    "new", "where", "find", "find_by", "find_each", "first", "last", "all", "none", "count", "create", "build",
    "update", "save", "destroy", "delete", "exists", "pluck", "select", "order", "limit", "joins", "includes",
    "is_a", "to_not", "not_to", "and_return", "and_raise", "to_s", "to_i", "to_a", "to_h", "to_sym", "to_json",
    "max_by", "min_by", "sort_by", "group_by", "each_with_index", "each_with_object", "load_tasks",
    "frozen_string_literal", "rails_helper", "spec_helper", "__init__", "__main__", "__name__", "zone", "now",
    "today", "yesterday", "tomorrow", "enable", "disable", "clear", "create_list", "build_list", "attributes_for",
    "Time", "Date", "DateTime", "Rails", "RSpec", "Rake", "Flipper", "ActiveRecord", "ActiveSupport", "Hash",
    "Array", "String", "Integer", "Float", "Object", "Class", "Module", "Kernel", "Struct", "Set", "Proc",
    "Exception", "StandardError", "RuntimeError", "ArgumentError", "TypeError", "ValueError", "KeyError",
    "True", "False", "None", "Math", "JSON", "Promise", "Error", "Number", "Boolean", "Symbol", "Map", "List", "Dict",
}


def identifiers_used(patch: str) -> list[str]:
    """Names the diff's added lines use and do not define: classes and modules, constants,
    methods called on a class, factories, and methods or functions with an underscore in
    their name. Strings and comments are skipped."""
    defined = set(identifiers_added(patch))
    found: dict[str, None] = {}
    for df in parse_diff(patch):
        if df.path.lower().endswith(PROSE_SUFFIXES):
            continue  # prose: every capitalised word would look like a class
        for h in df.hunks:
            for line in h.lines:
                if line.kind != "added":
                    continue
                text = STRING_OR_COMMENT_RE.sub("", line.text)
                for rx in USE_RES:
                    for m in rx.finditer(text):
                        name = m.group(1)
                        if len(name) >= 3 and name not in defined and name not in USE_STOP:
                            found.setdefault(name, None)
    return list(found)


def uses_grep_args(names: list[str]) -> list[str]:
    """The git grep that finds a definition of any of the names at a commit: a class,
    module, function, method, constant, scope, factory, or association by that name."""
    alt = "(" + "|".join(re.escape(n) for n in names) + ")"
    end = "([^A-Za-z0-9_]|$)"
    sp = "[[:space:]]"
    patterns = [
        f"^{sp}*(def{sp}+(self\\.)?|(class|module|function|struct|enum|interface|type|const|let|var|val){sp}+){alt}{end}",
        f"^{sp}*(scope|factory|has_many|has_one|belongs_to|attr_accessor|attr_reader|attr_writer|define_method){sp}+:{alt}{end}",
    ]
    upper = [n for n in names if n[0].isupper()]
    if upper:
        patterns.append(f"^{sp}*(" + "|".join(re.escape(n) for n in upper) + f"){sp}*=[^=]")
    args = ["grep", "-I", "-E", "-n"]
    for p in patterns:
        args += ["-e", p]
    return args


def fetch_uses(ctx: Context, c: Collected) -> None:
    names = identifiers_used(c.patch)
    c.uses_names = names
    if not names:
        ctx.notes.append("Files the diff uses (--uses): the diff uses no identifier to look for.")
        return
    r = ctx.shell.run(["git", *uses_grep_args(names), c.head_sha])
    if r.code not in (0, 1):
        ctx.missing.append(f"files the diff uses (git grep): {r.stderr.strip() or 'failed'}")
        return
    name_res = {n: re.compile(r"(?<![A-Za-z0-9_])" + re.escape(n) + r"(?![A-Za-z0-9_])") for n in names}
    defines: dict[str, list[str]] = {}
    for line in r.stdout.splitlines():
        parts = line.split(":", 3)  # sha, path, line number, text
        if len(parts) < 4:
            continue
        path, text = parts[1], parts[3]
        if not path or path in c.head_paths:
            continue
        for n, rx in name_res.items():
            if rx.search(text) and n not in defines.setdefault(path, []):
                defines[path].append(n)
    paths = [p for p in defines if defines[p]]
    chosen = paths[:USES_CAP]
    for path in chosen:
        c.uses_files[path] = file_at(ctx, c.head_sha, path)
        c.uses_defines[path] = defines[path]
    note = f"Files the diff uses (--uses): looked for the definitions of {len(names)} identifiers; {len(paths)} files define one of them"
    if len(paths) > USES_CAP:
        note += f", the first {USES_CAP} included"
    ctx.notes.append(note + ".")
    ctx.terminal.say(f"uses                {len(chosen)} files at the head")


def render_uses(c: Collected) -> str:
    out = [f"Files at the head, at {c.head_sha}, that define what the diff uses: a class, a constant, a method, a factory named on an added line. "
           f"Looked for the definitions of {len(c.uses_names)} identifiers.", ""]
    for path in sorted(c.uses_files):
        out += file_block(path, c.uses_files[path], "Defines: " + ", ".join(c.uses_defines[path]) + ".")
    return "\n".join(out)


GUTTER_RE = re.compile(r"^(\s*\d*\s+\d*\s[ +-] )")
SHA_RE = re.compile(r"[0-9a-f]{40}")


def redact_text(text: str, patterns: list[re.Pattern]) -> tuple[str, int]:
    """Replace every match with [REDACTED], leaving gutter numbers and shas alone."""
    count = 0
    out = []
    for line in text.split("\n"):
        prefix = ""
        m = GUTTER_RE.match(line)
        if m:
            prefix, line = m.group(1), line[m.end():]
        shas = {s.group(0) for s in SHA_RE.finditer(line)}
        for rx in patterns:
            def swap(match: re.Match) -> str:
                nonlocal count
                if any(match.group(0) in s for s in shas):
                    return match.group(0)
                count += 1
                return "[REDACTED]"
            line = rx.sub(swap, line)
        out.append(prefix + line)
    return "\n".join(out), count


def apply_redactions(ctx: Context, sections: list[Section]) -> None:
    patterns = []
    for raw in ctx.options.redact:
        try:
            patterns.append(re.compile(raw))
        except re.error as err:
            raise CollectError(f"--redact {raw!r} is not a valid pattern: {err}") from None
    if not patterns:
        return
    counts = []
    for sec in sections:
        sec.body, n = redact_text(sec.body, patterns)
        if n:
            counts.append(f"{sec.name} {n}")
    ctx.notes.append("Redactions (--redact): " + (", ".join(counts) if counts else "nothing matched") + ".")


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
    parser.add_argument("--no-files", action="store_true", help="leave out the section holding the touched files whole")
    parser.add_argument("--full-logs", action="store_true", help="keep every failing step's log whole instead of its last 200 lines")
    parser.add_argument("--plain-diff", action="store_true", help="add the branch diff a second time as git wrote it, for git apply")
    parser.add_argument("--uses", action="store_true", help="add files at the head that define a class, constant, method, or factory the diff uses")
    parser.add_argument("--related", action="store_true", help="add files at the head that name an identifier the diff adds or changes")
    parser.add_argument("--redact", action="append", default=[], metavar="PATTERN",
                        help="a regular expression to replace with [REDACTED] everywhere but the header; may be repeated")
    parser.add_argument("--version", action="version", version=f"pr-collect {__version__}")
    ns = parser.parse_args(argv)
    return Options(
        number=ns.number, ticket=ns.ticket, yes=ns.yes,
        no_files=ns.no_files, full_logs=ns.full_logs, plain_diff=ns.plain_diff, uses=ns.uses, related=ns.related, redact=list(ns.redact),
    )


def collect(ctx: Context) -> Path:
    """Run every part in order and write the file. Returns the file's path."""
    repo = preflight(ctx)
    ctx.terminal.say(f"pr-collect: {repo.full_name} on {repo.host}")
    branch = current_branch(ctx)
    ticket = identify_ticket(ctx, branch)
    number = find_pull_request(ctx, repo, branch)
    pr = fetch_pull_request(ctx, repo, number)
    ticket = check_ticket_against(ctx, ticket, pr.get("title", ""), "pull request's title")
    ticket = check_ticket_against(ctx, ticket, pr.get("body") or "", "pull request's description")
    c = Collected(repo, branch, ticket, number, pr, ctx.clock())
    ctx.notes.append(
        "Secrets that GitHub masks in Actions logs arrive masked; nothing else is masked. "
        "Look through the file before carrying it anywhere else."
    )
    me = gh_json(ctx, ["api", "user"], optional=True)
    c.taken_by = f"@{me['login']}" if isinstance(me, dict) and me.get("login") else ""
    ctx.terminal.say(f"pull request        #{number}: {pr.get('title', '')}")
    c.options_full_logs = ctx.options.full_logs
    fetch_refs(ctx, c)
    fetch_conversation(ctx, c)
    fetch_commits(ctx, c)
    fetch_checks(ctx, c)
    fetch_diff(ctx, c)
    if ctx.options.no_files:
        ctx.notes.append("The touched files were left out (--no-files).")
    else:
        fetch_files(ctx, c)
    if ctx.options.uses:
        fetch_uses(ctx, c)
    if ctx.options.related:
        fetch_related(ctx, c)
    sections = [
        Section("Pull request", render_pull_request(c)),
        Section("Conversation", render_conversation(c)),
        Section("Annotated diff", render_annotated_diff(ctx, c)),
        Section("Commits", render_commits(c)),
        Section("Checks", render_checks(c)),
    ]
    if ctx.options.plain_diff:
        sections.append(Section("Diff", render_diff(c)))
    if not ctx.options.no_files:
        sections.append(Section("Files at base", render_files(
            c.base_files, c.merge_base, "at the base (the merge base)", c.removed_paths, "removed by the pull request")))
    if c.uses_files:
        sections.append(Section("Files the diff uses", render_uses(c)))
    if c.related_files:
        sections.append(Section("Related files", render_files(c.related_files, c.head_sha, "related to the change, at the head")))
    apply_redactions(ctx, sections)
    existed = (repo.root / OUTPUT_NAME).exists()
    names = ["Header"] + [s.name for s in sections]
    sections.insert(0, Section("Header", render_header(ctx, c, names)))
    path = write_output(repo.root, assemble(sections))
    ctx.terminal.say(f"wrote {path}" + (" (written again in place)" if existed else ""))
    ctx.terminal.say(f"Do not commit {OUTPUT_NAME}: it holds other people's comments and code.")
    return path


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
