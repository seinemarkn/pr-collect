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


class TestReadTicket(unittest.TestCase):
    def test_key_shape_any_letters(self):
        for branch, number, key in [
            ("GLOB-10680-fingerprint-status", 10680, "GLOB-10680"),
            ("feature/ABC-77-small-thing", 77, "ABC-77"),
            ("2026-10-29-X9-5000-dated", 5000, "X9-5000"),
            ("fix-500-error-PROJ-10800-rapback", 10800, "PROJ-10800"),
        ]:
            t = pc.read_ticket(branch, "branch name")
            self.assertEqual((t.number, t.key), (number, key), branch)
            self.assertIn("as the key", t.source)

    def test_first_run_of_digits_over_100(self):
        t = pc.read_ticket("10800-rapback-subscribed-ui-update", "branch name")
        self.assertEqual((t.number, t.key), (10800, None))
        self.assertIn("greater than 100", t.source)
        self.assertEqual(pc.read_ticket("v2-fix-7-things-then-4321", "branch name").number, 4321)
        self.assertEqual(pc.read_ticket("fix-500-error-10800-rapback", "branch name").number, 500)

    def test_nothing(self):
        self.assertIsNone(pc.read_ticket("rapback-ui-fixes", "branch name"))
        self.assertIsNone(pc.read_ticket("v2-fix-100-things", "branch name"))
        self.assertIsNone(pc.read_ticket("feature/abc-77-lower-case-is-not-a-key", "branch name"))
        self.assertIsNone(pc.read_ticket("", "branch name"))

    def test_explain_branch(self):
        self.assertIn("no digits", pc.explain_branch("rapback-ui-fixes"))
        self.assertIn("greater than 100", pc.explain_branch("v2-fix-7"))

    def test_ticket_from_answer(self):
        self.assertEqual(pc.ticket_from_answer(" 10800 ", "typed").label, "10800")
        self.assertEqual(pc.ticket_from_answer("proj-10800", "typed").label, "PROJ-10800")
        with self.assertRaises(pc.CollectError):
            pc.ticket_from_answer("ten thousand", "typed")

    def test_label(self):
        self.assertEqual(pc.Ticket(5, "AB-5").label, "AB-5")
        self.assertEqual(pc.Ticket(5).label, "5")


class TestIdentifyTicket(unittest.TestCase):
    def run_it(self, branch, answers=(), interactive=True, **opts):
        term = FakeTerminal(list(answers), interactive)
        ctx = pc.Context(FakeShell(), term, pc.Options(**opts))
        ticket = pc.identify_ticket(ctx, branch)
        return ticket, term, ctx

    def test_t_option_wins_and_asks_nothing(self):
        ticket, term, _ = self.run_it("rapback-ui-fixes", ticket="10800")
        self.assertEqual(ticket.label, "10800")
        self.assertEqual(term.asked, [])
        self.assertIn("from the -t option", term.said[0])

    def test_confirmed_yes(self):
        for answer in ("", "y", "Yes"):
            ticket, term, _ = self.run_it("10800-rapback", [answer])
            self.assertEqual(ticket.number, 10800)
            self.assertEqual(len(term.asked), 1)
            self.assertIn("Is ticket 10800 correct?", term.asked[0])

    def test_confirmed_no_then_typed(self):
        ticket, term, _ = self.run_it("fix-500-error-10800-rapback", ["n", "10800"])
        self.assertEqual(ticket.number, 10800)
        self.assertEqual(len(term.asked), 2)

    def test_typed_straight_at_the_confirmation(self):
        ticket, term, _ = self.run_it("fix-500-error-10800-rapback", ["PROJ-10800"])
        self.assertEqual(ticket.label, "PROJ-10800")

    def test_no_then_empty_stops(self):
        with self.assertRaises(pc.CollectError) as cm:
            self.run_it("10800-rapback", ["no", ""])
        self.assertIn("no ticket number given", str(cm.exception))

    def test_yes_option_skips_the_question(self):
        ticket, term, ctx = self.run_it("10800-rapback", yes=True)
        self.assertEqual(term.asked, [])
        self.assertEqual(ctx.notes, [])

    def test_not_interactive_takes_it_and_notes(self):
        ticket, term, ctx = self.run_it("10800-rapback", interactive=False)
        self.assertEqual(ticket.number, 10800)
        self.assertEqual(len(ctx.notes), 1)
        self.assertIn("without confirmation", ctx.notes[0])

    def test_no_ticket_in_branch_then_prompt(self):
        ticket, term, _ = self.run_it("rapback-ui-fixes", ["10800"])
        self.assertEqual(ticket.number, 10800)
        self.assertIn("has no digits", term.warned[0])
        self.assertIn("Ticket number", term.asked[0])

    def test_no_ticket_in_branch_empty_answer_stops(self):
        with self.assertRaises(pc.CollectError) as cm:
            self.run_it("rapback-ui-fixes", [""])
        self.assertIn("No ticket number given", str(cm.exception))

    def test_no_ticket_in_branch_not_interactive_stops(self):
        with self.assertRaises(pc.CollectError) as cm:
            self.run_it("v2-fix-7", interactive=False)
        self.assertIn("greater than 100", str(cm.exception))
        self.assertIn("-t <number>", str(cm.exception))


class TestCheckTicketAgainst(unittest.TestCase):
    def ctx(self):
        return pc.Context(FakeShell(), FakeTerminal(), pc.Options())

    def test_same_number_fills_in_the_letters(self):
        ctx = self.ctx()
        t = pc.check_ticket_against(ctx, pc.Ticket(10800, None, "the branch name"), "[PROJ-10800] Add a thing", "pull request's title")
        self.assertEqual(t.key, "PROJ-10800")
        self.assertIn("letters from the pull request's title", t.source)
        self.assertEqual(ctx.notes, [])

    def test_different_number_is_said_and_branch_stands(self):
        ctx = self.ctx()
        t = pc.check_ticket_against(ctx, pc.Ticket(10800, "PROJ-10800"), "PROJ-10801 follow-up", "pull request's title")
        self.assertEqual(t.label, "PROJ-10800")
        self.assertIn("names ticket PROJ-10801, not PROJ-10800", ctx.notes[0])
        self.assertEqual(len(ctx.terminal.warned), 1)

    def test_nothing_in_the_text_or_already_keyed(self):
        ctx = self.ctx()
        t = pc.Ticket(10800, "PROJ-10800")
        self.assertIs(pc.check_ticket_against(ctx, t, "Add a thing", "title"), t)
        self.assertIs(pc.check_ticket_against(ctx, t, "PROJ-10800 again", "title"), t)
        self.assertIs(pc.check_ticket_against(ctx, t, None, "description"), t)


class TestFindPullRequest(unittest.TestCase):
    REPO = pc.Repo("github.com", "example", "widgets", Path("/tmp"))
    LIST = ["gh", "pr", "list", "-R", "github.com/example/widgets", "--head", "DEMO-123-x", "--state", "open", "--json", "number,title,url"]

    def test_number_argument_wins(self):
        ctx = pc.Context(FakeShell(), FakeTerminal(), pc.Options(number=7))
        self.assertEqual(pc.find_pull_request(ctx, self.REPO, "DEMO-123-x"), 7)
        self.assertEqual(ctx.shell.calls, [])

    def test_one_open_pull_request(self):
        sh = FakeShell().on(self.LIST, '[{"number": 42, "title": "Add filter", "url": "u"}]')
        ctx = pc.Context(sh, FakeTerminal(), pc.Options())
        self.assertEqual(pc.find_pull_request(ctx, self.REPO, "DEMO-123-x"), 42)
        self.assertIn("pull request #42", ctx.terminal.said[0])

    def test_none_open(self):
        ctx = pc.Context(FakeShell().on(self.LIST, "[]"), FakeTerminal(), pc.Options())
        with self.assertRaises(pc.CollectError) as cm:
            pc.find_pull_request(ctx, self.REPO, "DEMO-123-x")
        self.assertIn("no open pull request", str(cm.exception))

    def test_more_than_one(self):
        ctx = pc.Context(FakeShell().on(self.LIST, '[{"number": 1, "title": "a", "url": ""}, {"number": 2, "title": "b", "url": ""}]'), FakeTerminal(), pc.Options())
        with self.assertRaises(pc.CollectError) as cm:
            pc.find_pull_request(ctx, self.REPO, "DEMO-123-x")
        self.assertIn("more than one open pull request (1, 2)", str(cm.exception))


class TestCurrentBranch(unittest.TestCase):
    def test_branch(self):
        ctx = pc.Context(FakeShell().on(["git", "rev-parse", "--abbrev-ref", "HEAD"], "DEMO-123-x\n"), FakeTerminal(), pc.Options())
        self.assertEqual(pc.current_branch(ctx), "DEMO-123-x")

    def test_detached(self):
        ctx = pc.Context(FakeShell().on(["git", "rev-parse", "--abbrev-ref", "HEAD"], "HEAD\n"), FakeTerminal(), pc.Options())
        with self.assertRaises(pc.CollectError):
            pc.current_branch(ctx)


class TestGhJson(unittest.TestCase):
    def ctx(self, shell):
        return pc.Context(shell, FakeTerminal(), pc.Options())

    def test_concatenated_lists_and_objects(self):
        self.assertEqual(pc._decode_concatenated('[1,2]\n[3]'), [1, 2, 3])
        self.assertEqual(pc._decode_concatenated('{"a":[1],"n":1}{"a":[2],"n":2}'), {"a": [1, 2], "n": 2})
        self.assertEqual(pc._decode_concatenated('{"a":1}'), {"a": 1})
        self.assertEqual(pc._decode_concatenated('[1]{"a":1}'), [1])
        self.assertIsNone(pc._decode_concatenated("  "))

    def test_api_paginates_and_decodes(self):
        sh = FakeShell().on(["gh", "api", "--paginate", "repos/x/y/things"], '[{"id":1}][{"id":2}]')
        self.assertEqual(pc.gh_api(self.ctx(sh), "repos/x/y/things"), [{"id": 1}, {"id": 2}])
        sh.on(["gh", "api", "--hostname", "h", "repos/x/y/one"], '{"id":3}')
        self.assertEqual(pc.gh_api(self.ctx(sh), "repos/x/y/one", paginate=False, host="h"), {"id": 3})

    def test_failure_raises_or_records(self):
        sh = FakeShell().on(["gh", "api", "--paginate", "repos/x/y/logs"], "", "HTTP 404: Not Found", 1)
        ctx = self.ctx(sh)
        with self.assertRaises(pc.CollectError) as cm:
            pc.gh_api(ctx, "repos/x/y/logs")
        self.assertIn("HTTP 404", str(cm.exception))
        self.assertIsNone(pc.gh_api(ctx, "repos/x/y/logs", optional=True))
        self.assertIn("HTTP 404", ctx.missing[0])
        sh.on(["gh", "api", "--paginate", "repos/x/y/silent"], "", "", 1)
        with self.assertRaises(pc.CollectError) as cm:
            pc.gh_api(ctx, "repos/x/y/silent")
        self.assertIn("no output", str(cm.exception))
        self.assertIsNone(pc.gh_api(ctx, "repos/x/y/silent", optional=True))
        self.assertIn("failed", ctx.missing[1])

    def test_rate_limit_waits_then_succeeds(self):
        answers = iter([("", "HTTP 403: API rate limit exceeded. Retry-After: 7", 1), ("[1]", "", 0)])
        sh = FakeShell().on(["gh", "api", "--paginate", "repos/x/y/z"], lambda: next(answers))
        ctx = self.ctx(sh)
        self.assertEqual(pc.gh_api(ctx, "repos/x/y/z"), [1])
        self.assertEqual(sh.slept, [7])
        self.assertIn("wait (7s)", ctx.terminal.warned[0])

    def test_rate_limit_without_retry_after_waits_the_default_and_gives_up(self):
        sh = FakeShell().on(["gh", "api", "--paginate", "repos/x/y/z"], "", "HTTP 429 secondary rate limit", 1)
        ctx = self.ctx(sh)
        with self.assertRaises(pc.CollectError):
            pc.gh_api(ctx, "repos/x/y/z")
        self.assertEqual(sh.slept, [pc.DEFAULT_RETRY_WAIT] * pc.RETRY_LIMIT)
        self.assertEqual(len(sh.calls), pc.RETRY_LIMIT + 1)


if __name__ == "__main__":
    unittest.main()
