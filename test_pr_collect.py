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
import re
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


FIXTURES = HERE / "fixtures"
SHAS = __import__("json").loads((FIXTURES / "git" / "shas.json").read_text())
REPO_ARG = "github.com/example/widgets"
FIXED_TIME = pc.datetime(2026, 3, 5, 10, 30, 0, tzinfo=pc.timezone.utc)


def fx(*parts: str) -> str:
    return (FIXTURES.joinpath(*parts)).read_bytes().decode("utf-8", errors="replace")


def fixture_shell(root: Path) -> FakeShell:
    """Every gh and git answer for the made-up pull request #42."""
    sh = good_preflight(FakeShell(), root)
    sh.on(["git", "rev-parse", "--abbrev-ref", "HEAD"], SHAS["head_branch"] + "\n")
    sh.on(["gh", "pr", "list", "-R", REPO_ARG, "--head", SHAS["head_branch"], "--state", "open", "--json", "number,title,url"],
          '[{"number": 42, "title": "[DEMO-123] Add a filter to the widget catalog", "url": "https://github.com/example/widgets/pull/42"}]')
    sh.on(["gh", "pr", "view", "42", "-R", REPO_ARG, "--json", pc.PR_FIELDS], fx("gh", "pr-view.json"))
    sh.on(["gh", "api", "user"], fx("gh", "user.json"))
    api = ["gh", "api", "--hostname", "github.com", "--paginate"]
    sh.on([*api, "repos/example/widgets/issues/42/comments"], fx("gh", "issue-comments.json"))
    sh.on([*api, "repos/example/widgets/pulls/42/reviews"], fx("gh", "reviews.json"))
    sh.on([*api, "repos/example/widgets/issues/42/timeline"], fx("gh", "timeline.json"))
    sh.on(graphql_argv(), fx("gh", "review-threads.json"))
    mb, head, base = SHAS["merge_base"], SHAS["head"], SHAS["base"]
    sh.on(["git", "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main", "+refs/pull/42/head:refs/remotes/origin/pr/42"], "")
    sh.on(["git", "rev-parse", "refs/remotes/origin/pr/42"], head + "\n")
    sh.on(["git", "rev-parse", "refs/remotes/origin/main"], base + "\n")
    sh.on(["git", "merge-base", base, head], mb + "\n")
    sh.on(["git", "log", "--reverse", f"--format={pc.LOG_FORMAT}", "--numstat", "-M", f"{mb}..{head}"], fx("git", "log.txt"))
    sh.on([*api, f"repos/example/widgets/commits/{head}/check-runs"], fx("gh", "check-runs.json"))
    sh.on(["gh", "api", "--hostname", "github.com", f"repos/example/widgets/commits/{head}/status"], fx("gh", "status.json"))
    sh.on([*api, f"repos/example/widgets/actions/runs?head_sha={head}"], fx("gh", "runs.json"))
    sh.on([*api, "repos/example/widgets/actions/runs/5001/jobs"], fx("gh", "jobs-5001.json"))
    sh.on([*api, "repos/example/widgets/check-runs/6002/annotations"], fx("gh", "annotations-6002.json"))
    sh.on(["gh", "run", "view", "5001", "-R", REPO_ARG, "--log-failed"], fx("gh", "log-failed-5001.txt"))
    sh.on([*api, f"repos/example/widgets/deployments?sha={head}"], fx("gh", "deployments.json"))
    rng = f"{mb}...{head}"
    sh.on(["git", "show", f"{SHAS['commits'][0]}:lib/widgets/filter.py"], fx("git", "show", "head", "lib", "widgets", "filter.py"))
    sh.on(["git", "diff", "--no-color", "--no-ext-diff", rng], fx("git", "diff.patch"))
    sh.on(["git", "diff", "--name-status", "-M", rng], fx("git", "name-status.txt"))
    sh.on(["git", "diff", "--numstat", "-M", rng], fx("git", "numstat.txt"))
    sh.on([*api, "repos/example/widgets/pulls/42/files"], fx("gh", "pulls-files.json"))
    for label, sha in (("head", head), ("merge-base", mb)):
        for path in (FIXTURES / "git" / "show" / label).rglob("*"):
            if path.is_file():
                sh.on(["git", "show", f"{sha}:{path.relative_to(FIXTURES / 'git' / 'show' / label).as_posix()}"], fx("git", "show", label, *path.relative_to(FIXTURES / "git" / "show" / label).parts))
    return sh


def graphql_argv() -> list[str]:
    return ["gh", "api", "--hostname", "github.com", "graphql", "--paginate", "-f", f"query={pc.REVIEW_THREADS_QUERY}",
            "-F", "owner=example", "-F", "name=widgets", "-F", "number=42"]


def run_tool(root: Path, shell: FakeShell | None = None, answers=(), **opts) -> tuple[int, FakeTerminal, "pc.Context"]:
    shell = shell or fixture_shell(root)
    term = FakeTerminal(list(answers))
    ctx = pc.Context(shell, term, pc.Options(**opts), clock=lambda: FIXED_TIME)
    with working_directory(root):
        try:
            pc.collect(ctx)
            code = 0
        except pc.CollectError as err:
            term.warn(str(err))
            code = 1
    return code, term, ctx


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
        opts = pc.parse_args(["42", "-t", "123", "--yes", "--no-files", "--full-logs", "--plain-diff", "--uses", "--related", "--redact", "A", "--redact", "B"])
        self.assertEqual(opts.number, 42)
        self.assertEqual(opts.ticket, "123")
        self.assertTrue(opts.yes and opts.no_files and opts.full_logs and opts.plain_diff and opts.uses and opts.related)
        self.assertEqual(opts.redact, ["A", "B"])

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
        for flag in ("-t", "--ticket", "--yes", "--no-files", "--full-logs", "--plain-diff", "--uses", "--related", "--redact", "--version", "number"):
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
            ("fix-500-error-PROJ-10800-gadget", 10800, "PROJ-10800"),
        ]:
            t = pc.read_ticket(branch, "branch name")
            self.assertEqual((t.number, t.key), (number, key), branch)
            self.assertIn("as the key", t.source)

    def test_first_run_of_digits_over_100(self):
        t = pc.read_ticket("10800-gadget-subscribed-ui-update", "branch name")
        self.assertEqual((t.number, t.key), (10800, None))
        self.assertIn("greater than 100", t.source)
        self.assertEqual(pc.read_ticket("v2-fix-7-things-then-4321", "branch name").number, 4321)
        self.assertEqual(pc.read_ticket("fix-500-error-10800-gadget", "branch name").number, 500)

    def test_nothing(self):
        self.assertIsNone(pc.read_ticket("gadget-ui-fixes", "branch name"))
        self.assertIsNone(pc.read_ticket("v2-fix-100-things", "branch name"))
        self.assertIsNone(pc.read_ticket("feature/abc-77-lower-case-is-not-a-key", "branch name"))
        self.assertIsNone(pc.read_ticket("", "branch name"))

    def test_explain_branch(self):
        self.assertIn("no digits", pc.explain_branch("gadget-ui-fixes"))
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
        ticket, term, _ = self.run_it("gadget-ui-fixes", ticket="10800")
        self.assertEqual(ticket.label, "10800")
        self.assertEqual(term.asked, [])
        self.assertIn("from the -t option", term.said[0])

    def test_confirmed_yes(self):
        for answer in ("", "y", "Yes"):
            ticket, term, _ = self.run_it("10800-gadget", [answer])
            self.assertEqual(ticket.number, 10800)
            self.assertEqual(len(term.asked), 1)
            self.assertIn("Is ticket 10800 correct?", term.asked[0])

    def test_confirmed_no_then_typed(self):
        ticket, term, _ = self.run_it("fix-500-error-10800-gadget", ["n", "10800"])
        self.assertEqual(ticket.number, 10800)
        self.assertEqual(len(term.asked), 2)

    def test_typed_straight_at_the_confirmation(self):
        ticket, term, _ = self.run_it("fix-500-error-10800-gadget", ["PROJ-10800"])
        self.assertEqual(ticket.label, "PROJ-10800")

    def test_no_then_empty_stops(self):
        with self.assertRaises(pc.CollectError) as cm:
            self.run_it("10800-gadget", ["no", ""])
        self.assertIn("no ticket number given", str(cm.exception))

    def test_yes_option_skips_the_question(self):
        ticket, term, ctx = self.run_it("10800-gadget", yes=True)
        self.assertEqual(term.asked, [])
        self.assertEqual(ctx.notes, [])

    def test_not_interactive_takes_it_and_notes(self):
        ticket, term, ctx = self.run_it("10800-gadget", interactive=False)
        self.assertEqual(ticket.number, 10800)
        self.assertEqual(len(ctx.notes), 1)
        self.assertIn("without confirmation", ctx.notes[0])

    def test_no_ticket_in_branch_then_prompt(self):
        ticket, term, _ = self.run_it("gadget-ui-fixes", ["10800"])
        self.assertEqual(ticket.number, 10800)
        self.assertIn("has no digits", term.warned[0])
        self.assertIn("Ticket number", term.asked[0])

    def test_no_ticket_in_branch_empty_answer_stops(self):
        with self.assertRaises(pc.CollectError) as cm:
            self.run_it("gadget-ui-fixes", [""])
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

    REPO = pc.Repo("h", "x", "y", Path("/tmp"))

    def test_api_paginates_and_decodes(self):
        sh = FakeShell().on(["gh", "api", "--hostname", "h", "--paginate", "repos/x/y/things"], '[{"id":1}][{"id":2}]')
        self.assertEqual(pc.gh_api(self.ctx(sh), self.REPO, "repos/x/y/things"), [{"id": 1}, {"id": 2}])
        sh.on(["gh", "api", "--hostname", "h", "repos/x/y/one"], '{"id":3}')
        self.assertEqual(pc.gh_api(self.ctx(sh), self.REPO, "repos/x/y/one", paginate=False), {"id": 3})

    def test_failure_raises_or_records(self):
        sh = FakeShell().on(["gh", "api", "--hostname", "h", "--paginate", "repos/x/y/logs"], "", "HTTP 404: Not Found", 1)
        ctx = self.ctx(sh)
        with self.assertRaises(pc.CollectError) as cm:
            pc.gh_api(ctx, self.REPO, "repos/x/y/logs")
        self.assertIn("HTTP 404", str(cm.exception))
        self.assertIsNone(pc.gh_api(ctx, self.REPO, "repos/x/y/logs", optional=True))
        self.assertIn("HTTP 404", ctx.missing[0])
        sh.on(["gh", "api", "--hostname", "h", "--paginate", "repos/x/y/silent"], "", "", 1)
        with self.assertRaises(pc.CollectError) as cm:
            pc.gh_api(ctx, self.REPO, "repos/x/y/silent")
        self.assertIn("no output", str(cm.exception))
        self.assertIsNone(pc.gh_api(ctx, self.REPO, "repos/x/y/silent", optional=True))
        self.assertIn("failed", ctx.missing[1])

    def test_rate_limit_waits_then_succeeds(self):
        answers = iter([("", "HTTP 403: API rate limit exceeded. Retry-After: 7", 1), ("[1]", "", 0)])
        sh = FakeShell().on(["gh", "api", "--hostname", "h", "--paginate", "repos/x/y/z"], lambda: next(answers))
        ctx = self.ctx(sh)
        self.assertEqual(pc.gh_api(ctx, self.REPO, "repos/x/y/z"), [1])
        self.assertEqual(sh.slept, [7])
        self.assertIn("wait (7s)", ctx.terminal.warned[0])

    def test_dropped_connection_is_tried_once_more(self):
        answers = iter([("", "Get \"https://h/api\": read tcp 10.0.0.1:1->10.0.0.2:443: read: connection reset by peer", 1), ('{"ok": 1}', "", 0)])
        sh = FakeShell().on(["gh", "api", "--hostname", "h", "--paginate", "repos/x/y/files"], lambda: next(answers))
        ctx = self.ctx(sh)
        self.assertEqual(pc.gh_api(ctx, self.REPO, "repos/x/y/files"), {"ok": 1})
        self.assertEqual(sh.slept, [pc.NETWORK_RETRY_WAIT])
        self.assertEqual(ctx.terminal.warned, ["the connection to GitHub dropped (Get \"https://h/api\": read tcp 10.0.0.1:1->10.0.0.2:443: read: connection reset by peer); trying again in 5s"])
        self.assertEqual(ctx.missing, [])

    def test_dropped_connection_twice_is_recorded(self):
        sh = FakeShell().on(["gh", "api", "--hostname", "h", "--paginate", "repos/x/y/files"], "", "dial tcp: i/o timeout", 1)
        ctx = self.ctx(sh)
        self.assertIsNone(pc.gh_api(ctx, self.REPO, "repos/x/y/files", optional=True))
        self.assertEqual(len(sh.calls), pc.NETWORK_RETRY_LIMIT + 1)
        self.assertEqual(sh.slept, [pc.NETWORK_RETRY_WAIT] * pc.NETWORK_RETRY_LIMIT)
        self.assertIn("i/o timeout", ctx.missing[0])

    def test_other_failures_are_not_tried_again(self):
        sh = FakeShell().on(["gh", "api", "--hostname", "h", "--paginate", "repos/x/y/nope"], "", "HTTP 404: Not Found", 1)
        ctx = self.ctx(sh)
        with self.assertRaises(pc.CollectError):
            pc.gh_api(ctx, self.REPO, "repos/x/y/nope")
        self.assertEqual(len(sh.calls), 1)
        self.assertEqual(sh.slept, [])

    def test_rate_limit_without_retry_after_waits_the_default_and_gives_up(self):
        sh = FakeShell().on(["gh", "api", "--hostname", "h", "--paginate", "repos/x/y/z"], "", "HTTP 429 secondary rate limit", 1)
        ctx = self.ctx(sh)
        with self.assertRaises(pc.CollectError):
            pc.gh_api(ctx, self.REPO, "repos/x/y/z")
        self.assertEqual(sh.slept, [pc.DEFAULT_RETRY_WAIT] * pc.RETRY_LIMIT)
        self.assertEqual(len(sh.calls), pc.RETRY_LIMIT + 1)


class TestRendering(unittest.TestCase):
    def test_fence_for_and_fenced(self):
        self.assertEqual(pc.fence_for("plain"), "```")
        self.assertEqual(pc.fence_for("a ``` b"), "````")
        self.assertEqual(pc.fence_for("a ````` b"), "``````")
        self.assertEqual(pc.fenced("x", "py"), "```py\nx\n```\n")
        self.assertEqual(pc.fenced("x\n"), "```\nx\n```\n")

    def test_labels(self):
        self.assertEqual(pc.short(None), "?")
        self.assertEqual(pc.short("0123456789abcdef"), "0123456789")
        self.assertEqual(pc.user_label(None), "(unknown)")
        self.assertEqual(pc.user_label({"login": "x"}), "@x")
        self.assertEqual(pc.user_label({"name": "X Y"}), "X Y")
        self.assertEqual(pc.user_label({"login": "x", "name": "X Y"}), "X Y (@x)")
        self.assertEqual(pc.user_label({}), "(unknown)")
        self.assertEqual(pc.pr_state({"state": "OPEN", "isDraft": True}), "open, draft")
        self.assertEqual(pc.pr_state({"state": "MERGED"}), "merged")

    def test_assemble_headings(self):
        text = pc.assemble([pc.Section("Header", "a\n"), pc.Section("Diff", "b")])
        self.assertEqual(text, "# pr-data: Header\n\na\n\n# pr-data: Diff\n\nb\n")


class TestPullRequestSection(unittest.TestCase):
    def collected(self, **changes):
        pr = {**__import__("json").loads(fx("gh", "pr-view.json")), **changes}
        repo = pc.Repo("github.com", "example", "widgets", Path("/tmp"))
        return pc.Collected(repo, "DEMO-123-add-widget-filter", pc.Ticket(123, "DEMO-123", "the branch name"), 42, pr, FIXED_TIME)

    def test_every_field_of_the_record(self):
        text = pc.render_pull_request(self.collected())
        for piece in [
            "#42 [[DEMO-123] Add a filter to the widget catalog](https://github.com/example/widgets/pull/42)",
            "Octo Developer (@octo-dev)", "**State:** open", "main at ?", f"at {SHAS['head'][:10]}",
            "5 files, +55 -0", "enhancement, needs review", "@octo-dev", "@reviewer-two, team widgets-team",
            "CHANGES_REQUESTED", "MERGEABLE, merge state BLOCKED", "2026-03-02T09:15:00Z", "**Merged:** no",
            "#40 Catalog cannot be filtered", "**Milestone:** 1.2", "**From a fork:** no",
            "## Description", "- `python3 -m unittest`",
        ]:
            self.assertIn(piece, text, piece)

    def test_draft_merged_closed_and_empty(self):
        self.assertIn("**State:** open, draft", pc.render_pull_request(self.collected(isDraft=True)))
        merged = pc.render_pull_request(self.collected(state="MERGED", mergedAt="2026-03-06T10:00:00Z", mergedBy={"login": "boss"}))
        self.assertIn("**State:** merged", merged)
        self.assertIn("**Merged:** 2026-03-06T10:00:00Z by @boss", merged)
        bare = pc.render_pull_request(self.collected(
            state="CLOSED", body="", labels=[], assignees=[], reviewRequests=[], closingIssuesReferences=[],
            milestone=None, reviewDecision=None, mergeable=None, mergeStateStatus=None, isCrossRepository=True,
            headRepositoryOwner={"login": "forker"}))
        for piece in ["**State:** closed", "(no description)", "**Labels:** none", "**Assignees:** none",
                      "**Reviewers requested:** none", "**Linked issues:** none", "**Milestone:** none",
                      "none yet", "unknown, merge state unknown", "**From a fork:** yes, forker"]:
            self.assertIn(piece, bare, piece)

    def test_fetch_checks_the_number(self):
        sh = FakeShell().on(["gh", "pr", "view", "7", "-R", REPO_ARG, "--json", pc.PR_FIELDS], '{"number": 8}')
        ctx = pc.Context(sh, FakeTerminal(), pc.Options())
        with self.assertRaises(pc.CollectError) as cm:
            pc.fetch_pull_request(ctx, pc.Repo("github.com", "example", "widgets", Path("/tmp")), 7)
        self.assertIn("#7 was not found", str(cm.exception))


class TestHeaderSection(unittest.TestCase):
    def test_header_lines(self):
        root = Path("/tmp")
        repo = pc.Repo("github.com", "example", "widgets", root)
        pr = __import__("json").loads(fx("gh", "pr-view.json"))
        c = pc.Collected(repo, "DEMO-123-add-widget-filter", pc.Ticket(123, "DEMO-123", "the branch name, as the key DEMO-123"), 42, pr, FIXED_TIME, "@me")
        ctx = pc.Context(FakeShell(), FakeTerminal(), pc.Options())
        ctx.shell.calls = [["gh", "api", "x"]]
        text = pc.render_header(ctx, c, ["Header", "Pull request"])
        self.assertIn("**Ticket:** DEMO-123 (number 123), from the branch name, as the key DEMO-123", text)
        self.assertIn("**Branch:** DEMO-123-add-widget-filter into main", text)
        self.assertIn(f"**Head:** {SHAS['head']} · **Base:** ? · **Merge base:** ?", text)
        self.assertIn("**Taken:** 2026-03-05T10:30:00Z by @me with pr-collect", text)
        self.assertIn("**Sections:** 1. Header · 2. Pull request", text)
        self.assertIn("**Requests to GitHub:** 1", text)
        self.assertNotIn("Counts", text)
        self.assertNotIn("Not collected", text)
        c.counts = {"comments": 2, "threads": 5, "unresolved": 3, "commits": 3, "failed_jobs": 1, "files": 5}
        c.status_line = "all green"
        c.ticket = pc.Ticket(123, None, "the -t option")
        ctx.missing.append("the logs: HTTP 403")
        ctx.notes.append("a note")
        text = pc.render_header(ctx, c, ["Header"])
        self.assertIn("**Ticket:** 123, from the -t option", text)
        self.assertIn("**Checks:** all green", text)
        self.assertIn("2 comments, 5 review threads (3 unresolved), 3 commits, 1 failed jobs, 5 files changed", text)
        self.assertIn("- **Not collected:**\n  - the logs: HTTP 403", text)
        self.assertIn("- **Notes:**\n  - a note", text)


class TestEndToEnd(unittest.TestCase):
    """The whole run against the made-up pull request, byte for byte against the golden file."""

    GOLDEN = FIXTURES / "expected" / "pr-data.txt"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_golden_file(self):
        code, term, ctx = run_tool(self.root, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        if os.environ.get("PR_COLLECT_UPDATE_GOLDEN"):
            self.GOLDEN.write_text(text)
        self.assertEqual(text, self.GOLDEN.read_text())
        self.assertEqual(term.said[-1], "Do not commit pr-data.txt: it holds other people's comments and code.")
        self.assertIn(str(self.root / "pr-data.txt"), term.said[-2])

    def test_second_run_writes_in_place_and_says_so(self):
        run_tool(self.root, yes=True)
        code, term, _ = run_tool(self.root, yes=True)
        self.assertEqual(code, 0)
        self.assertIn("written again in place", term.said[-2])

    def test_title_letters_fill_in_the_key_and_the_ticket_is_confirmed(self):
        sh = fixture_shell(self.root)
        sh.on(["git", "rev-parse", "--abbrev-ref", "HEAD"], "123-add-widget-filter\n")
        sh.on(["gh", "pr", "list", "-R", REPO_ARG, "--head", "123-add-widget-filter", "--state", "open", "--json", "number,title,url"],
              '[{"number": 42, "title": "t", "url": "u"}]')
        code, term, ctx = run_tool(self.root, sh, answers=["y"])
        self.assertEqual(code, 0, term.warned)
        self.assertIn("Is ticket 123 correct?", term.asked[0])
        self.assertIn("**Ticket:** DEMO-123 (number 123), from the branch name, as the first number greater than 100; the letters from the pull request's title", (self.root / "pr-data.txt").read_text())

    def test_taken_by_is_blank_when_user_cannot_be_read(self):
        sh = fixture_shell(self.root).on(["gh", "api", "user"], "", "HTTP 401", 1)
        code, term, ctx = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 0)
        self.assertIn("by ? with pr-collect", (self.root / "pr-data.txt").read_text())
        self.assertIn("HTTP 401", ctx.missing[0])

    def test_main_end_to_end(self):
        sh = fixture_shell(self.root)
        term = FakeTerminal()
        with working_directory(self.root):
            code = pc.main(["--yes"], sh, term)
        self.assertEqual(code, 0, term.warned)


class TestConversation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def collected(self, shell=None):
        code, term, ctx = run_tool(self.root, shell, yes=True)
        self.assertEqual(code, 0, term.warned)
        return term, ctx, (self.root / "pr-data.txt").read_text()

    def test_threads_are_read_from_graphql(self):
        sh = fixture_shell(self.root)
        repo = pc.Repo("github.com", "example", "widgets", self.root)
        c = pc.Collected(repo, "b", pc.Ticket(1), 42, {}, FIXED_TIME)
        threads = pc.fetch_review_threads(pc.Context(sh, FakeTerminal(), pc.Options()), c)
        self.assertEqual([t.id for t in threads], ["T1", "T2", "T3", "T4", "T5"])
        t1, t2, t4, t5 = threads[0], threads[1], threads[3], threads[4]
        self.assertEqual((t1.path, t1.line, t1.side, t1.resolved, t1.outdated), ("lib/widgets/filter.py", 3, "RIGHT", False, False))
        self.assertEqual(t1.where, "lib/widgets/filter.py line 3 (new side)")
        self.assertEqual(t4.where, "lib/widgets/catalog.py line 3 (old side)")
        self.assertEqual(t5.where, "lib/widgets/filter.py, originally line 7 (new side), outdated")
        self.assertEqual(len(t2.comments), 2)
        self.assertEqual(t2.comments[0].login, "reviewer-two")
        self.assertEqual(t2.comments[0].original_commit, SHAS["commits"][1])
        self.assertTrue(t2.resolved)
        self.assertEqual(t1.created_at, "2026-03-03T09:01:00Z")

    def test_threads_paginate_and_survive_gaps(self):
        sh = fixture_shell(self.root).on(graphql_argv(), fx("gh", "review-threads-two-pages.json"))
        repo = pc.Repo("github.com", "example", "widgets", self.root)
        c = pc.Collected(repo, "b", pc.Ticket(1), 42, {}, FIXED_TIME)
        self.assertEqual(len(pc.fetch_review_threads(pc.Context(sh, FakeTerminal(), pc.Options()), c)), 5)
        sh.on(graphql_argv(), '{"data": null}{"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"comments": {"nodes": [{"author": null}]}}]}}}}}')
        threads = pc.fetch_review_threads(pc.Context(sh, FakeTerminal(), pc.Options()), c)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].comments[0].login, "ghost")
        self.assertEqual(threads[0].side, "RIGHT")
        self.assertEqual(threads[0].created_at, "")
        self.assertEqual(pc.Thread("x", "p", 5, 5, 3, "RIGHT", False, False, []).where, "p lines 3-5 (new side)")
        self.assertEqual(pc.Thread("x", "p", 5, 5, 5, "RIGHT", False, False, []).where, "p line 5 (new side)")

    def test_threads_failure_is_recorded_not_fatal(self):
        sh = fixture_shell(self.root).on(graphql_argv(), "", "GraphQL: something", 1)
        term, ctx, text = self.collected(sh)
        self.assertIn("the review threads (gh api graphql): GraphQL: something", ctx.missing[0])
        self.assertTrue(any("0 review threads (0 unresolved)" in line for line in term.said), term.said)

    def test_conversation_is_in_time_order_and_complete(self):
        term, ctx, text = self.collected()
        conv = text.split("# pr-data: Conversation\n", 1)[1].split("\n# pr-data: ", 1)[0]
        headings = [l for l in conv.splitlines() if l.startswith("## ")]
        times = [h[3:23] for h in headings]
        self.assertEqual(times, sorted(times))
        self.assertEqual(headings[1], "## 2026-03-02T09:15:00Z · Octo Developer (@octo-dev) opened the pull request")
        self.assertTrue(headings[0].startswith("## 2026-03-02T09:00:00Z · commit"), headings[0])
        for piece in [
            f"commit {SHAS['commits'][0][:10]} pushed: Add filter_widgets and Catalog.filtered",
            "@octo-dev added the label \"enhancement\"",
            "@octo-dev requested a review from @reviewer-two",
            "@octo-dev requested a review from team widgets-team",
            "@reviewer-one commented\n\n> Thanks for picking this up.",
            "> ```python\n> Catalog().colours()  # ['blue', 'green', 'red']\n> ```",
            "@octo-dev converted the pull request to a draft",
            "@octo-dev marked the pull request ready for review",
            f"@reviewer-two reviewed: requested changes (at {SHAS['commits'][1][:10]})\n\n> Two things",
            "@reviewer-one reviewed: commented (at",
            "@octo-dev force-pushed the branch\n",
            "@reviewer-one removed the label \"wip\"",
            "renamed the pull request from \"Add a filter\" to \"[DEMO-123] Add a filter to the widget catalog\"",
            "@octo-dev assigned @octo-dev",
            "@octo-dev set the milestone \"1.2\"",
            "@reviewer-one referenced this from #40 Catalog cannot be filtered",
            "@octo-dev removed the review request for @reviewer-one",
            "@octo-dev changed the base branch",
            "@octo-dev: some_new_event",
            "**Review thread** on lib/widgets/filter.py line 3 (new side) · unresolved",
            "**Review thread** on lib/widgets/catalog.py line 38 (new side) · resolved",
            "**Review thread** on lib/widgets/catalog.py line 3 (old side) · unresolved",
            "**Review thread** on lib/widgets/filter.py, originally line 7 (new side), outdated · resolved",
            "*(suggested replacement for the line(s) commented on)*\n> ```suggestion\n>     def filtered",
            "**@octo-dev** · 2026-03-03T10:20:00Z · https://github.com/example/widgets/pull/42#discussion_r8004\n> Taken, thanks.",
        ]:
            self.assertIn(piece, conv, piece)
        for skipped in ["(skipped", "mentioned", "subscribed"]:
            self.assertNotIn(skipped, conv)
        self.assertIn("**Counts:** 9 comments, 5 review threads (3 unresolved)", text)

    def test_event_lines_for_the_rarer_events(self):
        a = {"login": "a"}
        cases = {
            ("merged",): "@a merged the pull request",
            ("closed",): "@a closed the pull request",
            ("reopened",): "@a reopened the pull request",
            ("review_dismissed",): "@a dismissed a review",
            ("head_ref_deleted",): "@a deleted the branch",
            ("head_ref_restored",): "@a restored the branch",
            ("unassigned",): "@a unassigned @ghost",
            ("demilestoned",): "@a removed the milestone \"\"",
            ("unsubscribed",): None,
        }
        for (kind,), expected in cases.items():
            self.assertEqual(pc.describe_event({"event": kind, "actor": a}), expected, kind)
        self.assertEqual(pc.describe_event({"event": "merged", "actor": a, "commit_id": "abcdef0123456"}), "@a merged the pull request as abcdef0123")
        self.assertEqual(pc.describe_event({"event": "head_ref_force_pushed", "actor": a, "commit_id": "abcdef0123456"}), "@a force-pushed the branch to abcdef0123")
        self.assertEqual(pc.describe_event({"event": "cross-referenced", "actor": a, "source": {}}), "@a referenced this from #?")
        self.assertEqual(pc.event_time({"event": "committed", "author": {"date": "A"}}), "A")
        self.assertEqual(pc.event_time({"event": "committed"}), "")
        self.assertEqual(pc.event_time({"event": "labeled"}), "")
        self.assertEqual(pc.review_verdict("approved"), "approved")
        self.assertEqual(pc.review_verdict("DISMISSED"), "review dismissed")
        self.assertEqual(pc.review_verdict("PENDING"), "review pending")
        self.assertEqual(pc.review_verdict("odd"), "odd")
        self.assertEqual(pc.review_verdict(None), "reviewed")
        self.assertEqual(pc.quoted(""), "> (empty)")
        self.assertEqual(pc.quoted("a\n\nb\n"), "> a\n>\n> b")


class TestRefsAndCommits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetch_failure_stops(self):
        sh = fixture_shell(self.root).on(["git", "fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main", "+refs/pull/42/head:refs/remotes/origin/pr/42"], "", "fatal: could not read from remote", 128)
        code, term, _ = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 1)
        self.assertIn("git fetch failed while fetching the base branch", term.warned[0])
        self.assertIn("could not read from remote", term.warned[0])

    def test_head_that_moved_stops(self):
        sh = fixture_shell(self.root).on(["git", "rev-parse", "refs/remotes/origin/pr/42"], "0123456789abcdef0123456789abcdef01234567\n")
        code, term, _ = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 1)
        self.assertIn("is not the head GitHub reports", term.warned[0])

    def test_shas_land_in_the_header(self):
        code, term, ctx = run_tool(self.root, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertIn(f"**Head:** {SHAS['head']} · **Base:** {SHAS['base']} · **Merge base:** {SHAS['merge_base']}", text)
        self.assertIn(f"main at {SHAS['base'][:10]}", text)
        self.assertTrue(any(line.startswith("refs                head") for line in term.said), term.said)

    def test_parse_log_and_numstat(self):
        commits = pc.parse_log(fx("git", "log.txt"))
        self.assertEqual([c.sha for c in commits], SHAS["commits"])
        first, second, third = commits
        self.assertEqual((first.author, first.email, first.date), ("Octo Developer", "octo-dev@example.com", "2026-03-02T09:00:00Z"))
        self.assertEqual(first.message.split("\n")[0], "Add filter_widgets and Catalog.filtered")
        self.assertIn("so it can be reused.", first.message)
        self.assertEqual([(f.path, f.added, f.removed) for f in first.files], [("lib/widgets/catalog.py", 9, 0), ("lib/widgets/filter.py", 15, 0)])
        self.assertEqual(second.message, "Test the filter and the colour list")
        self.assertEqual([(f.path, f.added, f.removed) for f in second.files], [("tests/test_catalog.py", 9, 0)])
        self.assertEqual([(f.path, f.added, f.removed) for f in third.files], [("docs/diagram.png", None, None), ("docs/filtering.md", 8, 0)])
        self.assertEqual(pc.parse_log(""), [])
        self.assertEqual(pc.parse_numstat("junk line\n"), [])

    def test_numstat_paths(self):
        self.assertEqual(pc.numstat_path("docs/{usage.md => filtering.md}"), "docs/filtering.md")
        self.assertEqual(pc.numstat_path("{old => new}/file.py"), "new/file.py")
        self.assertEqual(pc.numstat_path("a.py => b.py"), "b.py")
        self.assertEqual(pc.numstat_path("plain.py"), "plain.py")

    def test_render_commits(self):
        repo = pc.Repo("github.com", "example", "widgets", self.root)
        c = pc.Collected(repo, "b", pc.Ticket(1), 42, {}, FIXED_TIME)
        self.assertEqual(pc.render_commits(c), "No commits between the merge base and the head.\n")
        c.commits = pc.parse_log(fx("git", "log.txt"))
        text = pc.render_commits(c)
        self.assertIn(f"## 1. {SHAS['commits'][0][:10]} · Octo Developer <octo-dev@example.com> · 2026-03-02T09:00:00Z", text)
        self.assertIn(f"Full sha: {SHAS['commits'][0]}", text)
        self.assertIn("The catalog can now be narrowed by colour or by a smallest size.", text)
        self.assertIn("Files: lib/widgets/catalog.py (+9 -0), lib/widgets/filter.py (+15 -0)", text)
        self.assertIn("Files: docs/diagram.png (binary), docs/filtering.md (+8 -0)", text)
        c.commits[0].files = []
        self.assertIn("Files: none", pc.render_commits(c))


class TestChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def section(self, text):
        return text.split("# pr-data: Checks\n", 1)[1].split("\n# pr-data: ", 1)[0]

    def test_helpers(self):
        self.assertEqual(pc.duration("2026-03-04T15:01:00Z", "2026-03-04T15:01:42Z"), "42s")
        self.assertEqual(pc.duration("2026-03-04T15:01:00Z", "2026-03-04T15:02:03Z"), "1m 3s")
        self.assertEqual(pc.duration("2026-03-04T15:01:00Z", "2026-03-04T17:02:03Z"), "2h 1m 3s")
        self.assertEqual(pc.duration("2026-03-04T15:01:00Z", "2026-03-04T15:00:00Z"), "0s")
        self.assertEqual(pc.duration(None, "x"), "")
        self.assertEqual(pc.duration("not a time", "2026-03-04T15:00:00Z"), "")
        self.assertEqual(pc.outcome({"conclusion": "FAILURE"}), "failure")
        self.assertEqual(pc.outcome({"status": "in_progress"}), "in progress")
        self.assertEqual(pc.outcome({"state": "pending"}), "pending")
        self.assertEqual(pc.outcome({}), "unknown")
        self.assertEqual([pc.mark({"conclusion": c}) for c in ("success", "failure", "skipped", "neutral", "timed_out")], ["PASS", "FAIL", "SKIP", "SKIP", "FAIL"])
        self.assertEqual(pc.mark({"status": "queued"}), "....")
        self.assertEqual(pc.mark({"conclusion": "mystery"}), "MYST")
        self.assertEqual(pc.mark({"conclusion": "ok"}), "OK  ")
        self.assertEqual(pc.trim_log(["a", "b", "c"], 2), (["b", "c"], "last 2 of 3 lines"))
        self.assertEqual(pc.trim_log(["a", "b"], 2), (["a", "b"], "2 lines"))
        self.assertEqual(pc.trim_log(["a", "b", "c"], None), (["a", "b", "c"], "3 lines"))
        steps = pc.parse_failed_log("j\ts\tone\nj\ts\ttwo\nj\tt\tthree\nbroken line\n")
        self.assertEqual(steps, {("j", "s"): ["one", "two"], ("j", "t"): ["three"]})

    def test_status_line_and_section(self):
        code, term, ctx = run_tool(self.root, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        line = "1 failing (tests); 2 successful (lint, ci/coverage); 1 skipped (docs); 1 in progress (build / image); not deployed"
        self.assertIn(f"**Checks:** {line}", text)
        self.assertIn("1 failed jobs", text)
        sec = self.section(text)
        for piece in [
            f"Status at {SHAS['head'][:10]}: {line}",
            "- PASS lint · GitHub Actions · success · 42s · https://github.com/example/widgets/actions/runs/5001/job/6001",
            "- FAIL tests · GitHub Actions · failure · 1m 3s · 1 test failed · ",
            "- SKIP docs · GitHub Actions · skipped · 1s",
            "- .... build / image · Example CI · in progress since 2026-03-04T15:05:00Z · https://ci.example.com/build/88",
            "- PASS ci/coverage · success · Coverage 94% (+1%) · https://coverage.example.com/widgets/42",
            "### CI run #17 (push) · failure · https://github.com/example/widgets/actions/runs/5001",
            "- PASS job lint · success · 42s · 3 steps\n- FAIL job tests · failure · 1m 3s\n  - PASS 1. Set up job · success",
            "- FAIL job tests · failure · 1m 3s",
            "  - FAIL 3. Run tests · failure",
            "  - SKIP 4. Upload report · skipped",
            "  - Annotations:\n    - failure tests/test_catalog.py:22 test_filtered_by_size: AssertionError: Lists differ: ['gear', 'spring'] != ['gear']\n    - warning .github:1 Node 16 actions are deprecated.",
            "  Log of step \"Run tests\" (last 200 of 229 lines):",
            "FAILED (failures=1)\n2026-03-04T15:02:02.002Z ##[error]Process completed with exit code 1.\n```",
            "  Log of step \"Upload report\" (1 lines):",
            "- SKIP job docs · skipped · 1s\n",
        ]:
            self.assertIn(piece, sec, piece)
        self.assertNotIn("  - PASS 2. Run lint", sec)
        self.assertIn("  - PASS 2. Install · success", sec)
        self.assertNotIn("collected test 29\n", sec)
        self.assertIn("collected test 30\n", sec)
        self.assertTrue(any(l.startswith("checks              1 failing") for l in term.said))

    def test_full_logs_keeps_everything(self):
        code, term, ctx = run_tool(self.root, yes=True, full_logs=True)
        sec = self.section((self.root / "pr-data.txt").read_text())
        self.assertIn("Log of step \"Run tests\" (229 lines):", sec)
        self.assertIn("collected test 1\n", sec)

    def test_logs_out_of_reach_are_named(self):
        sh = fixture_shell(self.root).on(["gh", "run", "view", "5001", "-R", REPO_ARG, "--log-failed"], "", "HTTP 403: Resource not accessible by integration", 1)
        code, term, ctx = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertIn("- **Not collected:**\n  - the failing-step logs of run CI #17 (jobs tests): HTTP 403", text)
        self.assertNotIn("Log of step", text)
        self.assertIn("  - Annotations:", text)

    def test_nothing_reported_and_deployed(self):
        api = ["gh", "api", "--hostname", "github.com", "--paginate"]
        head = SHAS["head"]
        sh = fixture_shell(self.root)
        sh.on([*api, f"repos/example/widgets/commits/{head}/check-runs"], "", "HTTP 500", 1)
        sh.on(["gh", "api", "--hostname", "github.com", f"repos/example/widgets/commits/{head}/status"], '{"statuses": []}')
        sh.on([*api, f"repos/example/widgets/actions/runs?head_sha={head}"], '{"workflow_runs": [{"id": 9, "name": "Nightly", "run_number": 2, "event": "schedule", "status": "in_progress", "conclusion": null, "created_at": "2026-03-04T15:30:00Z"}]}')
        sh.on([*api, "repos/example/widgets/actions/runs/9/jobs"], '{"jobs": [{"id": 90, "name": "sleep", "status": "in_progress", "conclusion": null, "steps": [{"name": "Wait", "status": "in_progress", "conclusion": null, "number": 1}]}]}')
        sh.on([*api, f"repos/example/widgets/deployments?sha={head}"], '[{"environment": "staging"}, {"environment": "staging"}]')
        code, term, ctx = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertIn("**Checks:** no checks reported at the head", text)
        sec = self.section(text)
        self.assertIn("## Check runs\n\nNone.", sec)
        self.assertIn("## Commit statuses\n\nNone.", sec)
        self.assertIn("### Nightly run #2 (schedule) · in progress since 2026-03-04T15:30:00Z\n\n- .... job sleep · in progress\n  - .... 1. Wait · in progress", sec)
        self.assertIn("0 failed jobs", text)
        self.assertIn("HTTP 500", ctx.missing[0])
        c = pc.Collected(pc.Repo("h", "o", "n", self.root), "b", pc.Ticket(1), 1, {}, FIXED_TIME)
        c.check_runs = [{"name": "x", "conclusion": "success"}]
        c.deployments = [{"environment": "staging"}, {"environment": "prod"}]
        self.assertEqual(pc.status_line(c), "1 successful (x); deployed to prod, staging")
        c.check_runs = [{"name": "y", "conclusion": "mystery"}]
        c.deployments = []
        self.assertEqual(pc.status_line(c), "1 other (y); not deployed")
        c.runs = []
        self.assertIn("## Actions runs\n\nNone.", pc.render_checks(c))
        c.runs = [{"id": 1, "name": "W", "run_number": 1, "event": "push", "conclusion": "failure", "status": "completed"}]
        c.jobs = {1: [{"id": 10, "name": "a", "conclusion": "failure"}, {"id": 11, "name": "b", "conclusion": "failure"}]}
        c.logs = {1: {("a", "s"): ["from a"], ("b", "s"): ["from b"]}}
        text = pc.render_checks(c)
        self.assertLess(text.index("from a"), text.index("from b"))
        self.assertEqual(text.count("Log of step"), 2)


class TestDiffAndFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def section(self, text, name):
        return text.split(f"# pr-data: {name}\n", 1)[1].split("\n# pr-data: ", 1)[0]

    def test_name_status(self):
        changes = pc.parse_name_status(fx("git", "name-status.txt"))
        self.assertEqual([(c.status, c.path, c.old_path) for c in changes], [
            ("added", "docs/diagram.png", None), ("renamed", "docs/filtering.md", "docs/usage.md"),
            ("modified", "lib/widgets/catalog.py", None), ("added", "lib/widgets/filter.py", None),
            ("modified", "tests/test_catalog.py", None)])
        self.assertEqual(pc.parse_name_status("D\tgone.py\nT\tlink\nX\todd\nbroken\n"), [
            pc.FileChange("removed", "gone.py"), pc.FileChange("type changed", "link"), pc.FileChange("x", "odd")])
        gone = pc.FileChange("removed", "gone.py")
        self.assertFalse(gone.at_head)
        self.assertTrue(gone.at_base)
        self.assertEqual(gone.base_path, "gone.py")
        self.assertFalse(pc.FileChange("added", "new.py").at_base)
        self.assertEqual(pc.FileChange("renamed", "b", "a").base_path, "a")

    def test_sections_and_counts(self):
        code, term, ctx = run_tool(self.root, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertIn("5 files changed", text)
        self.assertIn("  - The diff reconciles with GitHub's file list: 5 files, +41 -0.", text)
        self.assertNotIn("# pr-data: Diff\n", text)
        self.assertIn("- added, binary: docs/diagram.png\n- renamed (+8 -0): docs/usage.md -> docs/filtering.md\n- modified (+9 -0): lib/widgets/catalog.py", self.section(text, "Annotated diff"))
        head = self.section(text, "Files at head")
        self.assertIn("Not repeated here, added by the pull request; the Annotated diff section holds each whole: docs/diagram.png, lib/widgets/filter.py.\n\n"
                      f"Every other touched file whole at the head, at {SHAS['head']}.", head)
        self.assertNotIn("## docs/diagram.png", head)
        self.assertNotIn("## lib/widgets/filter.py", head)
        self.assertIn("## docs/filtering.md\n\n", head)
        self.assertIn("## lib/widgets/catalog.py\n\n", head)
        self.assertIn("## tests/test_catalog.py\n\n", head)
        self.assertNotIn("## docs/usage.md", head)
        base = self.section(text, "Files at base")
        self.assertTrue(base.startswith(f"\nEvery touched file whole at the base (the merge base), at {SHAS['merge_base']}."), base[:120])
        self.assertIn("## docs/usage.md\n\n13 lines.", base)
        self.assertNotIn("## lib/widgets/filter.py", base)
        self.assertNotIn("## docs/diagram.png", base)
        self.assertIn("## lib/widgets/catalog.py", base)
        self.assertTrue(any(l.startswith("diff                5 files, +41 -0") for l in term.said))
        self.assertIn("files               3 at the head, 3 at the base; 2 added whole in the annotated diff only", term.said)
        self.assertIn("- **Sections:** 1. Header · 2. Pull request · 3. Conversation · 4. Annotated diff · 5. Commits · 6. Checks · 7. Files at head · 8. Files at base", text)

    def test_plain_diff_flag(self):
        code, term, ctx = run_tool(self.root, yes=True, plain_diff=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        diff = self.section(text, "Diff")
        self.assertIn(f"`git diff {SHAS['merge_base']}...{SHAS['head']}`, 88 lines, as git wrote it", diff)
        self.assertIn("````diff\ndiff --git a/docs/diagram.png", diff)
        self.assertIn("+    return chosen\n", diff)
        self.assertIn("     unittest.main()\n````", diff)
        self.assertIn("· 6. Checks · 7. Diff · 8. Files at head · 9. Files at base", text)
        self.assertEqual([n for n, _ in pc.split_sections(text)], pc.SECTION_ORDER_ALL[:-2])

    def test_fenced_markdown_inside_markdown_survives(self):
        code, term, ctx = run_tool(self.root, yes=True)
        head = self.section((self.root / "pr-data.txt").read_text(), "Files at head")
        part = head.split("## docs/filtering.md", 1)[1].split("\n## lib/widgets/catalog.py", 1)[0]
        self.assertIn("\n````\n# Using the catalog", part)
        self.assertTrue(part.rstrip().endswith("````"))

    def test_no_files_flag(self):
        code, term, ctx = run_tool(self.root, yes=True, no_files=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertNotIn("# pr-data: Files at head", text)
        self.assertIn("The touched files were left out (--no-files).", text)
        self.assertIn("· 6. Checks\n", text)

    def test_reconciliation_differences_are_said(self):
        sh = fixture_shell(self.root).on(["gh", "api", "--hostname", "github.com", "--paginate", "repos/example/widgets/pulls/42/files"],
            '[{"filename": "docs/diagram.png", "additions": 0, "deletions": 0}, {"filename": "lib/widgets/other.py", "additions": 50, "deletions": 2}]')
        code, term, ctx = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 0, term.warned)
        note = ctx.notes[-1]
        self.assertIn("does NOT reconcile", note)
        self.assertIn("in the git diff but not on GitHub: docs/filtering.md, lib/widgets/catalog.py, lib/widgets/filter.py, tests/test_catalog.py", note)
        self.assertIn("on GitHub but not in the git diff: lib/widgets/other.py", note)
        self.assertIn("git counts +41 -0, GitHub +50 -2", note)
        self.assertEqual(len(term.warned), 1)

    def test_reconciliation_skipped_when_the_list_cannot_be_read(self):
        sh = fixture_shell(self.root).on(["gh", "api", "--hostname", "github.com", "--paginate", "repos/example/widgets/pulls/42/files"], "", "HTTP 502", 1)
        code, term, ctx = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 0, term.warned)
        self.assertFalse(any("reconcile" in n for n in ctx.notes))
        self.assertIn("HTTP 502", ctx.missing[0])

    def test_unreadable_file_and_empty_diff(self):
        sh = fixture_shell(self.root).on(["git", "show", f"{SHAS['head']}:lib/widgets/catalog.py"], "", "fatal: path not in tree", 128)
        code, term, ctx = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertIn("## lib/widgets/catalog.py\n\n(not readable; see the header)", text)
        self.assertIn(f"lib/widgets/catalog.py at {SHAS['head'][:10]}: fatal: path not in tree", text)
        c = pc.Collected(pc.Repo("h", "o", "n", self.root), "b", pc.Ticket(1), 1, {}, FIXED_TIME, merge_base="m", head_sha="h")
        self.assertTrue(pc.render_diff(c).startswith("`git diff m...h`, 0 lines, as git wrote it"))
        self.assertTrue(pc.render_diff(c).endswith("\n\n(empty)\n"))
        self.assertEqual(pc.render_file_list(c), "No files changed.\n")
        self.assertEqual(pc.render_files({}, "h", "at the head"), "No files at the head.\n")
        self.assertEqual(pc.render_files({}, "h", "at the base", ["b.py", "a.py"], "removed by the pull request"),
                         "Not repeated here, removed by the pull request; the Annotated diff section holds each whole: a.py, b.py.\n\nNo other files at the base.\n")

    def test_removed_files_are_not_repeated_at_the_base(self):
        c = pc.Collected(pc.Repo("h", "o", "n", self.root), "b", pc.Ticket(1), 1, {}, FIXED_TIME, merge_base="mb", head_sha="hd")
        c.files = [pc.FileChange("added", "new.py"), pc.FileChange("removed", "gone.py"), pc.FileChange("renamed", "b.py", "a.py")]
        sh = FakeShell().on(["git", "show", "hd:b.py"], "head b\n").on(["git", "show", "mb:a.py"], "base a\n")
        term = FakeTerminal()
        pc.fetch_files(pc.Context(sh, term, pc.Options()), c)
        self.assertEqual(len(sh.calls), 2)
        self.assertEqual((c.added_paths, c.removed_paths), (["new.py"], ["gone.py"]))
        self.assertEqual(c.head_paths, {"new.py", "b.py"})
        self.assertEqual(term.said, ["files               1 at the head, 1 at the base; 1 added and 1 removed whole in the annotated diff only"])
        text = pc.render_files(c.base_files, "mb", "at the base", c.removed_paths, "removed by the pull request")
        self.assertIn("holds each whole: gone.py.\n\nEvery other touched file whole at the base, at mb.\n\n## a.py\n\n1 lines.", text)
        self.assertEqual(pc.change_label(pc.FileChange("modified", "a.py", None, 3, None)), "modified (+3 -0): a.py")

    def test_git_diff_failure_stops(self):
        sh = fixture_shell(self.root).on(["git", "diff", "--no-color", "--no-ext-diff", f"{SHAS['merge_base']}...{SHAS['head']}"], "", "fatal: bad revision", 128)
        code, term, ctx = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 1)
        self.assertIn("git diff failed while taking the branch diff: fatal: bad revision", term.warned[0])

    def test_shell_decodes_bytes(self):
        sh = pc.Shell()
        r = sh.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\x00b\\xff')"])
        self.assertIn("\x00", r.stdout)
        self.assertEqual(len(r.stdout), 4)


class TestAnnotatedDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_diff_gutter_numbers_against_a_hand_count(self):
        files = pc.parse_diff(fx("git", "diff.patch"))
        self.assertEqual([f.path for f in files], ["docs/diagram.png", "docs/filtering.md", "lib/widgets/catalog.py", "lib/widgets/filter.py", "tests/test_catalog.py"])
        png, docs, catalog, flt, tests = files
        self.assertTrue(png.binary)
        self.assertEqual(png.hunks, [])
        self.assertEqual((docs.old_path, docs.new_path), ("docs/usage.md", "docs/filtering.md"))
        self.assertEqual(len(catalog.hunks), 2)
        h1, h2 = catalog.hunks
        self.assertEqual(h1.header, "@@ -2,6 +2,8 @@")
        self.assertEqual([(l.kind, l.old, l.new) for l in h1.lines], [
            ("context", 2, 2), ("context", 3, 3), ("context", 4, 4), ("added", None, 5), ("added", None, 6),
            ("context", 5, 7), ("context", 6, 8), ("context", 7, 9)])
        self.assertEqual(h1.lines[3].text, "from lib.widgets.filter import filter_widgets")
        self.assertEqual([(l.kind, l.old, l.new) for l in h2.lines][:5], [
            ("context", 32, 34), ("context", 33, 35), ("context", 34, 36), ("added", None, 37), ("added", None, 38)])
        self.assertEqual(h2.lines[4].text, "    def filtered(self, colour=None, min_size=None):")
        self.assertEqual(flt.old_path, "/dev/null")
        self.assertEqual([l.new for l in flt.hunks[0].lines][:3], [1, 2, 3])
        self.assertEqual(flt.hunks[0].lines[2].text, "MAX_SIZE = 10")
        self.assertEqual(tests.hunks[0].lines[4].new, 20)

    def test_parse_diff_removed_lines_no_newline_and_junk(self):
        patch = (
            "junk before the first file\n"
            "diff --git a/a.txt b/a.txt\nindex 1..2 100644\n--- a/a.txt\n+++ b/a.txt\n"
            "@@ -1,3 +1,2 @@\n one\n-two\n-three\n+four\n\\ No newline at end of file\n"
            "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
        )
        a, gone = pc.parse_diff(patch)
        self.assertEqual([(l.kind, l.old, l.new, l.text) for l in a.hunks[0].lines], [
            ("context", 1, 1, "one"), ("removed", 2, None, "two"), ("removed", 3, None, "three"), ("added", None, 2, "four")])
        self.assertEqual(a.hunks[0].lines[-1].note, "No newline at end of file")
        self.assertEqual(pc.gutter(a.hunks[0].lines[-1]), "            2 + four   \\ No newline at end of file")
        self.assertEqual(pc.gutter(a.hunks[0].lines[1]), "     2        - two")
        self.assertEqual(pc.gutter(a.hunks[0].lines[0]), "     1      1   one")
        self.assertEqual(gone.new_path, "/dev/null")
        self.assertEqual(gone.path, "gone.txt")
        self.assertEqual(pc.parse_diff(""), [])
        self.assertEqual(pc.parse_diff("@@ -1 +1 @@\n+x\n\\ stray\n"), [])
        trailing = pc.parse_diff("diff --git a/t b/t\n--- a/t\n+++ b/t\n@@ -1 +1 @@\n-x\n+y\n")
        self.assertEqual(len(trailing[0].hunks[0].lines), 2)
        stripped = pc.parse_diff("diff --git a/t b/t\n--- a/t\n+++ b/t\n@@ -1,2 +1,2 @@\n\n-x\n+y\n")
        self.assertEqual([(l.kind, l.old, l.new, l.text) for l in stripped[0].hunks[0].lines], [("context", 1, 1, ""), ("removed", 2, None, "x"), ("added", None, 2, "y")])

    def test_section_places_every_thread(self):
        code, term, ctx = run_tool(self.root, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        sec = text.split("# pr-data: Annotated diff\n", 1)[1].split("\n# pr-data: Commits", 1)[0]
        self.assertTrue(sec.startswith("\nFiles changed:\n\n- added, binary: docs/diagram.png"))
        self.assertIn("## added, binary: docs/diagram.png\n\n(binary, no hunks)", sec)
        self.assertIn("## renamed (+8 -0): docs/usage.md -> docs/filtering.md\n\nHunk `@@ -11,3 +11,11 @@ catalog.largest()`:", sec)
        # T1 on filter.py new line 3, right after the line, the fence closed and reopened
        self.assertIn("            3 + MAX_SIZE = 10\n```\n\n**Review thread** on lib/widgets/filter.py line 3 (new side) · unresolved", sec)
        self.assertIn("> It was meant for a later `max_size` option. I will drop it until that lands.\n\n```\n            4 + \n", sec)
        # T4 on the old side of a context line, T2 in the second hunk
        self.assertIn("     3      3   from dataclasses import dataclass\n```\n\n**Review thread** on lib/widgets/catalog.py line 3 (old side)", sec)
        self.assertIn("           38 +     def filtered(self, colour=None, min_size=None):\n```\n\n**Review thread** on lib/widgets/catalog.py line 38 (new side) · resolved", sec)
        # T3 in the tests, T5 outdated with its original line quoted
        self.assertIn("           20 +         self.assertEqual(Catalog().filtered(colour=\"red\").names(), [\"bolt\", \"spring\"])\n```\n\n**Review thread** on tests/test_catalog.py line 20", sec)
        self.assertIn("### Threads on lines not in this diff (outdated)\n\nOriginally on line 7 at " + SHAS["commits"][0][:10] + ": `\"\"\"The widgets matching every given condition, in their original order.\"\"\"`\n\n**Review thread** on lib/widgets/filter.py, originally line 7 (new side), outdated · resolved", sec)
        self.assertEqual(sec.count("**Review thread**"), 5)
        self.assertNotIn("## Threads on files not in this diff", sec)

    def test_stray_threads_unreadable_original_and_empty_diff(self):
        sh = fixture_shell(self.root).on(["git", "show", f"{SHAS['commits'][0]}:lib/widgets/filter.py"], "", "fatal: gone", 128)
        extra = fx("gh", "review-threads.json").replace('"path": "tests/test_catalog.py"', '"path": "lib/elsewhere.py"')
        sh.on(graphql_argv(), extra)
        code, term, ctx = run_tool(self.root, sh, yes=True)
        self.assertEqual(code, 0, term.warned)
        sec = (self.root / "pr-data.txt").read_text().split("# pr-data: Annotated diff\n", 1)[1].split("\n# pr-data: Commits", 1)[0]
        self.assertIn("Originally on line 7; the line could not be read from " + SHAS["commits"][0][:10] + ".", sec)
        self.assertIn("## Threads on files not in this diff\n\n**Review thread** on lib/elsewhere.py line 20 (new side)", sec)
        self.assertIn(f"lib/widgets/filter.py at {SHAS['commits'][0][:10]}: fatal: gone", ctx.missing[0])
        c = pc.Collected(pc.Repo("h", "o", "n", self.root), "b", pc.Ticket(1), 1, {}, FIXED_TIME, merge_base="m", head_sha="h")
        ctx2 = pc.Context(FakeShell(), FakeTerminal(), pc.Options())
        self.assertIn("(no hunks: the diff is empty)", pc.render_annotated_diff(ctx2, c))
        t = pc.Thread("x", "p", None, 999, None, "RIGHT", False, True, [pc.ThreadComment(1, "a", "b", "t", "", "abc")])
        cache = {("abc", "p"): "one\ntwo\n"}
        self.assertIsNone(pc.original_line_text(ctx2, t, cache))
        t.original_line = 2
        self.assertEqual(pc.original_line_text(ctx2, t, cache), "two")
        t.comments = []
        self.assertIsNone(pc.original_line_text(ctx2, t, cache))
        self.assertFalse(pc.line_matches(pc.Thread("x", "p", None, 1, None, "RIGHT", False, True, []), pc.DiffLine("added", None, 1, "")))
        blank = pc.Thread("x", "p", None, 1, None, "RIGHT", False, True, [pc.ThreadComment(1, "a", "b", "t", "", "abc")])
        self.assertIn("`(blank)`", pc.render_annotated_file(ctx2, pc.DiffFile("p", "p", [], []), None, [blank], {("abc", "p"): "\n\n"}))


class TestTheFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_eight_sections_in_order_and_split_back(self):
        code, term, ctx = run_tool(self.root, yes=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        names = [n for n, _ in pc.split_sections(text)]
        self.assertEqual(names, pc.SECTION_ORDER)
        self.assertEqual(len(names), 8)
        self.assertEqual([n for n in pc.SECTION_ORDER_ALL if n in pc.SECTION_ORDER], pc.SECTION_ORDER)
        self.assertEqual(pc.split_sections("no heading\n"), [])
        self.assertEqual(pc.split_sections("# pr-data: A\n\nx\n# pr-data: B\n"), [("A", "\nx"), ("B", "")])

    def test_nothing_is_fetched_twice(self):
        code, term, ctx = run_tool(self.root, yes=True)
        calls = [tuple(c) for c in ctx.shell.calls]
        self.assertEqual(len(calls), len(set(calls)), [c for c in calls if calls.count(c) > 1])
        self.assertEqual(ctx.shell.gh_calls, 17)
        self.assertIn("**Requests to GitHub:** 17", (self.root / "pr-data.txt").read_text())

    def test_header_carries_the_masking_note_and_the_progress_lines_name_every_part(self):
        code, term, ctx = run_tool(self.root, yes=True)
        text = (self.root / "pr-data.txt").read_text()
        self.assertIn("Secrets that GitHub masks in Actions logs arrive masked; nothing else is masked.", text)
        starts = [line.split()[0] for line in term.said]
        for part in ("ticket", "pull", "refs", "conversation", "commits", "checks", "diff", "files", "wrote", "Do"):
            self.assertIn(part, starts, part)

    def test_the_file_is_written_only_at_the_root_and_nothing_else(self):
        (self.root / "sub").mkdir()
        code, term, ctx = run_tool(self.root, yes=True)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["pr-data.txt", "sub"])
        self.assertEqual(list((self.root / "sub").iterdir()), [])


class TestRelatedAndRedact(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    GREP = ["git", "grep", "-l", "-w", "-I", "-e", "filtered", "-e", "colours", "-e", "MAX_SIZE", "-e", "filter_widgets",
            "-e", "test_filtered_by_colour", "-e", "test_filtered_by_size", "-e", "test_colours"]

    def test_identifiers_added(self):
        self.assertEqual(pc.identifiers_added(fx("git", "diff.patch")),
                         ["filtered", "colours", "MAX_SIZE", "filter_widgets", "test_filtered_by_colour", "test_filtered_by_size", "test_colours"])
        patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,4 @@\n+const widgetCount = 1\n+class Widget:\n-def old():\n+  x = 1\n"
        self.assertEqual(pc.identifiers_added(patch), ["widgetCount", "Widget"])

    def test_related_files_are_added(self):
        head = SHAS["head"]
        sh = fixture_shell(self.root).on([*self.GREP, head], f"{head}:lib/widgets/catalog.py\n{head}:lib/widgets/service.py\n{head}:README.md\n")
        sh.on(["git", "show", f"{head}:lib/widgets/service.py"], "from lib.widgets.filter import filter_widgets\n")
        sh.on(["git", "show", f"{head}:README.md"], "Use filtered().\n")
        code, term, ctx = run_tool(self.root, sh, yes=True, related=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertIn("# pr-data: Related files\n\nEvery touched file whole related to the change, at the head, at " + head, text)
        self.assertIn("## lib/widgets/service.py", text)
        self.assertIn("## README.md", text)
        self.assertNotIn("## lib/widgets/catalog.py\n\n1 lines", text)
        self.assertIn("Related files (--related): 7 identifiers (filtered, colours, MAX_SIZE, filter_widgets, test_filtered_by_colour, test_filtered_by_size, test_colours); 2 files name one of them.", text)
        self.assertIn("· 9. Related files", text)
        self.assertTrue(any(l.startswith("related             2 files") for l in term.said))

    def test_related_cap_grep_failure_and_no_identifiers(self):
        head = SHAS["head"]
        many = "".join(f"{head}:extra/file{i:02d}.py\n" for i in range(30))
        sh = fixture_shell(self.root).on([*self.GREP, head], many)
        for i in range(pc.RELATED_CAP):
            sh.on(["git", "show", f"{head}:extra/file{i:02d}.py"], f"# {i}\n")
        code, term, ctx = run_tool(self.root, sh, yes=True, related=True)
        self.assertEqual(code, 0, term.warned)
        self.assertIn("30 files name one of them, the first 25 included.", ctx.notes[-1])
        sh = fixture_shell(self.root).on([*self.GREP, head], "", "fatal: bad object", 128)
        code, term, ctx = run_tool(self.root, sh, yes=True, related=True)
        self.assertEqual(code, 0, term.warned)
        self.assertIn("related files (git grep): fatal: bad object", ctx.missing[0])
        sh = fixture_shell(self.root).on([*self.GREP, head], "", "", 1)
        code, term, ctx = run_tool(self.root, sh, yes=True, related=True)
        self.assertIn("0 files name one of them.", ctx.notes[-1])
        sh = fixture_shell(self.root).on(["git", "diff", "--no-color", "--no-ext-diff", f"{SHAS['merge_base']}...{head}"], "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
        code, term, ctx = run_tool(self.root, sh, yes=True, related=True)
        self.assertEqual(code, 0, term.warned)
        self.assertIn("the diff defines no identifier to look for", ctx.notes[-1])

    def test_redact_text_spares_gutter_and_shas(self):
        rx = [re.compile(r"\b\d{3}\b"), re.compile("secret")]
        text = "     12    345   call(123) secret\n" + ("a" * 40) + " 777 " + "0123456789abcdef0123456789abcdef01234567 123\nplain 999 secret"
        out, n = pc.redact_text(text, rx)
        self.assertEqual(out.split("\n")[0], "     12    345   call([REDACTED]) [REDACTED]")
        self.assertEqual(out.split("\n")[1], "a" * 40 + " [REDACTED] 0123456789abcdef0123456789abcdef01234567 123")
        self.assertEqual(out.split("\n")[2], "plain [REDACTED] [REDACTED]")
        self.assertEqual(n, 5)
        sha_only = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(pc.redact_text(sha_only, [re.compile("456")]), (sha_only, 0))

    def test_redact_flag_end_to_end(self):
        code, term, ctx = run_tool(self.root, yes=True, redact=[r"reviewer-two", r"\bMAX_SIZE\b"])
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertNotIn("reviewer-two", text.split("# pr-data: Pull request", 1)[1])
        self.assertIn("**@[REDACTED]** · 2026-03-03T09:01:00Z", text)
        self.assertIn("            3 + [REDACTED] = 10", text)
        self.assertIn("Redactions (--redact): Pull request 1, Conversation ", text)
        self.assertIn(SHAS["head"], text)
        code, term, ctx = run_tool(self.root, yes=True, redact=["nothing-like-this"])
        self.assertIn("Redactions (--redact): nothing matched.", ctx.notes[-1])

    def test_redact_bad_pattern_stops(self):
        code, term, ctx = run_tool(self.root, yes=True, redact=["("])
        self.assertEqual(code, 1)
        self.assertIn("--redact '(' is not a valid pattern", term.warned[0])


class TestUses(unittest.TestCase):
    """--uses: the files at the head that define what the diff's added lines name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    RUBY = (
        "diff --git a/x.rb b/x.rb\n--- a/x.rb\n+++ b/x.rb\n@@ -1 +1,9 @@\n"
        "+# OrderNotice is only mentioned in this comment\n"
        "+notice = OrderNotice.where(order: order, detail: OrderNotice::URGENT)&.max_by(&:event_date)\n"
        "+shipment = Shipment.where('status != ?', Shipment::ERROR).first\n"
        "+message = create(:order_message, numbers: [order.number])\n"
        "+order.update_order_status(notice) if notice.is_a?(OrderNotice)\n"
        "+tracker.append_message(\"Updated #{Order.name} #{order.id} with #{Alpha}\")\n"
        "+def helper_one(x)\n"
        "+  helper_two(x) + Time.zone.now\n"
        "+end\n"
    )

    def test_identifiers_used(self):
        # Classes and constants, a factory, methods with underscores; not what the diff defines
        # (helper_one), not strings or comments, not the language's own (Time, where, is_a).
        self.assertEqual(pc.identifiers_used(self.RUBY),
                         ["OrderNotice", "URGENT", "Shipment", "ERROR", "order_message", "update_order_status", "append_message", "helper_two"])
        # The fixture: Catalog is used by the new code; prose in docs/filtering.md is skipped.
        self.assertEqual(pc.identifiers_used(fx("git", "diff.patch")), ["Catalog"])
        self.assertEqual(pc.identifiers_used("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b = 1\n"), [])

    def test_grep_arguments(self):
        args = pc.uses_grep_args(["OrderNotice", "update_order_status"])
        self.assertEqual(args[:4], ["grep", "-I", "-E", "-n"])
        patterns = args[5::2]
        self.assertEqual(len(patterns), 3)
        self.assertIn("(def[[:space:]]+(self\\.)?|(class|module|function|struct|enum|interface|type|const|let|var|val)[[:space:]]+)(OrderNotice|update_order_status)([^A-Za-z0-9_]|$)", patterns[0])
        self.assertIn("(scope|factory|has_many|has_one|belongs_to|attr_accessor|attr_reader|attr_writer|define_method)[[:space:]]+:(OrderNotice|update_order_status)", patterns[1])
        self.assertEqual(patterns[2], "^[[:space:]]*(OrderNotice)[[:space:]]*=[^=]")
        self.assertEqual(len(pc.uses_grep_args(["lower_only"])[5::2]), 2)

    def grep_argv(self):
        return ["git", *pc.uses_grep_args(["Catalog"]), SHAS["head"]]

    def test_section_is_added(self):
        head = SHAS["head"]
        sh = fixture_shell(self.root).on(self.grep_argv(), (
            f"{head}:lib/widgets/catalog.py:21:class Catalog:\n"  # touched: never repeated
            f"{head}:lib/widgets/legacy.py:3:class Catalog:\n"
            f"{head}:lib/widgets/legacy.py:9:    def Catalog(self):\n"
            f"{head}:docs/notes.md:1:Catalog = the list\n"
            "a line without the shape\n"
        ))
        sh.on(["git", "show", f"{head}:lib/widgets/legacy.py"], "class Catalog:\n    pass\n")
        sh.on(["git", "show", f"{head}:docs/notes.md"], "Catalog = the list\n")
        code, term, ctx = run_tool(self.root, sh, yes=True, uses=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        sec = text.split("# pr-data: Files the diff uses\n", 1)[1]
        self.assertTrue(sec.startswith(f"\nFiles at the head, at {head}, that define what the diff uses: a class, a constant, a method, a factory named on an added line. Looked for the definitions of 1 identifiers.\n\n## docs/notes.md\n\nDefines: Catalog.\n\n1 lines.\n\n```\nCatalog = the list\n```\n\n\n## lib/widgets/legacy.py\n\nDefines: Catalog.\n\n2 lines."), sec[:400])
        self.assertNotIn("## lib/widgets/catalog.py\n\nDefines", text)
        self.assertIn("Files the diff uses (--uses): looked for the definitions of 1 identifiers; 2 files define one of them.", text)
        self.assertIn("· 8. Files at base · 9. Files the diff uses\n", text)
        self.assertIn("uses                2 files at the head", term.said)
        self.assertEqual(ctx.shell.gh_calls, 17)

    def test_uses_before_related_and_both_numbered(self):
        head = SHAS["head"]
        sh = fixture_shell(self.root).on(self.grep_argv(), f"{head}:lib/widgets/legacy.py:3:class Catalog:\n")
        sh.on(["git", "show", f"{head}:lib/widgets/legacy.py"], "class Catalog:\n")
        sh.on([*TestRelatedAndRedact.GREP, head], f"{head}:README.md\n")
        sh.on(["git", "show", f"{head}:README.md"], "Use filtered().\n")
        code, term, ctx = run_tool(self.root, sh, yes=True, uses=True, related=True)
        self.assertEqual(code, 0, term.warned)
        text = (self.root / "pr-data.txt").read_text()
        self.assertEqual([n for n, _ in pc.split_sections(text)], pc.SECTION_ORDER_ALL[:6] + pc.SECTION_ORDER_ALL[7:])
        self.assertIn("· 9. Files the diff uses · 10. Related files", text)

    def test_cap_grep_failure_no_match_and_no_identifiers(self):
        head = SHAS["head"]
        many = "".join(f"{head}:extra/file{i:02d}.py:1:class Catalog:\n" for i in range(30))
        sh = fixture_shell(self.root).on(self.grep_argv(), many)
        for i in range(pc.USES_CAP):
            sh.on(["git", "show", f"{head}:extra/file{i:02d}.py"], "class Catalog:\n")
        code, term, ctx = run_tool(self.root, sh, yes=True, uses=True)
        self.assertEqual(code, 0, term.warned)
        self.assertIn("30 files define one of them, the first 25 included.", ctx.notes[-1])
        self.assertEqual(len(ctx.shell.calls), len(set(tuple(c) for c in ctx.shell.calls)))
        sh = fixture_shell(self.root).on(self.grep_argv(), "", "fatal: bad object", 128)
        code, term, ctx = run_tool(self.root, sh, yes=True, uses=True)
        self.assertEqual(code, 0, term.warned)
        self.assertIn("files the diff uses (git grep): fatal: bad object", ctx.missing[0])
        self.assertNotIn("# pr-data: Files the diff uses", (self.root / "pr-data.txt").read_text())
        sh = fixture_shell(self.root).on(self.grep_argv(), "", "", 1)
        code, term, ctx = run_tool(self.root, sh, yes=True, uses=True)
        self.assertIn("0 files define one of them.", ctx.notes[-1])
        self.assertNotIn("# pr-data: Files the diff uses", (self.root / "pr-data.txt").read_text())
        sh = fixture_shell(self.root).on(["git", "diff", "--no-color", "--no-ext-diff", f"{SHAS['merge_base']}...{head}"], "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b = 1\n")
        code, term, ctx = run_tool(self.root, sh, yes=True, uses=True)
        self.assertEqual(code, 0, term.warned)
        self.assertIn("the diff uses no identifier to look for", ctx.notes[-1])


class TestDocumentation(unittest.TestCase):
    def test_readme_and_format_describe_the_tool(self):
        readme = (HERE / "README.md").read_text()
        for piece in ("## Setup", "## Usage", "### Options", "### The ticket number", "pr-data.txt", "PR-DATA-FORMAT.md",
                      "--yes", "--no-files", "--full-logs", "--plain-diff", "--uses", "--related", "--redact", "Python 3.11"):
            self.assertIn(piece, readme, piece)
        spec = (HERE / "PR-DATA-FORMAT.md").read_text()
        for name in pc.SECTION_ORDER_ALL:
            self.assertIn(f"# pr-data: {name}", spec)
        self.assertIn("Review thread", spec)

    def test_no_forbidden_words_in_this_folder(self):
        """A list of words that must not appear anywhere in this folder is kept outside
        it; point PR_COLLECT_FORBIDDEN_WORDS at that file to run the check."""
        listed = os.environ.get("PR_COLLECT_FORBIDDEN_WORDS")
        if not listed:
            self.skipTest("PR_COLLECT_FORBIDDEN_WORDS is not set")
        words = [w.strip() for w in Path(listed).read_text().splitlines() if w.strip() and not w.startswith("#")]
        hits = []
        for path in HERE.rglob("*"):
            parts = path.relative_to(HERE).parts
            if not path.is_file() or parts[0] in (".git", "__pycache__", ".coverage", "htmlcov"):
                continue
            name = "/".join(parts)
            text = path.read_bytes().decode("utf-8", errors="replace")
            for w in words:
                rx = re.escape(w)
                if w[0].isalnum():
                    rx = r"\b" + rx
                if w[-1].isalnum():
                    rx = rx + r"\b"
                if re.search(rx, text, re.IGNORECASE) or re.search(rx, name, re.IGNORECASE):
                    hits.append((name, w))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
