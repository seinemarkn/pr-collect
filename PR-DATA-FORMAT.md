# PR-DATA-FORMAT.md

What `pr-data.txt` holds and how to take it apart. A program or a reader that has only
this document can split the file into its sections, find any line of the diff by its line
number, and read every review thread at its place.

## 1. The file

- One file, `pr-data.txt`, UTF-8, Unix line endings, at the root of the clone it was
  made in. Written again in place on every run.
- Plain GitHub-flavoured Markdown. Everything that is code, a log, a diff, or a file's
  contents sits in a fenced block.
- Eight sections, always in this order, each under a top-level heading of one fixed form:

```
# pr-data: Header
# pr-data: Pull request
# pr-data: Conversation
# pr-data: Annotated diff
# pr-data: Commits
# pr-data: Checks
# pr-data: Files at head
# pr-data: Files at base
```

  `--no-files` leaves the last two out. `--plain-diff` adds `# pr-data: Diff` between
  Checks and Files at head; `--related` adds `# pr-data: Related files` at the end. The
  Header's **Sections** line lists what is present, numbered in order.

## 2. Splitting the file

A section heading is a line matching `^# pr-data: (.+)$`. Every line after it, up to the
next such line, is that section's body.

Fenced blocks inside a section can themselves hold lines beginning with `#` (a Markdown
file at the head, a shell script), so a strict splitter tracks fences: a line of three or
more backticks opens a fence, and the same number of backticks closes it; heading lines
inside a fence are content. pr-collect always chooses a fence longer than any run of
backticks in the content it wraps, so a fence is unambiguous. A simple splitter that
ignores fences works on nearly every file, since the heading form `# pr-data: ` does not
occur in ordinary content.

## 3. Header

A list of `- **Name:** value` lines:

| Line | Holds |
|---|---|
| **Ticket** | the ticket number, the full key when its letters were seen, and where it was read from |
| **Pull request** | number, title as a link, repository, host |
| **Branch** | the head branch and the base branch |
| **Head**, **Base**, **Merge base** | the three full shas the diff was taken between |
| **Taken** | time in UTC, the `gh` login that ran it, the pr-collect version |
| **Checks** | one status line: counts and names by outcome, and whether the head is deployed |
| **Counts** | comments, review threads (unresolved), commits, failed jobs, files changed |
| **Sections** | the sections present, numbered, in order |
| **Not collected** | parts that could not be fetched, each with why (only when any) |
| **Notes** | what the run noticed: the reconciliation of the diff against GitHub's file list, a ticket number that differs in the title, redaction counts, the masking note |
| **Requests to GitHub** | how many `gh` calls the run made |

## 4. Pull request

The record as a list (number, author, state, base and head with shas, size, labels,
assignees, reviewers requested, review decision, mergeability, dates, linked issues,
milestone, fork), then `## Description` with the body verbatim.

## 5. Conversation

One `## <time> · <what>` heading per item, in time order (ISO 8601 UTC):

- `<author> opened the pull request`
- `commit <sha> pushed: <title>`, `<actor> force-pushed the branch`, labels, review
  requests, draft and ready marks, renames, assignments, milestones, merges, closes
- `<actor> commented`, then the body as a block quote
- `<actor> reviewed: <verdict> (at <sha>)`, then the body if any
- `review thread`, then the thread (§6)

Bodies are verbatim inside `> ` quotes; a GitHub suggestion block keeps its
```` ```suggestion ```` fence and is preceded by the line
`*(suggested replacement for the line(s) commented on)*`.

## 6. Review threads

A thread, wherever it appears, is:

```
**Review thread** on <path> line <n> (<new side|old side>) · <resolved|unresolved>

**@<login>** · <time> · <url>
> <body>

**@<login>** · <time> · <url>
> <reply>
```

A multi-line comment says `lines <a>-<b>`. An outdated thread (its line is no longer in
the diff) says `, originally line <n> (<side>), outdated`.

## 7. Annotated diff

First `Files changed:`, one line per file: `- <status> (+a -r): <path>`, with
`<old> -> <new>` for a rename and `binary` instead of counts for a binary file.

Then one `## <that same line>` part per file in the diff, and under it one fenced block
per hunk, introduced by `` Hunk `@@ -a,b +c,d @@`: ``. Every line of a hunk is:

```
<old:>6> <new:>6> <m> <text>
```

- `old` and `new` are right-aligned in six columns; a context line has both, an added line
  only `new`, a removed line only `old`.
- `m` is ` ` for context, `+` for added, `-` for removed.
- A line with no newline at end of file ends with `   \ No newline at end of file`.

Regex for a gutter line: `^\s*(\d*)\s+(\d*)\s([ +-]) (.*)$` (groups: old, new, marker,
text; old or new is empty when absent).

A review thread is written **right after the line it was made on**: the fence is closed,
the thread (§6) follows, and the fence reopens for the rest of the hunk. Threads whose
line is no longer in the diff come at the end of the file's part under
`### Threads on lines not in this diff (outdated)`, each introduced by
`` Originally on line <n> at <sha>: `<the line as it was>` ``. Threads on files not in the
diff at all come last under `## Threads on files not in this diff`.

A binary file's part says `(binary, no hunks)`.

## 8. Commits

`## <i>. <short sha> · <author> <email> · <date>`, then `Full sha: …`, the message
verbatim, and `Files: <path> (+a -r), …`. Oldest first.

## 9. Checks

`Status at <sha>: <status line>`, then `## Check runs`, `## Commit statuses`, and
`## Actions runs`. Each check, status, job, and step is a list line starting with a
four-letter mark: `PASS`, `FAIL`, `SKIP`, `....` (still running), or the first letters of
an unusual outcome. A run is `### <workflow> run #<n> (<event>) · <outcome> · <url>`; its
jobs are nested beneath. A job that passed or was skipped is one line, ending in its step
count; a failed or still-running job lists its steps. Under a failed job: `- Annotations:`
with one line per annotation, then for each failing step
`` Log of step "<name>" (last 200 of N lines): `` and the log in a fenced block (the whole
log with `--full-logs`).

## 10. Diff (with `--plain-diff`)

One sentence naming the `git diff <merge base>...<head>` command and its length, then the
patch in a ```` ```diff ```` block exactly as git wrote it. It applies with `git apply`
once lifted out of the fence. Left out by default: the Annotated diff holds every line.

## 11. Files at head, Files at base, Related files

`## <path>` per file, sorted by path, then `<n> lines.` and the file in a fenced block.
The head section holds every file the diff touches that still exists at the head, except
the files the pull request **added**: those are whole in the Annotated diff already, so the
section opens with one line naming them (`Not repeated here, added by the pull request; …`)
and goes on with the others. The base section likewise holds every touched file as it was
at the merge base, under its old path, except the files the pull request **removed**, named
the same way. A binary file says `(binary, <n> bytes, not inlined)`; a file git could not
show says `(not readable; see the header)`.

## 12. Reading notes

- Shas are full in the Header and in `Full sha:` lines, ten characters elsewhere.
- Nothing in the file is summarised or shortened except the failing logs, which say so.
- The file is deterministic for a given pull request state: two runs differ only in the
  **Taken** time and whatever changed on GitHub in between.
- Where `--redact` was used, replaced text reads `[REDACTED]` and the Header counts the
  replacements per section.
