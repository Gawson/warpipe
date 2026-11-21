"""Translation helper: language choice, the Polish catalog, CLI in both languages."""

import ast
import contextlib
import io
import json
import os
import re
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, FakePw
from scenarios import per_app

from warpipe import cli, i18n
from warpipe.state import Config

PKG = Path(ROOT) / "warpipe"
POLISH_CHARS = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ„]")


def _fields(s: str) -> set[str]:
    return {f for _lit, f, _spec, _conv in string.Formatter().parse(s) if f is not None}


def scan_msgids() -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(msgid -> where, plural msgid -> singular, non-literal call sites) from warpipe/*.py."""
    msgids: dict[str, str] = {}
    plurals: dict[str, str] = {}
    dynamic: list[str] = []
    for path in sorted(PKG.glob("*.py")):
        if path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            name, args = node.func.id, node.args
            where = f"{path.name}:{node.lineno}"
            if name in ("_", "N_", "gettext"):
                if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                    msgids.setdefault(args[0].value, where)
                else:
                    dynamic.append(where)
            elif name == "ngettext":
                if len(args) >= 2 and all(isinstance(a, ast.Constant) for a in args[:2]):
                    plurals[args[1].value] = args[0].value
                    msgids.setdefault(args[0].value, where)
                else:
                    dynamic.append(where)
    return msgids, plurals, dynamic


class LanguageCase(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")


class TestDetectLanguage(LanguageCase):
    def test_env_override_wins(self):
        env = {"WARPIPE_LANG": "pl", "LANG": "en_US.UTF-8"}
        self.assertEqual(i18n.detect_language("en", env), "pl")
        self.assertEqual(i18n.detect_language("pl", {"WARPIPE_LANG": "en", "LANG": "pl_PL.UTF-8"}), "en")
        # "auto" (or empty) in the environment does not force anything
        self.assertEqual(i18n.detect_language("pl", {"WARPIPE_LANG": "auto"}), "pl")
        self.assertEqual(i18n.detect_language(None, {"WARPIPE_LANG": "", "LANG": "pl_PL.UTF-8"}), "pl")

    def test_config_setting(self):
        self.assertEqual(i18n.detect_language("pl", {"LANG": "en_US.UTF-8"}), "pl")
        self.assertEqual(i18n.detect_language("en", {"LANG": "pl_PL.UTF-8"}), "en")
        self.assertEqual(i18n.detect_language(" PL ", {}), "pl")
        # unsupported languages fall back to English, not to the locale
        self.assertEqual(i18n.detect_language("de", {"LANG": "pl_PL.UTF-8"}), "en")
        # junk = auto
        self.assertEqual(i18n.detect_language(42, {"LANG": "pl_PL.UTF-8"}), "pl")

    def test_auto_follows_locale_in_posix_order(self):
        self.assertEqual(i18n.detect_language("auto", {"LANG": "pl_PL.UTF-8"}), "pl")
        self.assertEqual(i18n.detect_language("auto", {"LANG": "en_GB.UTF-8"}), "en")
        self.assertEqual(i18n.detect_language("auto", {"LC_ALL": "pl_PL.UTF-8", "LANG": "en_US.UTF-8"}), "pl")
        self.assertEqual(i18n.detect_language("auto", {"LC_ALL": "C", "LC_MESSAGES": "pl_PL"}), "en")
        self.assertEqual(i18n.detect_language(None, {"LC_ALL": "", "LC_MESSAGES": "pl_PL", "LANG": "C"}), "pl")
        self.assertEqual(i18n.detect_language(None, {}), "en")

    def test_config_file_is_used_lazily(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text(json.dumps({"language": "pl"}), encoding="utf-8")
            env = {"DISCORD_DZWIEK_CONFIG_DIR": d, "LANG": "en_US.UTF-8"}
            with mock.patch.dict(os.environ, env):
                os.environ.pop("WARPIPE_LANG", None)
                i18n.set_language(None)
                self.assertEqual(i18n.language(), "pl")
                self.assertEqual(i18n._("Quit"), "Zakończ")
                self.assertEqual(Config(Path(d) / "config.json").language, "pl")
                # broken config = auto (locale)
                (Path(d) / "config.json").write_text("{nope", encoding="utf-8")
                i18n.set_language(None)
                self.assertEqual(i18n.language(), "en")
            self.assertEqual(Config(Path(d) / "missing.json").language, "auto")

    def test_set_language(self):
        i18n.set_language("pl")
        self.assertEqual(i18n._("Quit"), "Zakończ")
        self.assertEqual(i18n._("not a known msgid"), "not a known msgid")
        i18n.set_language("en")
        self.assertEqual(i18n._("Quit"), "Quit")


class TestPlural(LanguageCase):
    def test_english(self):
        self.assertEqual(i18n.ngettext("{n} stream", "{n} streams", 1).format(n=1), "1 stream")
        self.assertEqual(i18n.ngettext("{n} stream", "{n} streams", 3).format(n=3), "3 streams")

    def test_polish_forms(self):
        i18n.set_language("pl")
        got = {n: i18n.ngettext("{n} stream", "{n} streams", n).format(n=n) for n in (1, 2, 4, 5, 12, 22, 25, 102)}
        self.assertEqual(got, {1: "1 strumień", 2: "2 strumienie", 4: "4 strumienie", 5: "5 strumieni",
                               12: "12 strumieni", 22: "22 strumienie", 25: "25 strumieni",
                               102: "102 strumienie"})


class TestCatalog(unittest.TestCase):
    def test_every_msgid_has_a_polish_translation(self):
        msgids, plurals, _dynamic = scan_msgids()
        self.assertGreater(len(msgids), 100)
        missing = sorted(m for m in msgids if m not in i18n.PL and m not in plurals.values())
        self.assertEqual(missing, [], "msgids without a Polish translation")
        missing_pl = sorted(p for p in plurals if p not in i18n.PL_PLURAL)
        self.assertEqual(missing_pl, [], "plural msgids without Polish forms")

    def test_no_stale_entries(self):
        msgids, plurals, _dynamic = scan_msgids()
        self.assertEqual(sorted(set(i18n.PL) - set(msgids)), [], "catalog entries nobody uses")
        self.assertEqual(sorted(set(i18n.PL_PLURAL) - set(plurals)), [])

    def test_placeholders_match(self):
        for msgid, msgstr in i18n.PL.items():
            self.assertEqual(_fields(msgid), _fields(msgstr), msgid)
        msgids, plurals, _dynamic = scan_msgids()
        for plural, forms in i18n.PL_PLURAL.items():
            self.assertEqual(len(forms), 3)
            for f in forms:
                self.assertEqual(_fields(f), _fields(plural), plural)
            self.assertEqual(_fields(plurals[plural]), _fields(plural))

    def test_only_known_dynamic_lookups(self):
        """_() on a variable is fine only for N_()-marked tables (status/kind labels, reasons)."""
        _msgids, _plurals, dynamic = scan_msgids()
        self.assertEqual(sorted(w.split(":")[0] for w in dynamic),
                         ["cli.py", "planner.py", "planner.py", "planner.py"], dynamic)

    def test_no_polish_left_in_the_code(self):
        """Every Polish text lives in the catalog; code, comments and docstrings are English."""
        offenders = []
        for path in sorted(PKG.glob("*.py")) + [Path(ROOT) / "bin" / "warpipe"]:
            if path.name == "i18n.py":
                continue
            for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if POLISH_CHARS.search(line):
                    offenders.append(f"{path.name}:{no}: {line.strip()}")
        self.assertEqual(offenders, [])


class TestCliLanguages(LanguageCase):
    def run_cli(self, *argv):
        b, _ns = per_app()
        fake = FakePw(b)
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"DISCORD_DZWIEK_CONFIG_DIR": d + "/cfg",
                                             "DISCORD_DZWIEK_RUNTIME_DIR": d + "/run"}), \
                mock.patch.object(cli, "PwCli", lambda: fake), \
                mock.patch.object(cli, "setup_logging", lambda *a, **k: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_list_in_english_by_default(self):
        rc, out, _err = self.run_cli("--list")
        self.assertEqual(rc, 0)
        self.assertIn("Discord Go Live audio: active", out)
        self.assertIn("Apps playing audio (name for --apply):", out)
        self.assertIn("reasons: named discord_capture/game capture", out)
        self.assertIsNone(POLISH_CHARS.search(out), out)

    def test_list_in_polish(self):
        i18n.set_language("pl")
        rc, out, _err = self.run_cli("--list")
        self.assertEqual(rc, 0)
        self.assertIn("Stream z dźwiękiem: wykryty", out)
        self.assertIn("Aplikacje z dźwiękiem (nazwa do --apply):", out)
        self.assertIn("powody: nazwa discord_capture/game capture", out)
        self.assertIn("2 strumienie discord_capture", out)

    def test_json_keeps_stable_reason_ids(self):
        i18n.set_language("pl")
        rc, out, _err = self.run_cli("--list", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["status_text"], "Stream z dźwiękiem: wykryty")
        reasons = {r for c in data["candidates"] for r in c["reasons"]}
        self.assertIn("named discord_capture/game capture", reasons)

    def test_help_follows_warpipe_lang(self):
        launcher = os.path.join(ROOT, "bin", "warpipe")
        env = dict(os.environ, WARPIPE_LANG="en")
        en = subprocess.run([sys.executable, launcher, "--help"], capture_output=True, text=True,
                            env=env, timeout=20)
        self.assertEqual(en.returncode, 0, en.stderr)
        self.assertIn("show Discord capture streams and apps playing audio", en.stdout)
        env["WARPIPE_LANG"] = "pl"
        pl = subprocess.run([sys.executable, launcher, "--help"], capture_output=True, text=True,
                            env=env, timeout=20)
        self.assertEqual(pl.returncode, 0, pl.stderr)
        self.assertIn("pokaż strumienie Discorda i aplikacje z dźwiękiem", pl.stdout)


if __name__ == "__main__":
    unittest.main()
