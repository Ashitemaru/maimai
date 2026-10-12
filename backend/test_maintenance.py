import unittest
from datetime import datetime

from backend.maintenance import MAINTENANCE_MESSAGE, maintenance_status


class MaintenanceTests(unittest.TestCase):
    def test_shanghai_daily_boundaries_are_inclusive_start_exclusive_end(self):
        for value, active, delay in (
            ("2026-10-13T03:59:59.999999+08:00", False, 0),
            ("2026-10-13T04:00:00+08:00", True, 10800),
            ("2026-10-13T06:59:59.999999+08:00", True, 1),
            ("2026-10-13T07:00:00+08:00", False, 0),
            ("2026-10-12T20:00:00+00:00", True, 10800),
            ("2026-10-12T23:00:00+00:00", False, 0),
            ("2026-10-12T15:00:00-05:00", True, 10800),
            ("2026-10-14T04:00:00+08:00", True, 10800),
        ):
            with self.subTest(at=value):
                result = maintenance_status(datetime.fromisoformat(value))
                self.assertEqual(result["active"], active)
                self.assertEqual(result["retry_after_seconds"], delay)
                self.assertEqual(result["timezone"], "Asia/Shanghai")
                self.assertEqual(result["start_time"], "04:00")
                self.assertEqual(result["end_time"], "07:00")
                self.assertEqual(result["message"], MAINTENANCE_MESSAGE if active else None)
                if active:
                    retry_at = datetime.fromisoformat(result["retry_at"])
                    self.assertEqual((retry_at.hour, retry_at.minute), (7, 0))
                else:
                    self.assertIsNone(result["retry_at"])

    def test_timezone_is_required_instead_of_using_host_local_time(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            maintenance_status(datetime(2026, 10, 13, 4))


if __name__ == "__main__":
    unittest.main()
