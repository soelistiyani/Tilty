import csv
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from d701_monitor import (DataWriter, MinuteAggregator, TimeSettingsRecorder,
                          apply_calibration, downsample_rows, import_legacy_csv, parse_d701_line,
                          query_export_rows, query_history)

class TestD701Monitor(unittest.TestCase):
    def test_conversion(self):
        at = datetime(2026, 9, 8, 1, 2, 3, tzinfo=timezone.utc)
        r = parse_d701_line("$ 0.0076,-0.0623,26.34,N2343", "A", "host", at)
        self.assertAlmostEqual(r.x, math.radians(.0076)*1e6)
        self.assertAlmostEqual(r.y, math.radians(-.0623)*1e6)
    def test_calibration(self):
        self.assertAlmostEqual(apply_calibration(1.2, .2, 10), 10)
    def test_invalid(self):
        self.assertIsNone(parse_d701_line("garbage", "A", "host"))
    def test_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            at = datetime(2026, 9, 8, 23, 59, tzinfo=timezone.utc)
            r = parse_d701_line("$ 0.1,-0.2,25.3,N", "TILT A", "host", at)
            path = DataWriter(Path(temp)).write_minute(r, 60)
            self.assertEqual(path.name, "2026-09-08_TILT_A.csv")
            with path.open(encoding="utf-8-sig", newline="") as f: rows = list(csv.reader(f))
            self.assertEqual(rows[0][2:4], ["x_urad", "y_urad"])
            self.assertEqual(rows[1][-1], "60")
            history = query_history(temp, "TILT_A", at.replace(hour=0))
            # Writer falls back to the safe station name when old readings have no UUID.
            self.assertEqual(len(history), 1)
            self.assertAlmostEqual(history[0][1], r.x)
    def test_downsample_is_bounded(self):
        rows = [(i, float(i), float(i), float(i)) for i in range(100)]
        reduced = downsample_rows(rows, 10)
        self.assertLessEqual(len(reduced), 10)
        self.assertAlmostEqual(reduced[0][1], 4.5)
    def test_pc_time_setting_daily_and_change_records(self):
        base = {"observed_at_utc": "2026-09-08T00:00:00.000Z", "local_date": "2026-09-08",
                "local_time": "2026-09-08T07:00:00+07:00", "windows_timezone": "SE Asia Standard Time",
                "timezone_name": "+07", "utc_offset": "UTC+07:00", "dst_active": "no"}
        with tempfile.TemporaryDirectory() as temp:
            recorder = TimeSettingsRecorder(temp)
            self.assertTrue(recorder.record_if_needed(base))
            self.assertFalse(recorder.record_if_needed(base.copy()))
            changed = base.copy(); changed.update(observed_at_utc="2026-09-08T01:00:00.000Z",
                                                   local_time="2026-09-08T01:00:00+00:00",
                                                   windows_timezone="UTC", timezone_name="UTC",
                                                   utc_offset="UTC+00:00")
            self.assertTrue(recorder.record_if_needed(changed))
            next_day = changed.copy(); next_day.update(observed_at_utc="2026-09-09T01:00:00.000Z",
                                                       local_date="2026-09-09",
                                                       local_time="2026-09-09T01:00:00+00:00")
            self.assertTrue(recorder.record_if_needed(next_day))
            with recorder.path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["utc_offset"], "UTC+07:00")
    def test_import_legacy_csv_ignores_duplicates(self):
        headers = ("timestamp_utc", "station", "x_urad", "y_urad", "temperature_c", "status", "sample_count")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "old"; source.mkdir()
            with (source / "old.csv").open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers); writer.writeheader()
                writer.writerow(dict(zip(headers, ("2026-01-01T00:00:00.000Z", "STA", 1, 2, 38.99, "N", 60))))
            sensors = [{"id": "station-id", "station": "STA"}]
            first = import_legacy_csv(root / "data", source, sensors)
            second = import_legacy_csv(root / "data", source, sensors)
            rows = query_export_rows(root / "data", "station-id", datetime(2025, 1, 1, tzinfo=timezone.utc))
            self.assertEqual((first["imported"], second["duplicates"], len(rows)), (1, 1, 1))
            self.assertAlmostEqual(rows[0][4], 38.99)
    def test_average(self):
        agg = MinuteAggregator(); t = datetime(2026, 9, 8, 1, 2, 1, tzinfo=timezone.utc)
        agg.add(parse_d701_line("$ 1,2,20,N", "A", "host", t, factor_x=1, factor_y=1))
        agg.add(parse_d701_line("$ 3,4,24,N", "A", "host", t, factor_x=1, factor_y=1))
        result, count = agg.finish()
        self.assertEqual((result.x, result.y, result.temperature, count), (2, 3, 22, 2))

if __name__ == "__main__": unittest.main()
