#!/usr/bin/env python3
"""Run all Python tests offline with only generated, temporary application data."""

import argparse
import importlib
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def reject_network(*args, **kwargs):
    raise AssertionError(
        "Network access is disabled in Python tests; inject a fake transport/fetcher"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", help="Optional unittest module/class/method names")
    args = parser.parse_args()
    with ExitStack() as stack:
        for target in (
            "socket.socket.connect",
            "socket.socket.connect_ex",
            "socket.socket.sendto",
            "socket.create_connection",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.gethostbyaddr",
        ):
            stack.enter_context(patch(target, side_effect=reject_network))
        importlib.import_module("tests.support")  # Isolate app before test discovery.
        loader = unittest.TestLoader()
        suite = (
            loader.loadTestsFromNames(args.tests)
            if args.tests
            else unittest.TestSuite(
                [
                    loader.discover(
                        str(REPO / "backend"), pattern="test_*.py", top_level_dir=str(REPO)
                    ),
                    loader.discover(
                        str(REPO / "tests"), pattern="test_*.py", top_level_dir=str(REPO)
                    ),
                ]
            )
        )
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
