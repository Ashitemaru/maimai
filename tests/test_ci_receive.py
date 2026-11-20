import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.ci_receive import receive
from deploy.deploy_release import DeployError

SHA = "a" * 40


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.downloads = Path(self.temp.name) / "downloads"

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_is_named_by_digest_and_replaces_stale_partials(self):
        self.downloads.mkdir()
        stale = self.downloads / f"mai-journal-{SHA}.tar.gz.{'0' * 64}.part"
        stale.write_bytes(b"old")
        other = self.downloads / f"mai-journal-{'b' * 40}.tar.gz"
        other.write_bytes(b"other")
        body = b"archive bytes"
        target, size = receive(io.BytesIO(SHA.encode() + b"\n" + body), self.downloads)
        digest = hashlib.sha256(body).hexdigest()
        self.assertEqual(target.name, f"mai-journal-{SHA}.tar.gz.{digest}.part")
        self.assertEqual((target.read_bytes(), size), (body, len(body)))
        self.assertFalse(stale.exists())
        self.assertTrue(other.exists())

    def test_invalid_header_or_oversized_body_leaves_no_file(self):
        for header in (b"not-a-sha\n", b"A" * 40 + b"\n", b"\xff\n"):
            with self.subTest(header=header), self.assertRaises(DeployError):
                receive(io.BytesIO(header + b"x"), self.downloads)
        with patch("deploy.ci_receive.MAX_ARCHIVE", 4), self.assertRaises(DeployError):
            receive(io.BytesIO(SHA.encode() + b"\n" + b"12345"), self.downloads)
        self.assertEqual(list(self.downloads.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
