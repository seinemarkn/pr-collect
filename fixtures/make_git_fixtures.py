#!/usr/bin/env python3
"""Build the git side of the test fixtures from a throwaway repository.

Deterministic: fixed authors and dates give the same shas every run. Writes into
fixtures/git/: the diff, the file lists, the commit log, every touched file at the
merge base and at the head, and shas.json naming the commits.

    python3 fixtures/make_git_fixtures.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "git"

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Octo Developer", "GIT_AUTHOR_EMAIL": "octo-dev@example.com",
    "GIT_COMMITTER_NAME": "Octo Developer", "GIT_COMMITTER_EMAIL": "octo-dev@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}

BASE_FILES = {
    "lib/widgets/__init__.py": '"""Widgets: a small catalog of coloured shapes."""\n',
    "lib/widgets/catalog.py": '''"""The widget catalog."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Widget:
    name: str
    colour: str
    size: int


DEFAULT_WIDGETS = [
    Widget("bolt", "red", 2),
    Widget("gear", "blue", 5),
    Widget("spring", "red", 3),
    Widget("washer", "green", 1),
]


class Catalog:
    """Holds widgets and answers simple questions about them."""

    def __init__(self, widgets=None):
        self.widgets = list(widgets if widgets is not None else DEFAULT_WIDGETS)

    def names(self):
        return [w.name for w in self.widgets]

    def largest(self):
        return max(self.widgets, key=lambda w: w.size)

    def total_size(self):
        return sum(w.size for w in self.widgets)
''',
    "tests/test_catalog.py": '''import unittest

from lib.widgets.catalog import Catalog, Widget


class TestCatalog(unittest.TestCase):
    def test_names(self):
        self.assertEqual(Catalog().names(), ["bolt", "gear", "spring", "washer"])

    def test_largest(self):
        self.assertEqual(Catalog().largest().name, "gear")

    def test_total_size(self):
        self.assertEqual(Catalog().total_size(), 11)

    def test_empty(self):
        self.assertEqual(Catalog([]).names(), [])


if __name__ == "__main__":
    unittest.main()
''',
    "docs/usage.md": '''# Using the catalog

Create a catalog and ask it questions:

```python
from lib.widgets.catalog import Catalog

catalog = Catalog()
catalog.names()
catalog.largest()
```

The default catalog holds four widgets.
''',
}

HEAD_FILTER = '''"""Filtering a catalog by colour or size."""

MAX_SIZE = 10


def filter_widgets(widgets, colour=None, min_size=None):
    """The widgets matching every given condition, in their original order."""
    chosen = []
    for widget in widgets:
        if colour is not None and widget.colour != colour:
            continue
        if min_size is not None and widget.size < min_size:
            continue
        chosen.append(widget)
    return chosen
'''

HEAD_CATALOG = BASE_FILES["lib/widgets/catalog.py"].replace(
    'from dataclasses import dataclass\n',
    'from dataclasses import dataclass\n\nfrom lib.widgets.filter import filter_widgets\n',
).replace(
    '''    def total_size(self):
        return sum(w.size for w in self.widgets)
''',
    '''    def total_size(self):
        return sum(w.size for w in self.widgets)

    def filtered(self, colour=None, min_size=None):
        """A new catalog holding only the matching widgets."""
        return Catalog(filter_widgets(self.widgets, colour=colour, min_size=min_size))

    def colours(self):
        return sorted({w.colour for w in self.widgets})
''',
)

HEAD_TESTS = BASE_FILES["tests/test_catalog.py"].replace(
    '''    def test_empty(self):
        self.assertEqual(Catalog([]).names(), [])
''',
    '''    def test_empty(self):
        self.assertEqual(Catalog([]).names(), [])

    def test_filtered_by_colour(self):
        self.assertEqual(Catalog().filtered(colour="red").names(), ["bolt", "spring"])

    def test_filtered_by_size(self):
        self.assertEqual(Catalog().filtered(min_size=3).names(), ["gear", "spring"])

    def test_colours(self):
        self.assertEqual(Catalog().colours(), ["blue", "green", "red"])
''',
)

HEAD_DOCS = BASE_FILES["docs/usage.md"] + '''
## Filtering

Narrow the catalog by colour or by a smallest size:

```python
catalog.filtered(colour="red").names()
```
'''

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0005fe02fea7bf4bd90000000049454e44ae426082"
)


def git(repo: Path, *args: str, **kw) -> str:
    done = subprocess.run(["git", *args], cwd=repo, env=ENV, capture_output=True, text=kw.pop("text", True), check=True, **kw)
    return done.stdout


def commit(repo: Path, message: str, date: str) -> str:
    git(repo, "add", "-A")
    env = {**ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, env=env, check=True)
    return git(repo, "rev-parse", "HEAD").strip()


def write(repo: Path, files: dict) -> None:
    for path, content in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pr-collect-fixtures-"))
    try:
        repo = tmp / "widgets"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        write(repo, BASE_FILES)
        merge_base = commit(repo, "Start the widget catalog", "2026-03-01T10:00:00+00:00")

        git(repo, "checkout", "-q", "-b", "DEMO-123-add-widget-filter")
        write(repo, {"lib/widgets/filter.py": HEAD_FILTER, "lib/widgets/catalog.py": HEAD_CATALOG})
        c1 = commit(repo, "Add filter_widgets and Catalog.filtered\n\nThe catalog can now be narrowed by colour or by a smallest size.\nThe filter lives in its own module so it can be reused.", "2026-03-02T09:00:00+00:00")
        write(repo, {"tests/test_catalog.py": HEAD_TESTS})
        c2 = commit(repo, "Test the filter and the colour list", "2026-03-02T11:30:00+00:00")
        (repo / "docs/usage.md").unlink()
        write(repo, {"docs/filtering.md": HEAD_DOCS, "docs/diagram.png": PNG})
        c3 = commit(repo, "Document filtering; rename the usage page", "2026-03-04T15:00:00+00:00")
        head = c3

        git(repo, "checkout", "-q", "main")
        write(repo, {"README.md": "# widgets\n\nA catalog of coloured shapes.\n"})
        base = commit(repo, "Add a README on main", "2026-03-03T08:00:00+00:00")

        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True)
        rng = f"{merge_base}...{head}"
        (OUT / "diff.patch").write_text(git(repo, "diff", "--no-color", "--no-ext-diff", rng))
        (OUT / "name-status.txt").write_text(git(repo, "diff", "--name-status", "-M", rng))
        (OUT / "numstat.txt").write_text(git(repo, "diff", "--numstat", "-M", rng))
        (OUT / "log.txt").write_text(git(repo, "log", "--reverse", "--format=%x1e%H%x1f%an%x1f%ae%x1f%aI%x1f%B", "--numstat", "-M", f"{merge_base}..{head}"))
        for sha, label in ((merge_base, "merge-base"), (head, "head")):
            for line in git(repo, "ls-tree", "-r", "--name-only", sha).splitlines():
                p = OUT / "show" / label / line
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(git(repo, "show", f"{sha}:{line}", text=False))
        (OUT / "shas.json").write_text(json.dumps({
            "base": base, "head": head, "merge_base": merge_base, "commits": [c1, c2, c3],
            "base_branch": "main", "head_branch": "DEMO-123-add-widget-filter",
        }, indent=2) + "\n")
        print(f"wrote {OUT}: merge base {merge_base[:7]}, head {head[:7]}, base {base[:7]}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
