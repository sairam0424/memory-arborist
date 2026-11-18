"""Found by a third verification pass: the doctor told the person to 'see Preconditions in the skill', but the skill has no such
section (its section is called 'Before step 3'). A pointer into the skill must name a heading that exists."""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.dirname(HERE))
import memory_doctor as doc  # noqa: E402

SKILL = os.path.join(os.path.dirname(ENGINE), "skills", "tend", "SKILL.md")


class SkillPointerTests(unittest.TestCase):
    def headings(self):
        with open(SKILL, encoding="utf-8") as fh:
            return [line[3:].strip() for line in fh if line.startswith("## ")]

    def test_every_pointer_into_the_skill_names_a_section_that_exists(self):
        pointers = re.findall(r"\bsee (.+?) in the skill", doc.QUESTIONS_FIX)
        self.assertTrue(pointers, "the doctor's questions hint no longer points into the skill; update this test with it")
        for pointer in pointers:
            name = pointer.strip("\"'")
            with self.subTest(name):
                self.assertTrue(any(h.startswith(name) for h in self.headings()), "no section of the skill starts with %r" % name)


if __name__ == "__main__":
    unittest.main()
