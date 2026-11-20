import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.ci_receive import cached_digests, receive
from deploy.deploy_release import DeployError

SHA = "a" * 40


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_upload_replaces_any_previous_pending_archive(self):
        incoming = self.root / "incoming"
        incoming.mkdir()
        (incoming / f"mai-journal-{'b' * 40}.tar.gz").write_bytes(b"older")
        target, size = receive(io.BytesIO(SHA.encode() + b"\narchive"), self.root)
        self.assertEqual(target, incoming / f"mai-journal-{SHA}.tar.gz")
        self.assertEqual((target.read_bytes(), size), (b"archive", 7))
        self.assertEqual(list(incoming.iterdir()), [target])

    def test_invalid_header_or_oversized_body_leaves_no_file(self):
        for header in (b"not-a-sha\n", b"A" * 40 + b"\n", b"\xff\n"):
            with self.subTest(header=header), self.assertRaises(DeployError):
                receive(io.BytesIO(header + b"x"), self.root)
        with patch("deploy.ci_receive.MAX_ARCHIVE", 4), self.assertRaises(DeployError):
            receive(io.BytesIO(SHA.encode() + b"\n12345"), self.root)
        self.assertEqual(list((self.root / "incoming").iterdir()), [])

    def test_lists_cached_wheel_digests(self):
        self.assertEqual(cached_digests(self.root), [])
        (self.root / "wheels").mkdir()
        (self.root / "wheels" / f"{'1' * 64}.whl").write_bytes(b"")
        (self.root / "wheels" / ".partial").write_bytes(b"")
        self.assertEqual(cached_digests(self.root), ["1" * 64])


if __name__ == "__main__":
    unittest.main()
