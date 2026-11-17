"""The files Claude Code reads to install the plugin must agree with each other and with the code they point at."""

import glob
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memtest_helpers as th  # noqa: E402

ROOT = os.path.dirname(th.SCRIPTS)
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PLUGIN_ROOT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\s\"']+)")


def load_json(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def version_tuple(text):
    return tuple(int(part) for part in SEMVER.match(text).groups())


def frontmatter(path):
    """(dict of top-level 'key: value' lines, body) for a SKILL.md with a simple frontmatter block."""
    text = th.read(path)
    assert text.startswith("---\n"), "SKILL.md must start with a frontmatter block"
    head, _sep, body = text[4:].partition("\n---\n")
    pairs = {}
    for line in head.split("\n"):
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            pairs[key.strip()] = value.strip()
    return pairs, body


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_json(".claude-plugin", "plugin.json")
        self.market = load_json(".claude-plugin", "marketplace.json")

    def test_the_version_is_semver(self):
        self.assertRegex(self.plugin["version"], SEMVER)

    def test_the_marketplace_lists_exactly_this_plugin_under_the_same_name(self):
        self.assertEqual(
            [p["name"] for p in self.market["plugins"]], [self.plugin["name"]]
        )
        self.assertEqual(self.market["name"], self.plugin["name"])

    def test_the_entry_points_at_the_repository_root_and_carries_no_second_version(
        self,
    ):
        entry = self.market["plugins"][0]
        self.assertEqual(entry["source"], "./")
        self.assertNotIn(
            "version", entry
        )  # Claude Code uses plugin.json's and validate flags a second one

    def test_the_names_are_valid_and_not_reserved_prefixes(self):
        self.assertRegex(self.plugin["name"], r"^[a-z][a-z0-9-]*$")
        self.assertFalse(
            self.plugin["name"].startswith(
                ("claude-", "anthropic-", "anthropics-", "cc-plugin-")
            )
        )

    def test_the_repository_url_names_this_plugin(self):
        self.assertTrue(self.plugin["repository"].endswith("/" + self.plugin["name"]))

    def test_the_license_is_apache(self):
        self.assertEqual(self.plugin["license"], "Apache-2.0")
        self.assertIn("Apache License", th.read(os.path.join(ROOT, "LICENSE"))[:400])


class HooksTests(unittest.TestCase):
    def setUp(self):
        self.hooks = load_json("hooks", "hooks.json")

    def commands(self):
        for blocks in self.hooks["hooks"].values():
            for block in blocks:
                for handler in block["hooks"]:
                    yield handler

    def test_every_hook_runs_a_script_that_exists_in_exec_form(self):
        seen = 0
        for handler in self.commands():
            self.assertEqual(handler["type"], "command")
            self.assertEqual(handler["command"], "python3")
            targets = [
                PLUGIN_ROOT_REF.match(arg).group(1)
                for arg in handler["args"]
                if PLUGIN_ROOT_REF.match(arg)
            ]
            self.assertEqual(len(targets), 1, handler)
            self.assertTrue(os.path.isfile(os.path.join(ROOT, targets[0])), targets[0])
            seen += 1
        self.assertGreaterEqual(seen, 2)

    def test_every_hook_has_a_short_timeout(self):
        for handler in self.commands():
            self.assertLessEqual(handler["timeout"], 5)


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_json(".claude-plugin", "plugin.json")
        self.skills = sorted(glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md")))

    def test_there_is_one_skill_named_after_its_folder(self):
        self.assertEqual(len(self.skills), 1)
        pairs, _body = frontmatter(self.skills[0])
        self.assertEqual(
            pairs["name"], os.path.basename(os.path.dirname(self.skills[0]))
        )

    def test_the_frontmatter_is_safe_yaml_and_the_skill_waits_for_the_person(self):
        pairs, _body = frontmatter(self.skills[0])
        self.assertEqual(pairs["disable-model-invocation"], "true")
        for key in ("description", "compatibility"):
            value = pairs[key]
            self.assertTrue(
                value.startswith('"') and value.endswith('"') and len(value) > 2, key
            )
            self.assertNotIn(
                '"', value[1:-1].replace('\\"', ""), key
            )  # no unescaped quote inside the quoted string

    def test_the_skill_scripts_exist_and_the_doc_names_the_skill_by_its_plugin_prefix(
        self,
    ):
        _pairs, body = frontmatter(self.skills[0])
        for script in set(
            re.findall(r"`((?:recall/|dream/|doctor/)?memory_[a-z_]+\.py)", body)
        ):
            self.assertTrue(
                os.path.isfile(os.path.join(ROOT, "engine", script)), script
            )
        invocation = "/%s:%s" % (
            self.plugin["name"],
            os.path.basename(os.path.dirname(self.skills[0])),
        )
        self.assertIn(invocation, th.read(os.path.join(ROOT, "README.md")))


class SkillTextHazardTests(unittest.TestCase):
    """Claude Code expands $1, $2 ... and $ARGUMENTS inside a skill before the model reads it. The first tester saw dollar figures
    turn into stray words ("about person", "You.33"), so a skill text must never contain one."""

    def test_the_skill_has_no_dollar_digit_or_arguments_placeholder(self):
        text = th.read(os.path.join(ROOT, "skills", "tend", "SKILL.md"))
        self.assertEqual(re.findall(r"\$(?:\d|ARGUMENTS)", text), [])

    def test_every_script_the_skill_names_is_written_with_the_folder_it_really_lives_in(self):
        text = th.read(os.path.join(ROOT, "skills", "tend", "SKILL.md"))
        for name in set(re.findall(r"`((?:recall/|dream/|doctor/)?memory_[a-z_]+\.py)", text)):
            folder, _sep, script = name.rpartition("/")
            expected = os.path.join(ROOT, "engine", folder, script)
            self.assertTrue(os.path.isfile(expected), name)
        self.assertNotIn("dream/memory_dream.py", text)  # memory_dream.py sits in engine/, not engine/dream/


class ChangelogAndReadmeTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_json(".claude-plugin", "plugin.json")
        self.changelog = th.read(os.path.join(ROOT, "CHANGELOG.md"))
        self.readme = th.read(os.path.join(ROOT, "README.md"))

    def test_the_changelog_keeps_an_unreleased_section(self):
        self.assertIn("\n## [Unreleased]\n", self.changelog)

    def test_no_changelog_version_is_newer_than_the_manifest(self):
        headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", self.changelog, re.M)
        for heading in headings:
            self.assertLessEqual(
                version_tuple(heading), version_tuple(self.plugin["version"]), heading
            )

    def test_a_released_manifest_version_has_its_changelog_section(self):
        headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", self.changelog, re.M)
        if (
            headings
        ):  # once any release is recorded, the current version must be one of them
            self.assertIn(self.plugin["version"], headings)

    def test_the_readme_install_commands_use_the_real_names(self):
        name = self.plugin["name"]
        self.assertIn("claude plugin install %s@%s" % (name, name), self.readme)
        self.assertRegex(
            self.readme,
            r"claude plugin marketplace add sairam0424/%s(#stable)?" % re.escape(name),
        )
        self.assertIn("claude plugin uninstall %s@%s" % (name, name), self.readme)

    def test_the_readme_says_it_is_independent_directly_under_the_title(self):
        lines = [ln for ln in self.readme.split("\n") if ln.strip()]
        self.assertEqual(lines[0], "# " + self.plugin["name"])
        self.assertIn("not made, endorsed or supported by Anthropic", lines[1])


class CommunityFilesTests(unittest.TestCase):
    def test_the_expected_files_exist(self):
        for rel in (
            "LICENSE",
            "NOTICE",
            "SECURITY.md",
            "SUPPORT.md",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "CHANGELOG.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/dependabot.yml",
        ):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)

    def test_the_bug_form_asks_for_the_doctor_and_warns_against_pasting_notes(self):
        text = th.read(
            os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "bug_report.yml")
        )
        self.assertIn("memory_doctor.py", text)
        self.assertIn("do not paste your notes", text.lower())

    def test_security_names_a_route_that_works_without_the_security_tab(self):
        text = th.read(os.path.join(ROOT, "SECURITY.md"))
        self.assertIn("security contact request", text)


STEP = re.compile(r"^(\d)\. \*\*(.+?)\*\*", re.M)
STEP_WORDS = {  # one step number must mean the same step in the README and in the skill
    0: ("check", "doctor"),
    1: ("safety",),
    2: ("evidence",),
    3: ("baseline",),
    4: ("proposals", "lanes"),
    5: ("recall check",),
    6: ("report", "show the user"),
    7: ("promote",),
    8: ("undo",),
}


class ReleaseDocsTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_json(".claude-plugin", "plugin.json")
        self.changelog = th.read(os.path.join(ROOT, "CHANGELOG.md"))
        self.readme = th.read(os.path.join(ROOT, "README.md"))
        self.skill = th.read(os.path.join(ROOT, "skills", "tend", "SKILL.md"))

    def test_the_manifest_version_always_has_its_changelog_section(self):
        headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", self.changelog, re.M)
        self.assertIn(self.plugin["version"], headings)

    def test_the_marketplace_entry_carries_no_second_version(self):
        for entry in load_json(".claude-plugin", "marketplace.json")["plugins"]:
            self.assertNotIn("version", entry)

    def test_a_step_number_means_the_same_step_in_the_readme_and_the_skill(self):
        for name, text in (("README", self.readme), ("SKILL", self.skill)):
            found = {int(num): label.lower() for num, label in STEP.findall(text)}
            self.assertEqual(sorted(found), sorted(STEP_WORDS), name)
            for number, label in found.items():
                self.assertTrue(any(word in label for word in STEP_WORDS[number]), "%s step %d is %r" % (name, number, label))

    def test_the_documented_commands_keep_bytecode_out_of_the_plugin_folder(self):
        self.assertIn('python3 -B -I "${CLAUDE_PLUGIN_ROOT}/engine/<script>"', self.skill)
        self.assertIn("python3 -B -I ~/.claude/plugins/cache/", self.readme)

    def test_the_safety_net_step_lints_before_it_snapshots(self):
        step = re.search(r"^1\. \*\*Safety net\.\*\*.*$", self.skill, re.M).group(0)
        self.assertLess(step.index("lint --no-log"), step.index("memory_hygiene.py snapshot"))

    def test_the_changelog_no_longer_says_the_hook_cannot_ask_in_bypass_mode(self):
        self.assertNotIn("cannot ask in bypass-permissions mode", self.changelog)

    def test_the_security_contact_form_asks_for_no_details_and_blank_issues_stay_off(self):
        form = th.read(os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "security_contact.yml"))
        self.assertIn('title: "security contact request"', form)
        self.assertIn("type: checkboxes", form)
        self.assertEqual(form.count("type: textarea"), 0)  # no free-text box for details of the problem
        self.assertIn("blank_issues_enabled: false", th.read(os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "config.yml")))
        self.assertIn("Security contact request", th.read(os.path.join(ROOT, "SECURITY.md")))


if __name__ == "__main__":
    unittest.main()
