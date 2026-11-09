# pr-collect

Collect everything a reviewer needs about one GitHub pull request into a single text
file, `pr-data.txt`, written at the root of the clone you run it in.

One command, run on the branch of the pull request, gathers:

- the pull request's record: title, description, author, state, base and head, size,
  labels, reviewers, review decision, mergeability, linked issues;
- the whole conversation in time order: comments, reviews with their verdicts, review
  threads with their replies, and the events between them (commits pushed, force pushes,
  labels, review requests, draft and ready marks, renames, merges);
- the commits, oldest first, with the files each touched;
- the checks and GitHub Actions runs on the pull request's branch, one line per passing
  job, and for a failed job its steps, annotations, and the failing steps' logs;
- the branch diff from git, **annotated**: old and new line numbers on every line and every
  review thread written in right after the line it was made on;
- every touched file whole **as it stands on main**, the branch the pull request merges
  into, at the point the pull request's branch split off: the file before the change, with
  no diff in it. Read beside the annotated diff, it gives the file as the pull request
  leaves it. A file the pull request adds or removes is already whole in the annotated diff
  and is named rather than repeated.

The file is plain Markdown in seven sections under fixed headings, so a person or a program
can read it anywhere and split it apart. Three more sections come with the flags below. The exact layout is in
[PR-DATA-FORMAT.md](PR-DATA-FORMAT.md).

## Setup

Setting up is yours to do; pr-collect installs nothing and changes nothing on your system.
Have these in place:

- **Python 3.11 or newer.**
- **git**, with the repository cloned and the branch of the pull request checked out.
- **The GitHub CLI, `gh`**, logged in to the host the repository is on (`gh auth login`,
  or `gh auth login --hostname <your GitHub Enterprise host>`). pr-collect talks to GitHub
  only through `gh`, so it uses `gh`'s login and never handles a token itself.

Then get the script onto the system:

```sh
git clone https://github.com/seinemarkn/pr-collect.git
```

How you make the `pr-collect` command reachable is up to you and your system: run it by
its path (`python3 /path/to/pr-collect/pr-collect.py`), add a shell alias, or link it
into a directory already on your PATH. pr-collect never edits your PATH or your shell
files.

## Usage

Go to the root of the clone, check out the branch of the pull request, and run:

```sh
pr-collect
```

It reads the branch, works out the ticket number (see below) and confirms it with you,
finds the open pull request for the branch, says what it is doing one line at a time, and
ends with the path of `pr-data.txt`.

```
pr-collect: example/widgets on github.com
ticket DEMO-123, read from the branch name, as the key DEMO-123
Is ticket DEMO-123 correct? [Y/n, or type the number]
pull request #42 for the branch DEMO-123-add-widget-filter: Add a filter to the widget catalog
pull request        #42: Add a filter to the widget catalog
refs                head 22abb9a877, base 5971d32b31, merge base 8c25148139
conversation        2 comments, 2 reviews, 5 review threads (3 unresolved)
commits             3
checks              1 failing (tests); 2 successful (lint, ci/coverage); 1 skipped (docs); not deployed
diff                5 files, +41 -0
files               3 on main; 2 added whole in the annotated diff only
wrote /home/you/src/widgets/pr-data.txt
Do not commit pr-data.txt: it holds other people's comments and code.
```

A second run on the same branch writes the file again in place.

### Options

```
pr-collect [number] [-t TICKET] [--yes] [--no-files] [--full-logs] [--plain-diff]
           [--uses] [--related] [--redact PATTERN] [--version]
```

- `number` — the pull request number, for a branch with more than one open pull request
  or a pull request you are not on.
- `-t TICKET` — the ticket number (or full key, such as `ABC-123`) when the branch name
  does not carry it. Nothing is asked.
- `--yes` — take the ticket number read from the branch name without asking.
- `--no-files` — leave out the section holding the touched files whole, for a shorter
  file.
- `--full-logs` — keep every failing step's log whole instead of its last 200 lines.
- `--plain-diff` — add the branch diff a second time, as git wrote it, in a section of its
  own, for `git apply`. The annotated diff already holds every line.
- `--uses` — add a section of files on the pull request's branch that define what the diff
  uses: a class, a constant, a method, or a factory named on an added line, up to 25 files,
  each saying what it defines. This is what a reader needs to judge a call the diff makes.
- `--related` — add a section of files on the pull request's branch that name an identifier
  the diff defines (a method, a class, a constant), up to 25, so a reader can see who else
  uses it.
- `--redact PATTERN` — a regular expression replaced with `[REDACTED]` everywhere but the
  header; repeat it for several. Line numbers and commit shas are left alone. The header
  counts the replacements per section.

### The ticket number

Branches are usually named after a ticket, and the ticket is what the work is filed by,
so pr-collect reads the ticket number from the branch name:

1. A ticket key anywhere in the name, written the way issue trackers write them: upper-case
   letters, a dash, digits. `ABC-123-add-widget-filter` gives ticket `ABC-123`.
2. Otherwise, the first run of digits greater than 100. `123-add-widget-filter` gives
   ticket `123`.

pr-collect says what it read and where from, then asks whether it is right. Answer `n`
to type the number instead. When the branch name carries no ticket at all, it says what is
wrong with the name and asks for the number; an empty answer stops the run. The pull
request's title and description are read the same way, and a different number there is
noted in the header, but the branch's number stands.

## What pr-collect never does

- It never writes anything but `pr-data.txt`, and only at the root of the clone.
- It never sends the file anywhere. What you do with it next is yours.
- It never commits, pushes, or changes the repository; the fetch it makes is of the branch
  the pull request merges into and of the pull request's branch, into `refs/remotes/origin/`.
- It never reads an issue tracker. The ticket number is read from the branch name and
  nothing more.
- It never handles a token. Logging in is `gh`'s job.

Do not commit `pr-data.txt`: it holds other people's comments and code.

## Tests

```sh
python3 -m unittest test_pr_collect.py
```

The tests need nothing but the standard library. Every call to `gh` and `git` goes through
one function, and every question to the user through another; the tests replace both with
fakes that answer from `fixtures/`, a made-up pull request whose git history is generated
by `fixtures/make_git_fixtures.py`. The end-to-end test compares the whole `pr-data.txt`
with `fixtures/expected/pr-data.txt`; set `PR_COLLECT_UPDATE_GOLDEN=1` to regenerate it
after a deliberate change to the layout.

## License

[MIT](LICENSE)
