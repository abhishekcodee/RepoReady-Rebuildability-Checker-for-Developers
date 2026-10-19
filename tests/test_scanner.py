import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch
SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path: sys.path.insert(0, str(SOURCE_ROOT))
from repoready.scanner import InvalidTargetError, scan_project

class ScannerTests(unittest.TestCase):
    def test_invalid_and_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(scan_project(d).files, ())
            with self.assertRaises(InvalidTargetError): scan_project(pathlib.Path(d) / "missing")
            p=pathlib.Path(d)/"file"; p.write_text("x")
            with self.assertRaises(InvalidTargetError): scan_project(p)

    def test_text_spaces_skips_and_sensitive(self):
        with tempfile.TemporaryDirectory(prefix="repo ready ") as d:
            root=pathlib.Path(d); (root/"src").mkdir(); (root/"src"/"a b.py").write_text("print('x')")
            (root/"src"/"café ☃.txt").write_text("unicode", encoding="utf-8")
            (root/"build").mkdir(); (root/"build"/"generated.py").write_text("bad")
            (root/".env").write_text("SECRET=x")
            snap=scan_project(root)
            self.assertEqual(snap.get("src/a b.py").text, "print('x')")
            self.assertEqual(snap.get("src/café ☃.txt").text, "unicode")
            self.assertIn(pathlib.Path("build"), snap.skipped_paths)
            self.assertIsNone(snap.get(".env").text)

    def test_generated_directories_are_skipped_case_insensitively(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            for name in ("Build", ".CACHE", "NODE_MODULES", "__PYCACHE__"):
                directory = root / name
                directory.mkdir()
                (directory / "ignored.py").write_text("not loaded", encoding="utf-8")

            snapshot = scan_project(root)

            self.assertEqual(snapshot.files, ())
            self.assertEqual(
                {path.name.casefold() for path in snapshot.skipped_paths},
                {"build", ".cache", "node_modules", "__pycache__"},
            )

    def test_size_limit_preserves_safe_metadata_without_reading_content(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            large = root / "large.txt"
            large.write_text("0123456789", encoding="utf-8")

            snapshot = scan_project(root, max_file_size=4)
            entry = snapshot.get("large.txt")

            self.assertIsNotNone(entry)
            self.assertFalse(entry.is_text)
            self.assertIsNone(entry.text)
            self.assertIn("size limit", entry.error or "")

    def test_read_errors_are_recorded_without_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            target = root / "unreadable.txt"
            target.write_text("content", encoding="utf-8")

            with patch.object(pathlib.Path, "read_bytes", side_effect=PermissionError("denied")):
                snapshot = scan_project(root)

            entry = snapshot.get("unreadable.txt")
            self.assertIsNotNone(entry)
            self.assertFalse(entry.is_text)
            self.assertIsNone(entry.text)
            self.assertIn("read failed", entry.error or "")
            self.assertTrue(snapshot.scan_errors)

    def test_private_key_like_files_are_not_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            key = root / "private_key.pem"
            key.write_text("PRIVATE KEY MATERIAL", encoding="utf-8")

            snapshot = scan_project(root)
            entry = snapshot.get("private_key.pem")

            self.assertIsNotNone(entry)
            self.assertIsNone(entry.text)
            self.assertIn("sensitive", entry.error or "")

    def test_symlink_is_not_followed_when_supported(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d); outside=pathlib.Path(d).parent/"rr-outside"; outside.mkdir(exist_ok=True)
            (outside/"secret.txt").write_text("secret")
            link=root/"link"
            try: link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError): return
            snap=scan_project(root); self.assertIn(pathlib.Path("link"), snap.skipped_paths)

    def test_broken_symlink_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            link = root / "broken-link"
            try:
                target = root / "removed-target"
                target.mkdir()
                link.symlink_to(target, target_is_directory=True)
                target.rmdir()
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")

            snapshot = scan_project(root)

            self.assertIn(pathlib.Path("broken-link"), snapshot.skipped_paths)
            self.assertEqual(snapshot.files, ())

    def test_symlink_target_itself_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            target = root / "actual"
            target.mkdir()
            link = root / "project-link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")

            with self.assertRaises(InvalidTargetError):
                scan_project(link)

    def test_deep_tree_does_not_use_python_recursion(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            current = root
            try:
                for _ in range(1100):
                    current /= "d"
                    current.mkdir()
            except OSError:
                self.skipTest("platform path limits prevent a recursion-depth fixture")
            (current / "leaf.txt").write_text("leaf", encoding="utf-8")

            snapshot = scan_project(root)

            self.assertEqual(len(snapshot.files), 1)
            self.assertEqual(snapshot.files[0].text, "leaf")

    def test_hardlinks_are_returned_but_read_once_when_supported(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            original = root / "original.txt"
            alias = root / "alias.txt"
            original.write_text("same inode", encoding="utf-8")
            try:
                os.link(original, alias)
            except (OSError, NotImplementedError):
                self.skipTest("hardlinks are unavailable")

            original_read_bytes = pathlib.Path.read_bytes
            with patch.object(pathlib.Path, "read_bytes", autospec=True,
                              side_effect=original_read_bytes) as read_bytes:
                snapshot = scan_project(root)

            self.assertEqual(read_bytes.call_count, 1)
            self.assertEqual(snapshot.text("original.txt"), "same inode")
            self.assertEqual(snapshot.text("alias.txt"), "same inode")

    def test_sensitive_hardlink_is_not_loaded_through_public_alias(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            sensitive = root / ".env"
            alias = root / "settings.txt"
            sensitive.write_text("API_KEY=hardlink-secret", encoding="utf-8")
            try:
                os.link(sensitive, alias)
            except (OSError, NotImplementedError):
                self.skipTest("hardlinks are unavailable")

            snapshot = scan_project(root)

            self.assertIsNone(snapshot.text(".env"))
            self.assertIsNone(snapshot.text("settings.txt"))
            self.assertNotIn("hardlink-secret", repr(snapshot))

    def test_scandir_failures_are_sanitized(self):
        with tempfile.TemporaryDirectory() as d:
            secret = "CONTENTS_SHOULD_NOT_LEAK"
            with patch("repoready.scanner.os.scandir",
                       side_effect=OSError(secret)):
                snapshot = scan_project(d)

            self.assertTrue(snapshot.scan_errors)
            self.assertNotIn(secret, repr(snapshot))
            self.assertTrue(all("OSError" in error for error in snapshot.scan_errors))

    def test_stat_failures_are_recorded_without_exception_text(self):
        class FakeEntry:
            name = "unicode-☃.txt"

            def is_symlink(self):
                return False

            def is_dir(self, follow_symlinks=False):
                return False

            def is_file(self, follow_symlinks=False):
                return True

            def stat(self, follow_symlinks=False):
                raise OSError("STAT_SECRET_VALUE")

        class FakeScandir:
            def __enter__(self):
                return iter((FakeEntry(),))

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as d:
            with patch("repoready.scanner.os.scandir", return_value=FakeScandir()):
                snapshot = scan_project(d)

        self.assertEqual(snapshot.files[0].relative_path.name, "unicode-☃.txt")
        self.assertIn("stat failed: OSError", snapshot.files[0].error or "")
        self.assertNotIn("STAT_SECRET_VALUE", repr(snapshot))

    def test_read_failure_does_not_include_exception_text_or_content(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            target = root / "public.txt"
            target.write_text("VALUE_MUST_NOT_BE_EXPOSED", encoding="utf-8")

            with patch.object(pathlib.Path, "read_bytes",
                              side_effect=PermissionError("VALUE_MUST_NOT_BE_EXPOSED")):
                snapshot = scan_project(root)

            self.assertNotIn("VALUE_MUST_NOT_BE_EXPOSED", repr(snapshot))
            self.assertIn("read failed: PermissionError", snapshot.files[0].error or "")
            self.assertIn("PermissionError", snapshot.scan_errors[0])

    def test_sensitive_content_is_absent_from_snapshot_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            sensitive_value = "API_KEY=DO_NOT_PUBLISH_123"
            (root / ".env.production").write_text(sensitive_value, encoding="utf-8")

            snapshot = scan_project(root)

            self.assertNotIn(sensitive_value, repr(snapshot))
            self.assertNotIn(sensitive_value, snapshot.scan_errors)

if __name__ == '__main__': unittest.main()
