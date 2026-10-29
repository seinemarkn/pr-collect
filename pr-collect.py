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


@dataclass
class Section:
    name: str
    body: str


HEADING_PREFIX = "# pr-data: "
SECTION_ORDER = [
    "Header", "Pull request", "Conversation", "Annotated diff", "Commits",
    "Checks", "Diff", "Files at head", "Files at base",
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
    pr = fetch_pull_request(ctx, repo, number)
    ticket = check_ticket_against(ctx, ticket, pr.get("title", ""), "pull request's title")
    ticket = check_ticket_against(ctx, ticket, pr.get("body") or "", "pull request's description")
    c = Collected(repo, branch, ticket, number, pr, ctx.clock())
    me = gh_json(ctx, ["api", "user"], optional=True)
    c.taken_by = f"@{me['login']}" if isinstance(me, dict) and me.get("login") else ""
    ctx.terminal.say(f"pull request        #{number}: {pr.get('title', '')}")
    fetch_refs(ctx, c)
    fetch_conversation(ctx, c)
    fetch_commits(ctx, c)
    sections = [
        Section("Pull request", render_pull_request(c)),
        Section("Conversation", render_conversation(c)),
        Section("Commits", render_commits(c)),
    ]
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
