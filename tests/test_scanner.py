import pathlib, sys, tempfile, unittest
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
            (root/"build").mkdir(); (root/"build"/"generated.py").write_text("bad")
            (root/".env").write_text("SECRET=x")
            snap=scan_project(root)
            self.assertEqual(snap.get("src/a b.py").text, "print('x')")
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

if __name__ == '__main__': unittest.main()
