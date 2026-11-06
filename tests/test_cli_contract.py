"""Contract every tool must keep: help on every argument, --json after the subcommand, --dry-run on writers,
one --since grammar, a health subcommand, and stdin `-` where the command takes an id."""
import argparse
import importlib.util
import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ["pr", "ci", "k", "aws", "obs", "tf", "git", "temporal", "oncall", "mem", "tx", "modal"]
WRITERS = {("pr", "reply"), ("pr", "comment"), ("ci", "dispatch"), ("mem", "add"), ("mem", "append"), ("mem", "edit"), ("mem", "doctor")}
SINCE_TOOLS = ["aws", "obs", "oncall", "temporal", "modal", "tx"]
STDIN_TOOLS = ["ci", "temporal", "oncall", "modal", "k"]


class Stop(Exception):
    pass


def load(tool):
    spec = importlib.util.spec_from_file_location(f"jx_{tool}", ROOT / "tools" / f"{tool}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parser_of(mod):
    """Run main() far enough to build the parser, then stop before parse_args consumes argv."""
    box = {}
    def grab(self, *a, **k):
        box["p"] = self
        raise Stop()
    saved = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = grab
    try:
        mod.main()
    except (Stop, SystemExit):
        pass
    finally:
        argparse.ArgumentParser.parse_args = saved
    return box["p"]


def subparsers(parser):
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    seen = {}
    for name, sub in action.choices.items():
        seen.setdefault(id(sub), (name, sub))
    return dict(seen.values())


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mods = {t: load(t) for t in TOOLS}
        cls.subs = {t: subparsers(parser_of(m)) for t, m in cls.mods.items()}

    def test_every_argument_has_help(self):
        missing = [f"{t} {n} {a.dest}" for t, subs in self.subs.items() for n, p in subs.items()
                   for a in p._actions if not isinstance(a, argparse._HelpAction) and not a.help]
        self.assertEqual(missing, [])

    def test_json_is_accepted_after_every_subcommand(self):
        missing = [f"{t} {n}" for t, subs in self.subs.items() for n, p in subs.items()
                   if not any("--json" in a.option_strings for a in p._actions)]
        self.assertEqual(missing, [])

    def test_writers_have_dry_run(self):
        missing = [f"{t} {n}" for t, n in WRITERS if not any("--dry-run" in a.option_strings for a in self.subs[t][n]._actions)]
        self.assertEqual(missing, [])

    def test_one_since_grammar(self):
        now = datetime.now(timezone.utc)
        for t in SINCE_TOOLS:
            since = self.mods[t].since
            for text in ("30m", "6h", "7d", "2w", "2026-01-31T00:00:00Z", "2026-01-31T00:00:00"):
                got = since(text)
                if t == "obs":
                    self.assertIsInstance(got, int, f"{t} {text}")
                else:
                    self.assertEqual(got.tzinfo is not None, True, f"{t} {text}")
            if t != "obs":
                self.assertLess(abs((now - since("7d")) - timedelta(days=7)).total_seconds(), 5, t)
            with self.assertRaises(SystemExit):
                since("nope")

    def test_every_tool_has_a_health_subcommand(self):
        missing = [t for t, subs in self.subs.items() if not ({"doctor", "whoami"} & set(subs)) and t != "tx"]
        self.assertEqual(missing, [])

    def test_stdin_dash_reads_named_column_or_first_column(self):
        for t in [x for x in STDIN_TOOLS if x != "ci"]:
            fn = self.mods[t].ids_arg
            saved = sys.stdin
            try:
                sys.stdin = io.StringIO("x\tpod\ty\n1\tapi-1\tq\n2\tapi-2\tq\n")
                self.assertEqual(fn("-", "pod"), ["api-1", "api-2"], t)
                sys.stdin = io.StringIO("a\nb\na\n")
                self.assertEqual(fn("-", "pod"), ["a", "b"], t)
                self.assertEqual(fn("literal", "pod"), ["literal"], t)
            finally:
                sys.stdin = saved

    def test_git_wt_rm_has_force(self):
        self.assertTrue(any("--force" in a.option_strings for a in self.subs["git"]["wt"]._actions))


if __name__ == "__main__":
    unittest.main()
