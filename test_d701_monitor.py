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
    def test_watchdog_silent_connection_and_diagnostics(self):
        import queue
        import socket
        import threading
        from unittest.mock import patch
        from d701_monitor import SensorWorker, new_sensor
        with tempfile.TemporaryDirectory() as temp:
            events = queue.Queue(); stop = threading.Event()
            sensor = new_sensor("TEST", "127.0.0.1")
            sensor.update(data_warning_seconds=1, data_reconnect_seconds=2)
            worker = SensorWorker(sensor, temp, events, stop)
            class SilentSocket:
                def setsockopt(self, *args): pass
                def ioctl(self, *args): pass
                def settimeout(self, *args): pass
                def recv(self, *args): raise socket.timeout()
                def shutdown(self, *args): pass
                def close(self): pass
            def finish_wait(delay):
                if worker.health["reconnects"] >= 2:
                    stop.set(); return True
                return False
            with patch("d701_monitor.socket.create_connection", return_value=SilentSocket()) as connect, \
                 patch("d701_monitor.time.monotonic", side_effect=[0, 1, 2, 3, 4, 5]), \
                 patch.object(stop, "wait", side_effect=finish_wait):
                worker.run()
            self.assertEqual(connect.call_count, 2)
            self.assertEqual(worker.health["reconnects"], 2)
            records = list(events.queue)
            self.assertTrue(any("tanpa byte" in str(r[2]) for r in records))
            self.assertTrue(any("watchdog" in str(r[2]) for r in records))
            self.assertFalse(worker.health["connected"])
            logs = list(Path(temp).rglob("*_diagnostics.jsonl"))
            self.assertEqual(len(logs), 1)
            self.assertIn("watchdog", logs[0].read_text(encoding="utf-8"))

    def test_incoming_bytes_without_valid_samples_do_not_reconnect(self):
        import queue
        import threading
        from d701_monitor import SensorWorker, new_sensor
        with tempfile.TemporaryDirectory() as temp:
            worker = SensorWorker(new_sensor(), temp, queue.Queue(), threading.Event())
            worker.byte_tick = 119; worker.valid_tick = 0; worker.session_valid = False
            worker.check_health(120)
            self.assertIn("belum ada sampel valid", list(worker.events.queue)[-1][2])
            worker.publish_health(b"partial")
            self.assertEqual(worker.health["buffer_bytes"], 7)

    def test_application_version(self):
        from d701_monitor import APP_VERSION
        self.assertEqual(APP_VERSION, "1.7.0")

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
    def test_metadata_defaults_and_legacy_config_migration(self):
        from d701_monitor import default_metadata, normalize_config
        fields = default_metadata()
        for key in ("code", "latitude", "manufacturer", "n_samp", "output_unit",
                    "logger_model", "timestamp_source", "time_sync"):
            self.assertIn(key, fields)
        legacy = {"sensors": [{"station": "OLD", "host": "127.0.0.1", "port": 4001}]}
        migrated = normalize_config(legacy)["sensors"][0]
        self.assertEqual(migrated["metadata"]["datum"], "WGS84")
        self.assertEqual(migrated["metadata"]["output_unit"], "microradian")

    def test_metadata_snapshot_and_history_log(self):
        from d701_monitor import new_sensor, write_metadata_snapshot
        with tempfile.TemporaryDirectory() as temp:
            sensor = new_sensor("TEST STATION", "127.0.0.1")
            sensor["metadata"].update(code="TST-01", positive_direction="+X menuju puncak",
                                      updated_at_utc="2026-09-17T00:00:00.000Z")
            current, history = write_metadata_snapshot(temp, sensor)
            self.assertTrue(current.exists())
            self.assertTrue(history.exists())
            self.assertEqual(current.name, "TEST_STATION_metadata.txt")
            self.assertEqual(history.name, "TEST_STATION_metadata_history.log")
            self.assertIn("Kode stasiun: TST-01", current.read_text(encoding="utf-8"))
            write_metadata_snapshot(temp, sensor)
            self.assertEqual(history.read_text(encoding="utf-8").count("TILTY STATION METADATA"), 2)

    def test_average(self):
        agg = MinuteAggregator(); t = datetime(2026, 9, 8, 1, 2, 1, tzinfo=timezone.utc)
        agg.add(parse_d701_line("$ 1,2,20,N", "A", "host", t, factor_x=1, factor_y=1))
        agg.add(parse_d701_line("$ 3,4,24,N", "A", "host", t, factor_x=1, factor_y=1))
        result, count = agg.finish()
        self.assertEqual((result.x, result.y, result.temperature, count), (2, 3, 22, 2))

if __name__ == "__main__": unittest.main()
