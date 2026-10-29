"""Tests for pr-collect.py.

Run with:
    python3 -m unittest test_pr_collect.py

Everything the tool does through gh, git, and the terminal goes through two seams,
Shell and Terminal. The tests replace them with FakeShell and FakeTerminal, which
answer from fixtures, so no network and no terminal are needed.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).parent


def _load_script():
    """Load pr-collect.py as a module (hyphenated filename → importlib)."""
    spec = importlib.util.spec_from_file_location("pr_collect", HERE / "pr-collect.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pr_collect"] = module
    spec.loader.exec_module(module)
    return module


pc = _load_script()


class FakeShell:
    """Answers gh and git calls from a table keyed by the exact argument list.

    An unexpected call fails the test loudly, so every command the tool runs is one
    the test meant it to run.
    """

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[list[str]] = []
        self.slept: list[float] = []

    def on(self, argv: list[str], stdout: str = "", stderr: str = "", code: int = 0) -> "FakeShell":
        self.responses[tuple(argv)] = (stdout, stderr, code)
        return self

    def run(self, argv: list[str]) -> "pc.CommandResult":
        self.calls.append(list(argv))
        key = tuple(argv)
        if key not in self.responses:
            raise AssertionError(f"unexpected command: {' '.join(argv)}")
        stdout, stderr, code = self.responses[key]
        if callable(stdout):
            stdout, stderr, code = stdout()
        return pc.CommandResult(list(argv), stdout, stderr, code)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    @property
    def gh_calls(self) -> int:
        return sum(1 for argv in self.calls if argv and argv[0] == "gh")


class FakeTerminal:
    """Captures what the tool says and answers its questions from a queue."""

    def __init__(self, answers: list[str] | None = None, interactive: bool = True) -> None:
        self.answers = list(answers or [])
        self.interactive = interactive
        self.said: list[str] = []
        self.warned: list[str] = []
        self.asked: list[str] = []

    def say(self, line: str) -> None:
        self.said.append(line)

    def warn(self, line: str) -> None:
        self.warned.append(line)

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt!r}")
        return self.answers.pop(0)


@contextmanager
def working_directory(path: Path):
    before = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


def good_preflight(shell: FakeShell, root: Path, host: str = "github.com") -> FakeShell:
    shell.on(["gh", "--version"], "gh version 2.0.0\n")
    shell.on(["git", "rev-parse", "--show-toplevel"], f"{root}\n")
    shell.on(["git", "remote", "get-url", "origin"], f"git@{host}:example/widgets.git\n")
    shell.on(["gh", "auth", "status", "--hostname", host], "Logged in\n")
    return shell


class TestShellSeam(unittest.TestCase):
    def test_run_returns_output_and_code(self):
        sh = pc.Shell()
        result = sh.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
        self.assertEqual(result.stdout.strip(), "out")
        self.assertEqual(result.stderr.strip(), "err")
        self.assertEqual(result.code, 3)
        self.assertFalse(result.ok)
        self.assertEqual(len(sh.calls), 1)

    def test_run_missing_command(self):
        sh = pc.Shell()
        result = sh.run(["pr-collect-no-such-command-xyz"])
        self.assertEqual(result.code, 127)
        self.assertIn("command not found", result.stderr)

    def test_gh_calls_counts_only_gh(self):
        sh = pc.Shell()
        sh.calls = [["gh", "api"], ["git", "diff"], ["gh", "pr"]]
        self.assertEqual(sh.gh_calls, 2)

    def test_sleep_calls_time_sleep(self):
        sh = pc.Shell()
        calls = []
        original = pc.time.sleep
        pc.time.sleep = calls.append
        try:
            sh.sleep(0.5)
        finally:
            pc.time.sleep = original
        self.assertEqual(calls, [0.5])


class TestTerminalSeam(unittest.TestCase):
    def test_say_warn_and_ask(self):
        out, err, stdin = io.StringIO(), io.StringIO(), io.StringIO("  yes \n")
        term = pc.Terminal(out, err, stdin)
        term.say("hello")
        term.warn("trouble")
        self.assertEqual(term.ask("ok? "), "yes")
        self.assertEqual(out.getvalue(), "hello\nok? ")
        self.assertEqual(err.getvalue(), "pr-collect: trouble\n")
        self.assertFalse(term.interactive)

    def test_interactive_follows_isatty(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        self.assertTrue(pc.Terminal(io.StringIO(), io.StringIO(), Tty()).interactive)

    def test_defaults_to_the_process_streams(self):
        term = pc.Terminal()
        self.assertIs(term.out, sys.stdout)
        self.assertIs(term.err, sys.stderr)
        self.assertIs(term.stdin, sys.stdin)


class TestParseArgs(unittest.TestCase):
    def test_defaults(self):
        opts = pc.parse_args([])
        self.assertEqual(opts, pc.Options())

    def test_every_flag(self):
        opts = pc.parse_args(["42", "-t", "123", "--yes", "--no-files", "--full-logs", "--related"])
        self.assertEqual(opts.number, 42)
        self.assertEqual(opts.ticket, "123")
        self.assertTrue(opts.yes and opts.no_files and opts.full_logs and opts.related)

    def test_version_and_help_exit(self):
        for flag in ("--version", "--help"):
            with self.assertRaises(SystemExit) as cm:
                with self.redirected():
                    pc.parse_args([flag])
            self.assertEqual(cm.exception.code, 0)

    def test_help_names_every_flag(self):
        with self.assertRaises(SystemExit):
            with self.redirected() as (out, _):
                pc.parse_args(["--help"])
        text = out.getvalue()
        for flag in ("-t", "--ticket", "--yes", "--no-files", "--full-logs", "--related", "--version", "number"):
            self.assertIn(flag, text)

    @contextmanager
    def redirected(self):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            yield out, err
        finally:
            sys.stdout, sys.stderr = old


class TestParseRemote(unittest.TestCase):
    def test_shapes(self):
        cases = {
            "git@github.com:example/widgets.git": ("github.com", "example", "widgets"),
            "git@github.com-personal:example/widgets.git": ("github.com", "example", "widgets"),
            "ssh://git@github.example.net/example/widgets.git": ("github.example.net", "example", "widgets"),
            "https://github.com/example/widgets.git": ("github.com", "example", "widgets"),
            "https://github.com/example/widgets": ("github.com", "example", "widgets"),
            "https://user@github.example.net/example/widgets/": ("github.example.net", "example", "widgets"),
        }
        for url, expected in cases.items():
            self.assertEqual(pc.parse_remote(url), expected, url)

    def test_not_a_repository_url(self):
        self.assertIsNone(pc.parse_remote("/srv/git/widgets.git"))
        self.assertIsNone(pc.parse_remote("https://example.com/widgets"))


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, shell):
        return pc.Context(shell, FakeTerminal(), pc.Options())

    def assertStops(self, shell, fragment):
        with working_directory(self.root):
            with self.assertRaises(pc.CollectError) as cm:
                pc.preflight(self.ctx(shell))
        self.assertIn(fragment, str(cm.exception))

    def test_no_gh(self):
        self.assertStops(FakeShell().on(["gh", "--version"], "", "command not found: gh", 127), "gh, is not installed")

    def test_not_a_repository(self):
        sh = FakeShell().on(["gh", "--version"], "gh\n").on(["git", "rev-parse", "--show-toplevel"], "", "fatal: not a git repository", 128)
        self.assertStops(sh, "not inside a git repository")

    def test_not_at_the_root(self):
        sh = FakeShell().on(["gh", "--version"], "gh\n").on(["git", "rev-parse", "--show-toplevel"], f"{self.root.parent}\n")
        self.assertStops(sh, "at the root of the clone")

    def test_no_origin(self):
        sh = FakeShell().on(["gh", "--version"], "gh\n").on(["git", "rev-parse", "--show-toplevel"], f"{self.root}\n")
        sh.on(["git", "remote", "get-url", "origin"], "", "error: No such remote", 2)
        self.assertStops(sh, "no remote named origin")

    def test_origin_not_github(self):
        sh = FakeShell().on(["gh", "--version"], "gh\n").on(["git", "rev-parse", "--show-toplevel"], f"{self.root}\n")
        sh.on(["git", "remote", "get-url", "origin"], "/srv/git/widgets.git\n")
        self.assertStops(sh, "not a GitHub repository URL")

    def test_not_logged_in(self):
        sh = good_preflight(FakeShell(), self.root, host="github.example.net")
        sh.on(["gh", "auth", "status", "--hostname", "github.example.net"], "", "You are not logged in", 1)
        self.assertStops(sh, "not logged in to github.example.net")

    def test_passes(self):
        sh = good_preflight(FakeShell(), self.root)
        with working_directory(self.root):
            repo = pc.preflight(self.ctx(sh))
        self.assertEqual((repo.host, repo.owner, repo.name, repo.root), ("github.com", "example", "widgets", self.root))
        self.assertEqual(repo.full_name, "example/widgets")


class TestMain(unittest.TestCase):
    def test_main_reports_a_collect_error(self):
        term = FakeTerminal()
        code = pc.main([], FakeShell().on(["gh", "--version"], "", "", 127), term)
        self.assertEqual(code, 1)
        self.assertEqual(len(term.warned), 1)
        self.assertIn("gh, is not installed", term.warned[0])

    def test_main_uses_real_seams_by_default(self):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            with working_directory(Path(tempfile.gettempdir())):
                code = pc.main([], FakeShell().on(["gh", "--version"], "", "", 127))
        finally:
            sys.stdout, sys.stderr = old
        self.assertEqual(code, 1)
        self.assertIn("pr-collect:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
