# PR-DATA-FORMAT.md

What `pr-data.txt` holds and how to take it apart. A program or a reader that has only
this document can split the file into its sections, find any line of the diff by its line
number, and read every review thread at its place.

## 1. The file

- One file, `pr-data.txt`, UTF-8, Unix line endings, at the root of the clone it was
  made in. Written again in place on every run.
- Plain GitHub-flavoured Markdown. Everything that is code, a log, a diff, or a file's
  contents sits in a fenced block.
- Seven sections, always in this order, each under a top-level heading of one fixed form:

```
# pr-data: Header
# pr-data: Pull request
# pr-data: Conversation
# pr-data: Annotated diff
# pr-data: Commits
# pr-data: Checks
# pr-data: Files on main
```

  The last heading names the branch the pull request merges into: `Files on main` for a
  pull request into `main`, `Files on develop` for one into `develop`. `--plain-diff` adds
  `# pr-data: Diff` between Checks and Files on main; `--uses` adds
  `# pr-data: Files the diff uses` and `--related` adds `# pr-data: Related files`, in that
  order, at the end.
  Each section after the Header can be left out by its flag (`--no-pull-request`,
  `--no-conversation`, `--no-diff`, `--no-commits`, `--no-checks`, `--no-main-files`); the
  Header is always present. The Header's **Sections** line lists exactly what is present,
  numbered in order, and its **Notes** name each section left out and the flag that did it.

Two phrases recur: **the pull request's branch** is the branch under review, as it is now;
**main** (or the branch's real name) is the branch the pull request merges into, as it
stood where the pull request's branch split off from it, which git calls the merge base.

## 2. Splitting the file

A section heading is a line matching `^# pr-data: (.+)$`. Every line after it, up to the
next such line, is that section's body.

Fenced blocks inside a section can themselves hold lines beginning with `#` (a Markdown
file among the touched files, a shell script), so a strict splitter tracks fences: a line of three or
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
| **Branch** | the pull request's branch and the branch it merges into |
| **Head**, **Base**, **Merge base** | the three full shas the diff was taken between, in git's words: the tip of the pull request's branch, the tip of the branch it merges into, and where the two split |
| **Taken** | time in UTC, the `gh` login that ran it, the pr-collect version |
| **Checks** | one status line: counts and names by outcome, and whether the pull request's commit is deployed; `not collected (--no-checks)` when the Checks section was left out |
| **Counts** | comments, review threads (unresolved), commits, failed jobs, files changed; only what was collected, so a count whose section was left out is missing, and the line is absent when nothing was counted |
| **Sections** | the sections present, numbered, in order |
| **Not collected** | parts that could not be fetched, each with why (only when any) |
| **Notes** | what the run noticed: the masking note, each section left out and its flag (`The Checks section was left out (--no-checks).`), the reconciliation of the diff against GitHub's file list, a ticket number that differs in the title, redaction counts |
| **Requests to GitHub** | how many `gh` calls the run made |

## 4. Pull request

The record as a list (number, author, state, the two branches with their shas, size, labels,
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

## 11. Files on main

Every file the diff touches, whole, **as it stood on the branch the pull request merges
into at the merge base**: the file before the change, with no diff in it. `## <path>` per
file, sorted by path and under the file's path on that branch (the old path for a rename),
then `<n> lines.` and the file in a fenced block. The section opens with one sentence
naming the branch and the commit: `Every touched file whole as it stood on main where the
pull request's branch split off (commit <sha>).`

Two kinds of touched file are named, not inlined, each in one line before that sentence
(which then says `Every other touched file …`): the files the pull request **added**, which
did not exist on the branch (`Not on main, added by the pull request; the Annotated diff
section holds each whole: …`), and the files it **removed**, which the Annotated diff shows
whole as removed lines (`Not repeated here, removed by the pull request; …`). A binary file
says `(binary, <n> bytes, not inlined)`; a file git could not show says `(not readable; see
the header)`. With no file to inline the section is the one line `No touched files on
main.` (or `No other touched files …` after the named ones).

Read beside the Annotated diff, this section gives the file as the pull request leaves it:
the hunks say which lines change and the gutter says where.

## 12. Files the diff uses, Related files

Both hold files from the pull request's branch that the diff does **not** touch, in the
same per-file form. **Files the diff uses** (`--uses`) are files that define a class, module,
constant, method, scope, factory, or association the diff's added lines name; each file's
heading is followed by `Defines: <names>.` **Related files** (`--related`) are files that
name an identifier the diff defines. Both are capped at 25 files; the Header's **Notes** say
how many were found.

## 13. Reading notes

- Shas are full in the Header and in `Full sha:` lines, ten characters elsewhere.
- Nothing in the file is summarised or shortened except the failing logs, which say so.
- The file is deterministic for a given pull request state: two runs differ only in the
  **Taken** time and whatever changed on GitHub in between.
- Where `--redact` was used, replaced text reads `[REDACTED]` and the Header counts the
  replacements per section.
