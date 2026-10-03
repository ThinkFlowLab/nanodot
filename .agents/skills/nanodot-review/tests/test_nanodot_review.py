"""Offline structural, routing and packaging checks for nanodot review."""

import contextlib
import importlib.util
import io
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT
spec = importlib.util.spec_from_file_location("review_checks", SKILL / "scripts/review_checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)
SHA = "a" * 40
DIFF = """diff --git a/src/nanodot/core/runner.py b/src/nanodot/core/runner.py
--- a/src/nanodot/core/runner.py
+++ b/src/nanodot/core/runner.py
@@ -8,2 +8,2 @@
 keep()
-old()
+new()
"""


class NanodotReviewTests(unittest.TestCase):
    def finding(self, **overrides):
        result = dict(head_sha=SHA, severity="P2", path="src/nanodot/core/runner.py",
                      line=9, side="RIGHT", trigger="restart after delivery",
                      consequence="duplicate delivery", evidence="replay changes identity",
                      correction="preserve durable occurrence identity")
        result.update(overrides)
        return result

    def valid(self, finding):
        with contextlib.redirect_stderr(io.StringIO()):
            return checks.validate_finding(finding, SHA, DIFF)

    def test_finding_requires_exact_head_and_changed_coordinate(self):
        self.assertTrue(self.valid(self.finding()))
        for override in ({"head_sha": "b" * 40}, {"line": 11}, {"line": True},
                         {"side": "UNKNOWN"}, {"path": "src/nanodot/missing.py"},
                         {"start_line": 7}, {"start_side": "LEFT"}):
            with self.subTest(override=override):
                self.assertFalse(self.valid(self.finding(**override)))

    def test_grounding_and_severity_are_explicit_but_not_truth_proofs(self):
        for field in ("trigger", "consequence", "evidence", "correction"):
            self.assertFalse(self.valid(self.finding(**{field: " "})))
        self.assertFalse(self.valid(self.finding(severity="blocking")))
        self.assertFalse(self.valid(None))
        for severity in ("P0", "P1", "P2", "P3"):
            self.assertTrue(self.valid(self.finding(severity=severity)))

    def test_mixed_routes_retain_seams_without_omni_assumptions(self):
        result = checks.routes(["src/nanodot/core/tasks.py", "src/nanodot/ports/github.py",
                                "src/nanodot/core/github_eval.py", "tests/test_runner.py"])
        self.assertEqual(result, ["contracts", "lifecycle", "persistence-delivery",
                                  "tests-packaging", "watch-state"])
        self.assertEqual(checks.routes(["docs/design.md"]), ["docs-contracts"])
        self.assertEqual(checks.routes(["unrecognized.txt"]), ["inspect-context"])

    def test_unmapped_source_never_silently_loses_review_routing(self):
        self.assertEqual(checks.routes(["src/nanodot/new_feature.py"]), ["inspect-context"])
        self.assertEqual(checks.routes(["src/nanodot/core/tasks.py", "src/nanodot/new_feature.py"]),
                         ["inspect-context", "lifecycle", "persistence-delivery"])
        self.assertEqual(checks.routes(["src/nanodot/native/runner_control.py"]), ["lifecycle"])
        self.assertEqual(checks.routes(["src/nanodot/native/secrets_file.py"]), ["permissions-egress"])
        self.assertEqual(checks.routes(["src/nanodot/core/config.py"]), ["memory-inference"])
        self.assertEqual(checks.routes(["src/nanodot/paths.py"]), ["persistence-delivery"])

    def test_skill_frontmatter_and_ui_metadata(self):
        text = (SKILL / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: nanodot-review\ndescription: "))
        self.assertIn("ThinkFlowLab/nanodot", text.split("---", 2)[1])
        ui = (SKILL / "agents/openai.yaml").read_text()
        self.assertIn("$nanodot-review", ui)
        self.assertNotIn("allow_implicit_invocation: false", ui)

    def test_local_markdown_links_resolve_and_stay_inside_bundle(self):
        for path in SKILL.rglob("*.md"):
            for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
                if "://" in link or link.startswith("#"):
                    continue
                target = (path.parent / link.split("#", 1)[0]).resolve()
                self.assertIn(SKILL.resolve(), [target, *target.parents], str(target))
                self.assertTrue(target.exists(), str(target))

    def test_bundle_is_self_contained_when_copied(self):
        with tempfile.TemporaryDirectory() as target:
            bundle = Path(target) / "nanodot-review"
            shutil.copytree(SKILL, bundle, ignore=shutil.ignore_patterns("__pycache__"))
            for source in SKILL.rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts:
                    self.assertEqual(source.read_bytes(), (bundle / source.relative_to(SKILL)).read_bytes())
            spec = importlib.util.spec_from_file_location("copied_review_checks", bundle / "scripts/review_checks.py")
            copied = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(copied)
            self.assertEqual(copied.routes(["src/nanodot/core/runner.py"]), checks.routes(["src/nanodot/core/runner.py"]))


if __name__ == "__main__":
    unittest.main()
