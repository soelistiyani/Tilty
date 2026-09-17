from __future__ import annotations

import csv
import json
import math
import os
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.dates import DateFormatter
from matplotlib.figure import Figure

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "Tilty"
APP_VERSION = "1.7.0"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
LEGACY_CONFIG_PATH = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "D701Monitor" / "config.json"
DEFAULT_DATA_DIR = Path.home() / "Documents" / "Tilty Data"
DEG_TO_URAD = math.pi / 180 * 1_000_000
LINE_RE = re.compile(r"^\s*\$?\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([^,\r\n]+?)\s*$")


def resource_path(name):
    return Path(getattr(sys, "_MEIPASS", APP_DIR)) / name


@dataclass(frozen=True)
class Reading:
    timestamp: datetime
    station: str
    host: str
    x: float
    y: float
    temperature: float
    status: str
    raw: str
    station_id: str = ""


def utc_now():
    return datetime.now(timezone.utc)


def windows_timezone_name():
    """Return the Windows timezone key when available, without extra dependencies."""
    if os.name != "nt":
        return time.tzname[0] if time.tzname else "Unknown"
    try:
        import winreg
        path = r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            return winreg.QueryValueEx(key, "TimeZoneKeyName")[0]
    except (OSError, ImportError):
        return time.tzname[0] if time.tzname else "Unknown"


def pc_time_setting(observed_at=None):
    observed_utc = (observed_at or utc_now()).astimezone(timezone.utc)
    local = observed_utc.astimezone()
    offset = local.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    dst = local.dst() or timedelta(0)
    return {
        "observed_at_utc": iso_utc(observed_utc),
        "local_date": local.strftime("%Y-%m-%d"),
        "local_time": local.isoformat(timespec="seconds"),
        "windows_timezone": windows_timezone_name(),
        "timezone_name": local.tzname() or "Unknown",
        "utc_offset": f"UTC{sign}{hours:02d}:{minutes:02d}",
        "dst_active": "yes" if dst != timedelta(0) else "no",
    }


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_station_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()) or "UNKNOWN"


def apply_calibration(value_deg, zero_deg=0.0, factor_urad_per_deg=DEG_TO_URAD):
    return (value_deg - zero_deg) * factor_urad_per_deg


def default_metadata():
    """Return a complete, JSON-friendly metadata draft for one deployment."""
    return {
        "code": "", "start_date": "", "end_date": "", "location_type": "",
        "latitude": "", "longitude": "", "elevation_m": "", "datum": "WGS84",
        "depth_m": "", "location_notes": "",
        "manufacturer": "", "model": "", "serial_number": "", "firmware": "",
        "axis_x_azimuth_deg": "", "axis_y_azimuth_deg": "", "orientation_reference": "True north",
        "positive_direction": "", "n_samp": "", "gain": "", "filter": "",
        "output_unit": "microradian",
        "logger_manufacturer": "", "logger_model": "", "logger_serial_number": "",
        "logger_firmware": "", "timestamp_source": "Data logger", "timezone": "UTC",
        "time_sync": "NTP", "time_server": "", "sync_interval": "",
        "notes": "", "updated_at_utc": "",
    }


def normalize_metadata(source=None):
    result = default_metadata()
    if isinstance(source, dict):
        result.update({key: "" if value is None else str(value)
                       for key, value in source.items() if key in result})
    return result
METADATA_LABELS = {
    "code": "Kode stasiun", "start_date": "Tanggal mulai", "end_date": "Tanggal selesai",
    "location_type": "Jenis lokasi", "latitude": "Latitude", "longitude": "Longitude",
    "elevation_m": "Elevasi (m)", "datum": "Datum", "depth_m": "Kedalaman (m)",
    "location_notes": "Catatan lokasi", "manufacturer": "Merek sensor", "model": "Model sensor",
    "serial_number": "Serial number sensor", "firmware": "Firmware sensor",
    "axis_x_azimuth_deg": "Azimuth +X (derajat)", "axis_y_azimuth_deg": "Azimuth +Y (derajat)",
    "orientation_reference": "Referensi orientasi", "positive_direction": "Konvensi arah positif",
    "n_samp": "N_SAMP", "gain": "Gain", "filter": "Filter", "output_unit": "Output unit",
    "logger_manufacturer": "Merek data logger", "logger_model": "Model data logger",
    "logger_serial_number": "Serial number data logger", "logger_firmware": "Firmware data logger",
    "timestamp_source": "Sumber timestamp", "timezone": "Timezone", "time_sync": "Sinkronisasi waktu",
    "time_server": "Server / sumber waktu", "sync_interval": "Interval sinkronisasi",
    "notes": "Catatan deployment", "updated_at_utc": "Terakhir diperbarui (UTC)",
}


def write_metadata_snapshot(data_dir, sensor, recorded_at=None):
    """Write the latest human-readable metadata and append an audit snapshot."""
    metadata = normalize_metadata(sensor.get("metadata"))
    timestamp = recorded_at or metadata.get("updated_at_utc") or iso_utc(utc_now())
    station = sensor.get("station", "UNKNOWN").strip() or "UNKNOWN"
    safe_name = safe_station_name(station)
    folder = Path(data_dir) / safe_name
    folder.mkdir(parents=True, exist_ok=True)
    lines = [
        "TILTY STATION METADATA",
        f"Recorded at UTC: {timestamp}",
        f"Station ID: {sensor.get('id', '')}",
        f"Station name: {station}",
    ]
    lines.extend(f"{METADATA_LABELS[key]}: {metadata.get(key, '')}" for key in default_metadata())
    snapshot = "\n".join(lines) + "\n"
    current_path = folder / f"{safe_name}_metadata.txt"
    current_path.write_text(snapshot, encoding="utf-8")
    history_path = folder / f"{safe_name}_metadata_history.log"
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + "=" * 72 + "\n" + snapshot)
    return current_path, history_path



def parse_d701_line(line, station, host, timestamp=None, station_id="", zero_x_deg=0.0,
                    zero_y_deg=0.0, factor_x=DEG_TO_URAD, factor_y=DEG_TO_URAD):
    clean = line.strip()
    match = LINE_RE.match(clean)
    if not match:
        return None
    return Reading(timestamp or utc_now(), station, host,
                   apply_calibration(float(match.group(1)), zero_x_deg, factor_x),
                   apply_calibration(float(match.group(2)), zero_y_deg, factor_y),
                   float(match.group(3)), match.group(4).strip(), clean, station_id)


def new_sensor(station="STATION", host="192.168.1.10", port=4001):
    return {"id": uuid.uuid4().hex, "enabled": True, "station": station, "host": host, "port": port,
            "zero_x_deg": 0.0, "zero_y_deg": 0.0, "factor_x": DEG_TO_URAD, "factor_y": DEG_TO_URAD,
            "metadata": default_metadata()}



def default_config():
    return {"data_dir": str(DEFAULT_DATA_DIR), "refresh_seconds": 2,
            "sensors": [new_sensor("TILT_A", "192.189.77.45"), new_sensor("TILT_B", "192.189.77.46")]}


def normalize_config(source):
    result = default_config()
    result.update({k: source[k] for k in ("data_dir", "refresh_seconds") if k in source})
    result["sensors"] = []
    for old in source.get("sensors", []):
        sensor = new_sensor(old.get("station", "STATION"), old.get("host", "127.0.0.1"), int(old.get("port", 4001)))
        sensor.update(old)
        sensor["id"] = old.get("id") or uuid.uuid4().hex
        for key in ("zero_x_deg", "zero_y_deg"):
            sensor[key] = float(sensor.get(key, 0))
        for key in ("factor_x", "factor_y"):
            sensor[key] = float(sensor.get(key, DEG_TO_URAD))
        sensor["metadata"] = normalize_metadata(old.get("metadata"))
        result["sensors"].append(sensor)
    return result


class DataWriter:
    def __init__(self, root=DEFAULT_DATA_DIR):
        self.root, self.lock = Path(root), threading.Lock()

    def write_raw(self, station, host, timestamp, line):
        name = safe_station_name(station)
        folder = self.root / name / "raw" / timestamp.strftime("%Y")
        folder.mkdir(parents=True, exist_ok=True)
        with self.lock, (folder / f"{timestamp:%Y-%m-%d}_{name}_raw.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{iso_utc(timestamp)}\t{host}\t{line.rstrip()}\n")

    def write_minute(self, reading, sample_count=1):
        name = safe_station_name(reading.station)
        folder = self.root / name / "minute" / reading.timestamp.strftime("%Y")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{reading.timestamp:%Y-%m-%d}_{name}.csv"
        fresh = not path.exists()
        with self.lock, path.open("a", encoding="utf-8-sig", newline="") as handle:
            out = csv.writer(handle)
            if fresh:
                out.writerow(("timestamp_utc", "station", "x_urad", "y_urad", "temperature_c", "status", "sample_count"))
            out.writerow((iso_utc(reading.timestamp), reading.station, f"{reading.x:.6f}", f"{reading.y:.6f}",
                          f"{reading.temperature:.3f}", reading.status, sample_count))
            handle.flush()
        self.write_database(reading, sample_count)
        return path

    def write_database(self, reading, sample_count):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "d701_history.sqlite3"
        with closing(sqlite3.connect(path, timeout=15)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=15000")
            db.execute("""CREATE TABLE IF NOT EXISTS minute_readings (
                station_id TEXT NOT NULL, station TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL, x_urad REAL NOT NULL,
                y_urad REAL NOT NULL, temperature_c REAL NOT NULL,
                status TEXT NOT NULL, sample_count INTEGER NOT NULL,
                PRIMARY KEY (station_id, timestamp_utc))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_minute_station_time ON minute_readings(station_id, timestamp_utc)")
            db.execute("""INSERT OR REPLACE INTO minute_readings
                (station_id, station, timestamp_utc, x_urad, y_urad, temperature_c, status, sample_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (reading.station_id or safe_station_name(reading.station), reading.station,
                 iso_utc(reading.timestamp), reading.x, reading.y, reading.temperature,
                 reading.status, sample_count))
            db.commit()


def downsample_rows(rows, max_points=1500):
    """Average adjacent database rows so plotting cost stays bounded."""
    if len(rows) <= max_points:
        return rows
    bucket = math.ceil(len(rows) / max_points)
    result = []
    for start in range(0, len(rows), bucket):
        group = rows[start:start + bucket]
        result.append((group[len(group)//2][0],
                       sum(row[1] for row in group)/len(group),
                       sum(row[2] for row in group)/len(group),
                       sum(row[3] for row in group)/len(group)))
    return result


def query_history(data_dir, station_id, since, max_points=1500, until=None):
    path = Path(data_dir) / "d701_history.sqlite3"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path, timeout=10)) as db:
        sql = """SELECT timestamp_utc, x_urad, y_urad, temperature_c
            FROM minute_readings WHERE station_id = ? AND timestamp_utc >= ?"""
        params = [station_id, iso_utc(since)]
        if until is not None:
            sql += " AND timestamp_utc <= ?"
            params.append(iso_utc(until))
        rows = db.execute(sql + " ORDER BY timestamp_utc", params).fetchall()
    parsed = [(datetime.fromisoformat(row[0].replace("Z", "+00:00")), row[1], row[2], row[3]) for row in rows]
    return downsample_rows(parsed, max_points)


def query_export_rows(data_dir, station_id, since, until=None):
    """Return complete minute rows for CSV export without graph downsampling."""
    path = Path(data_dir) / "d701_history.sqlite3"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path, timeout=15)) as db:
        sql = """SELECT timestamp_utc, station, x_urad, y_urad,
            temperature_c, status, sample_count
            FROM minute_readings WHERE station_id = ? AND timestamp_utc >= ?"""
        params = [station_id, iso_utc(since)]
        if until is not None:
            sql += " AND timestamp_utc <= ?"
            params.append(iso_utc(until))
        return db.execute(sql + " ORDER BY timestamp_utc", params).fetchall()

def import_legacy_csv(data_dir, source_dir, sensors):
    """Index legacy minute CSV files into the history database without duplicates."""
    source = Path(source_dir)
    files = [source] if source.is_file() else sorted(source.rglob("*.csv"))
    result = {"files": len(files), "imported": 0, "duplicates": 0, "invalid": 0, "unknown": 0}
    station_ids = {}
    for sensor in sensors:
        station_ids[sensor["station"].strip().lower()] = sensor["id"]
        station_ids[safe_station_name(sensor["station"]).lower()] = sensor["id"]
    db_path = Path(data_dir) / "d701_history.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    required = {"timestamp_utc", "station", "x_urad", "y_urad", "temperature_c", "status", "sample_count"}
    with closing(sqlite3.connect(db_path, timeout=30)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("""CREATE TABLE IF NOT EXISTS minute_readings (
            station_id TEXT NOT NULL, station TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL, x_urad REAL NOT NULL,
            y_urad REAL NOT NULL, temperature_c REAL NOT NULL,
            status TEXT NOT NULL, sample_count INTEGER NOT NULL,
            PRIMARY KEY (station_id, timestamp_utc))""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_minute_station_time ON minute_readings(station_id, timestamp_utc)")
        sql = """INSERT OR IGNORE INTO minute_readings
            (station_id, station, timestamp_utc, x_urad, y_urad, temperature_c, status, sample_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        for path in files:
            try:
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if not reader.fieldnames or not required.issubset(reader.fieldnames):
                        continue
                    for row in reader:
                        try:
                            station = row["station"].strip()
                            station_id = station_ids.get(station.lower()) or station_ids.get(safe_station_name(station).lower())
                            if not station_id:
                                result["unknown"] += 1; continue
                            timestamp = datetime.fromisoformat(row["timestamp_utc"].strip().replace("Z", "+00:00"))
                            if timestamp.tzinfo is None: timestamp = timestamp.replace(tzinfo=timezone.utc)
                            values = (station_id, station, iso_utc(timestamp), float(row["x_urad"]),
                                      float(row["y_urad"]), float(row["temperature_c"]),
                                      row["status"].strip(), int(float(row["sample_count"])))
                            cursor = db.execute(sql, values)
                            if cursor.rowcount: result["imported"] += 1
                            else: result["duplicates"] += 1
                        except (KeyError, TypeError, ValueError):
                            result["invalid"] += 1
            except (OSError, csv.Error, UnicodeError):
                result["invalid"] += 1
        db.commit()
    return result

class TimeSettingsRecorder:
    FIELDS = ("observed_at_utc", "local_date", "local_time", "windows_timezone",
              "timezone_name", "utc_offset", "dst_active")
    SIGNATURE_FIELDS = ("windows_timezone", "timezone_name", "utc_offset", "dst_active")

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "pc_time_settings.csv"

    def record_if_needed(self, setting=None):
        setting = setting or pc_time_setting()
        last = None
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                    for row in csv.DictReader(handle):
                        last = row
            except (OSError, csv.Error):
                last = None
        same_day = last and last.get("local_date") == setting["local_date"]
        same_setting = last and all(last.get(key) == setting[key] for key in self.SIGNATURE_FIELDS)
        if same_day and same_setting:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
            if fresh:
                writer.writeheader()
            writer.writerow({key: setting[key] for key in self.FIELDS})
        return True


# Compatibility name used by older integrations/tests.
DailyWriter = DataWriter


class MinuteAggregator:
    def __init__(self):
        self.minute = self.prototype = None
        self.x_sum = self.y_sum = self.temp_sum = 0.0
        self.count = 0

    def add(self, reading):
        minute = reading.timestamp.astimezone(timezone.utc).replace(second=0, microsecond=0)
        completed = self.finish() if self.minute is not None and minute != self.minute else None
        if self.minute is None:
            self.minute, self.prototype = minute, reading
        self.x_sum += reading.x; self.y_sum += reading.y; self.temp_sum += reading.temperature; self.count += 1
        return completed

    def finish_if_due(self, now):
        minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
        return self.finish() if self.minute is not None and minute > self.minute else None

    def finish(self):
        if self.minute is None or not self.count:
            return None
        p, count = self.prototype, self.count
        result = Reading(self.minute, p.station, p.host, self.x_sum/count, self.y_sum/count,
                         self.temp_sum/count, p.status, "", p.station_id)
        self.minute = self.prototype = None
        self.x_sum = self.y_sum = self.temp_sum = 0.0; self.count = 0
        return result, count


class SensorWorker(threading.Thread):
    def __init__(self, sensor, data_dir, events, stop_event):
        super().__init__(name=f"D701-{sensor['station']}", daemon=True)
        self.sensor, self.events, self.stop_event = sensor.copy(), events, stop_event
        self.writer, self.aggregator, self.sock = DataWriter(data_dir), MinuteAggregator(), None

    def emit(self, kind, payload=None):
        self.events.put((kind, self.sensor["id"], payload))

    def close_socket(self):
        sock, self.sock = self.sock, None
        if sock:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()

    def save(self, completed):
        if completed:
            try:
                self.writer.write_minute(*completed)
            except sqlite3.Error as exc:
                self.emit("error", f"SQLite gagal: {exc}")

    def run(self):
        s, delay = self.sensor, 2
        while not self.stop_event.is_set():
            try:
                self.emit("status", f"Menghubungkan {s['host']}:{s['port']}")
                self.sock = socket.create_connection((s["host"], int(s["port"])), timeout=10)
                self.sock.settimeout(1); self.emit("status", "Terhubung"); delay, buffer = 2, b""
                while not self.stop_event.is_set():
                    try: chunk = self.sock.recv(4096)
                    except socket.timeout:
                        self.save(self.aggregator.finish_if_due(utc_now())); continue
                    if not chunk: raise ConnectionError("koneksi ditutup NPort")
                    buffer += chunk
                    if len(buffer) > 65536:
                        buffer = b""; self.emit("error", "buffer melebihi 64 KiB")
                    while b"\n" in buffer or b"\r" in buffer:
                        pos = min(p for p in (buffer.find(b"\n"), buffer.find(b"\r")) if p >= 0)
                        raw, buffer = buffer[:pos], buffer[pos+1:]
                        buffer = buffer.lstrip(b"\r\n")
                        if not raw.strip(): continue
                        at, line = utc_now(), raw.decode("ascii", errors="replace").strip()
                        self.writer.write_raw(s["station"], s["host"], at, line)
                        reading = parse_d701_line(line, s["station"], s["host"], at, s["id"],
                                                 s["zero_x_deg"], s["zero_y_deg"], s["factor_x"], s["factor_y"])
                        if reading:
                            self.save(self.aggregator.add(reading)); self.emit("reading", reading)
                        else: self.emit("error", f"format salah: {line[:60]}")
            except (OSError, ConnectionError) as exc:
                if not self.stop_event.is_set():
                    self.emit("status", f"Terputus: {exc}; ulang {delay} dtk")
                    self.stop_event.wait(delay); delay = min(delay*2, 30)
            finally: self.close_socket()
        self.save(self.aggregator.finish()); self.emit("status", "Berhenti")


class MetadataWizard(tk.Toplevel):
    PAGES = ("Stasiun", "Lokasi", "Perangkat", "Akuisisi", "Tinjau")
    REQUIRED = ("code", "start_date", "location_type", "manufacturer", "model",
                "serial_number", "output_unit", "logger_model", "timestamp_source", "time_sync")
    SECTIONS = {
        0: (("Identitas stasiun", (
                ("_station", "Nama stasiun", "entry", ()),
                ("code", "Kode", "entry", ()),
                ("start_date", "Tanggal mulai (YYYY-MM-DD)", "entry", ()),
                ("end_date", "Tanggal selesai (kosong = aktif)", "entry", ()),
                ("location_type", "Jenis lokasi", "combo",
                 ("Platform", "Borehole", "Vault", "Surface", "Tunnel", "Building", "Lainnya")),
            )),),
        1: (("Koordinat", (
                ("latitude", "Latitude", "entry", ()),
                ("longitude", "Longitude", "entry", ()),
                ("elevation_m", "Elevasi (m)", "entry", ()),
                ("datum", "Datum", "combo", ("WGS84", "DGN95", "Lokal")),
                ("depth_m", "Kedalaman instalasi (m)", "entry", ()),
            )),
            ("Orientasi sensor", (
                ("axis_x_azimuth_deg", "Azimuth sumbu X (derajat)", "entry", ()),
                ("axis_y_azimuth_deg", "Azimuth sumbu Y (derajat)", "entry", ()),
                ("orientation_reference", "Referensi orientasi", "combo",
                 ("True north", "Magnetic north", "Grid north", "Lokal")),
                ("positive_direction",
                 "Arah positif (contoh: +X menuju puncak; +Y 90 derajat searah jarum jam)",
                 "entry", ()),
                ("location_notes", "Catatan lokasi", "entry", ()),
            ))),
        2: (("Sensor tiltmeter", (
                ("manufacturer", "Merek", "entry", ()),
                ("model", "Tipe / model", "entry", ()),
                ("serial_number", "Serial number", "entry", ()),
                ("firmware", "Firmware", "entry", ()),
                ("output_unit", "Output unit", "combo",
                 ("microradian", "milliradian", "degree", "arcsecond", "volt", "raw count")),
            )),
            ("Konfigurasi internal", (
                ("n_samp", "N_SAMP", "entry", ()),
                ("gain", "Gain", "entry", ()),
                ("filter", "Filter", "entry", ()),
            ))),
        3: (("Data logger", (
                ("logger_manufacturer", "Merek data logger", "entry", ()),
                ("logger_model", "Model data logger", "entry", ()),
                ("logger_serial_number", "Serial number data logger", "entry", ()),
                ("logger_firmware", "Firmware data logger", "entry", ()),
            )),
            ("Timestamp dan sinkronisasi", (
                ("timestamp_source", "Sumber timestamp", "combo",
                 ("Sensor", "Data logger", "Komputer akuisisi", "Server")),
                ("timezone", "Timezone data", "combo", ("UTC", "Asia/Jakarta", "Asia/Makassar", "Asia/Jayapura")),
                ("time_sync", "Sinkronisasi waktu", "combo", ("NTP", "GPS", "PTP", "RTC internal", "Manual")),
                ("time_server", "Server / sumber waktu", "entry", ()),
                ("sync_interval", "Interval sinkronisasi", "entry", ()),
                ("notes", "Catatan deployment", "entry", ()),
            ))),
    }

    def __init__(self, parent, sensor):
        super().__init__(parent)
        self.parent, self.sensor, self.result, self.page = parent, sensor, None, 0
        metadata = normalize_metadata(sensor.get("metadata"))
        self.vars = {key: tk.StringVar(self, value=value) for key, value in metadata.items()}
        self.vars["_station"] = tk.StringVar(self, value=sensor.get("station", ""))
        self.title("Metadata Stasiun")
        self.geometry("1040x720")
        self.minsize(900, 620)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build()
        self.grab_set()
        self.wait_visibility()
        self.focus_set()
        self.wait_window(self)

    def _build(self):
        header = tk.Frame(self, bg="#F7FAFC", padx=24, pady=16)
        header.pack(fill="x")
        tk.Label(header, text="Metadata Stasiun", bg="#F7FAFC", fg="#102A43",
                 font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(header, text="Lengkapi informasi instalasi secara bertahap",
                 bg="#F7FAFC", fg="#627D98", font=("Segoe UI", 10)).pack(anchor="w")
        self.steps = tk.Frame(self, bg="#F7FAFC", padx=20, pady=8)
        self.steps.pack(fill="x")
        self.step_labels = []
        for index, name in enumerate(self.PAGES):
            label = tk.Label(self.steps, text=f"{index + 1}  {name}", padx=12, pady=8,
                             font=("Segoe UI", 9, "bold"), cursor="hand2")
            label.pack(side="left", expand=True)
            label.bind("<Button-1>", lambda _event, value=index: self.go_to(value))
            self.step_labels.append(label)

        content = ttk.Frame(self, padding=(20, 14))
        content.pack(fill="both", expand=True)
        self.page_frame = ttk.Frame(content)
        self.page_frame.pack(side="left", fill="both", expand=True, padx=(0, 14))
        summary = ttk.LabelFrame(content, text="Ringkasan", padding=16, width=250)
        summary.pack(side="right", fill="y")
        summary.pack_propagate(False)
        self.summary_name = tk.StringVar()
        self.summary_code = tk.StringVar()
        self.summary_location = tk.StringVar()
        self.summary_period = tk.StringVar()
        self.summary_complete = tk.StringVar()
        ttk.Label(summary, textvariable=self.summary_code, font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(4, 8))
        ttk.Label(summary, textvariable=self.summary_name).pack(anchor="w", pady=4)
        ttk.Label(summary, textvariable=self.summary_location).pack(anchor="w", pady=4)
        ttk.Label(summary, textvariable=self.summary_period).pack(anchor="w", pady=4)
        ttk.Separator(summary).pack(fill="x", pady=16)
        ttk.Label(summary, textvariable=self.summary_complete, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(summary, text="Data teknis dapat dilengkapi\nsecara bertahap.",
                  foreground="#627D98", justify="left").pack(anchor="w", pady=(12, 0))

        footer = ttk.Frame(self, padding=(20, 12))
        footer.pack(fill="x")
        self.position_text = tk.StringVar()
        ttk.Label(footer, textvariable=self.position_text).pack(side="left")
        ttk.Button(footer, text="Batal", command=self.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="Simpan draf", command=self.save_draft).pack(side="right", padx=8)
        self.next_button = ttk.Button(footer, command=self.next_page)
        self.next_button.pack(side="right", padx=8)
        self.back_button = ttk.Button(footer, text="Kembali", command=self.previous_page)
        self.back_button.pack(side="right")
        for variable in self.vars.values():
            variable.trace_add("write", lambda *_args: self.refresh_summary())
        self.render_page()

    def _field(self, parent, row, column, key, label, kind, values):
        box = ttk.Frame(parent)
        box.grid(row=row, column=column, sticky="ew", padx=7, pady=6)
        ttk.Label(box, text=label).pack(anchor="w", pady=(0, 3))
        if kind == "combo":
            widget = ttk.Combobox(box, textvariable=self.vars[key], values=values, state="readonly")
        else:
            widget = ttk.Entry(box, textvariable=self.vars[key])
        widget.pack(fill="x")
        return widget

    def render_page(self):
        for child in self.page_frame.winfo_children():
            child.destroy()
        for index, label in enumerate(self.step_labels):
            active = index == self.page
            label.configure(bg="#0B74C9" if active else "#EAF2F8",
                            fg="white" if active else "#486581")
        self.position_text.set(f"Langkah {self.page + 1} dari {len(self.PAGES)}")
        self.back_button.configure(state="normal" if self.page else "disabled")
        self.next_button.configure(text="Simpan metadata" if self.page == 4
                                   else f"Lanjut: {self.PAGES[self.page + 1]}  >")
        if self.page == 4:
            self._render_review()
        else:
            for section_title, fields in self.SECTIONS[self.page]:
                group = ttk.LabelFrame(self.page_frame, text=section_title, padding=12)
                group.pack(fill="x", pady=(0, 12))
                group.columnconfigure(0, weight=1)
                group.columnconfigure(1, weight=1)
                for index, field in enumerate(fields):
                    self._field(group, index // 2, index % 2, *field)
        self.refresh_summary()

    def _render_review(self):
        ttk.Label(self.page_frame, text="Tinjau metadata", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(self.page_frame, text="Pastikan informasi utama sudah benar sebelum disimpan.",
                  foreground="#627D98").pack(anchor="w", pady=(2, 14))
        rows = (
            ("Stasiun", self.vars["_station"].get()),
            ("Kode / periode", f"{self.vars['code'].get()}  |  {self._period()}"),
            ("Lokasi", self.vars["location_type"].get() or "Belum diisi"),
            ("Koordinat", f"{self.vars['latitude'].get() or '-'}, {self.vars['longitude'].get() or '-'}"),
            ("Sensor", " / ".join(filter(None, (self.vars["manufacturer"].get(),
                                                  self.vars["model"].get(),
                                                  self.vars["serial_number"].get()))) or "Belum diisi"),
            ("Konfigurasi", f"N_SAMP {self.vars['n_samp'].get() or '-'} | "
                            f"gain {self.vars['gain'].get() or '-'} | filter {self.vars['filter'].get() or '-'}"),
            ("Data logger", " / ".join(filter(None, (self.vars["logger_manufacturer"].get(),
                                                       self.vars["logger_model"].get()))) or "Belum diisi"),
            ("Waktu", f"{self.vars['timestamp_source'].get() or '-'} | "
                      f"{self.vars['time_sync'].get() or '-'} | {self.vars['timezone'].get() or '-'}"),
        )
        card = ttk.LabelFrame(self.page_frame, padding=14)
        card.pack(fill="x")
        for row, (label, value) in enumerate(rows):
            ttk.Label(card, text=label, font=("Segoe UI", 9, "bold")).grid(
                row=row, column=0, sticky="nw", padx=(0, 18), pady=6)
            ttk.Label(card, text=value, wraplength=520).grid(row=row, column=1, sticky="nw", pady=6)
        card.columnconfigure(1, weight=1)

    def _period(self):
        start = self.vars["start_date"].get().strip() or "belum diisi"
        end = self.vars["end_date"].get().strip() or "sekarang"
        return f"{start} - {end}"

    def refresh_summary(self):
        self.summary_name.set(self.vars["_station"].get().strip() or "Nama belum diisi")
        self.summary_code.set(self.vars["code"].get().strip() or "DRAF")
        self.summary_location.set(self.vars["location_type"].get().strip() or "Jenis lokasi belum diisi")
        self.summary_period.set(self._period())
        keys = [key for key in default_metadata() if key != "updated_at_utc"]
        filled = sum(bool(self.vars[key].get().strip()) for key in keys) + bool(self.vars["_station"].get().strip())
        self.summary_complete.set(f"Kelengkapan {round(filled / (len(keys) + 1) * 100)}%")

    def validate_values(self, require_complete=False):
        try:
            for key in ("start_date", "end_date"):
                value = self.vars[key].get().strip()
                if value:
                    datetime.strptime(value, "%Y-%m-%d")
            start, end = self.vars["start_date"].get().strip(), self.vars["end_date"].get().strip()
            if start and end and end < start:
                raise ValueError("Tanggal selesai harus sama atau setelah tanggal mulai.")
            for key, low, high, label in (
                    ("latitude", -90, 90, "Latitude"), ("longitude", -180, 180, "Longitude"),
                    ("axis_x_azimuth_deg", 0, 360, "Azimuth X"),
                    ("axis_y_azimuth_deg", 0, 360, "Azimuth Y")):
                value = self.vars[key].get().strip().replace(",", ".")
                if value and not low <= float(value) <= high:
                    raise ValueError(f"{label} harus antara {low} dan {high}.")
            if not self.vars["_station"].get().strip():
                raise ValueError("Nama stasiun wajib diisi.")
            if require_complete:
                missing = [key for key in self.REQUIRED if not self.vars[key].get().strip()]
                if missing:
                    labels = {"code": "kode", "start_date": "tanggal mulai",
                              "location_type": "jenis lokasi", "manufacturer": "merek sensor",
                              "model": "model sensor", "serial_number": "serial number sensor",
                              "output_unit": "output unit", "logger_model": "model data logger",
                              "timestamp_source": "sumber timestamp", "time_sync": "sinkronisasi waktu"}
                    raise ValueError("Lengkapi field wajib: " + ", ".join(labels[key] for key in missing) + ".")
            return True
        except ValueError as exc:
            messagebox.showerror("Metadata tidak valid", str(exc), parent=self)
            return False

    def go_to(self, page):
        if self.validate_values(False):
            self.page = page
            self.render_page()

    def previous_page(self):
        if self.page:
            self.page -= 1
            self.render_page()

    def next_page(self):
        if not self.validate_values(self.page == 4):
            return
        if self.page == 4:
            self._save()
        else:
            self.page += 1
            self.render_page()

    def save_draft(self):
        if self.validate_values(False):
            self._save()

    def _save(self):
        metadata = {key: variable.get().strip() for key, variable in self.vars.items() if key != "_station"}
        metadata["updated_at_utc"] = iso_utc(utc_now())
        self.result = {"station": self.vars["_station"].get().strip(), "metadata": metadata}
        self.destroy()


class SensorDialog(simpledialog.Dialog):

    def __init__(self, parent, title, sensor=None):
        self.sensor, self.result = (sensor or new_sensor()).copy(), None
        super().__init__(parent, title)

    def body(self, master):
        self.vars = {}
        for row, (key, label) in enumerate((("station", "Nama stasiun"), ("host", "IP/hostname NPort"), ("port", "TCP port"))):
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=5)
            self.vars[key] = tk.StringVar(value=str(self.sensor[key]))
            ttk.Entry(master, textvariable=self.vars[key], width=30).grid(row=row, column=1, padx=6, pady=5)
        self.enabled = tk.BooleanVar(value=self.sensor.get("enabled", True))
        ttk.Checkbutton(master, text="Aktif", variable=self.enabled).grid(row=3, column=1, sticky="w")
        return master.winfo_children()[1]

    def validate(self):
        try:
            port = int(self.vars["port"].get())
            if not 1 <= port <= 65535: raise ValueError("Port harus 1–65535")
            if not self.vars["station"].get().strip() or not self.vars["host"].get().strip():
                raise ValueError("Nama dan alamat tidak boleh kosong")
            self.sensor.update(station=self.vars["station"].get().strip(), host=self.vars["host"].get().strip(),
                               port=port, enabled=self.enabled.get())
            return True
        except ValueError as exc:
            messagebox.showerror("Konfigurasi tidak valid", str(exc), parent=self); return False

    def apply(self): self.result = self.sensor


class CalibrationDialog(simpledialog.Dialog):
    def __init__(self, parent, sensor):
        self.sensor, self.result = sensor.copy(), None
        super().__init__(parent, f"Kalibrasi — {sensor['station']}")

    def body(self, master):
        ttk.Label(master, text="µrad = (derajat − zero) × faktor", font=("Segoe UI", 10, "bold")).grid(row=0, columnspan=3, pady=10)
        for col, text in enumerate(("Sumbu", "Zero offset (°)", "Faktor (µrad/°)")):
            ttk.Label(master, text=text).grid(row=1, column=col, padx=7)
        self.vars = {}
        for row, axis in enumerate(("x", "y"), 2):
            ttk.Label(master, text=axis.upper()).grid(row=row, column=0)
            z = tk.StringVar(value=f"{self.sensor[f'zero_{axis}_deg']:.10g}")
            f = tk.StringVar(value=f"{self.sensor[f'factor_{axis}']:.12g}")
            ttk.Entry(master, textvariable=z).grid(row=row, column=1, padx=5, pady=5)
            ttk.Entry(master, textvariable=f).grid(row=row, column=2, padx=5, pady=5)
            self.vars[axis] = z, f
        ttk.Button(master, text="Gunakan faktor standar", command=self.standard).grid(row=4, column=1, columnspan=2, pady=8)
        return master

    def standard(self):
        for _, factor in self.vars.values(): factor.set(f"{DEG_TO_URAD:.12g}")

    def validate(self):
        try:
            for axis, (zero, factor) in self.vars.items():
                self.sensor[f"zero_{axis}_deg"] = float(zero.get().replace(",", "."))
                self.sensor[f"factor_{axis}"] = float(factor.get().replace(",", "."))
                if not self.sensor[f"factor_{axis}"]: raise ValueError("Faktor tidak boleh nol")
            return True
        except ValueError as exc:
            messagebox.showerror("Kalibrasi tidak valid", str(exc), parent=self); return False

    def apply(self): self.result = self.sensor

class CustomPeriodDialog(simpledialog.Dialog):
    FORMAT = "%Y-%m-%d %H:%M"

    def body(self, master):
        ttk.Label(master, text="Periode grafik dalam waktu UTC", font=("Segoe UI Semibold", 10)).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(master, text="Mulai (YYYY-MM-DD HH:MM)").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Label(master, text="Selesai (YYYY-MM-DD HH:MM)").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        end = utc_now().replace(second=0, microsecond=0)
        start = end - timedelta(days=7)
        current = getattr(self.parent, "save_period", None)
        if current:
            start, end = current
        self.start_var = tk.StringVar(value=start.strftime(self.FORMAT))
        self.end_var = tk.StringVar(value=end.strftime(self.FORMAT))
        start_entry = ttk.Entry(master, textvariable=self.start_var, width=22)
        ttk.Entry(master, textvariable=self.end_var, width=22).grid(row=2, column=1, sticky="ew", pady=4)
        start_entry.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(master, text="Tanggal dan waktu akhir termasuk dalam grafik.", foreground="#627D98").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        return start_entry

    def validate(self):
        try:
            start = datetime.strptime(self.start_var.get().strip(), self.FORMAT).replace(tzinfo=timezone.utc)
            end = datetime.strptime(self.end_var.get().strip(), self.FORMAT).replace(tzinfo=timezone.utc)
            if end <= start:
                raise ValueError("Waktu selesai harus setelah waktu mulai.")
            self.period = (start, end)
            return True
        except ValueError as exc:
            messagebox.showerror("Periode tidak valid", str(exc), parent=self)
            return False

    def apply(self):
        self.result = self.period


class MonitorApp(tk.Tk):
    RANGE_DAYS = {"1 Hari": 1, "1 Minggu": 7, "1 Bulan": 30, "3 Bulan": 90}

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — Tiltmeter Monitor"); self.geometry("1180x800"); self.minsize(900, 650)
        try: self.iconbitmap(default=str(resource_path("tilty.ico")))
        except tk.TclError: pass
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.events, self.workers = queue.Queue(), {}
        self.config_data = self.load_config(); self.status = {}; self.latest = {}; self.counts = {}; self.history = {}
        self.historical = {}; self.history_request = 0; self.save_period = None
        self.build_ui(); self.refresh_table(); self.after(100, self.process_events); self.after(1000, self.refresh_plot)
        self.after(1200, self.check_time_setting)
        self.after(500, self.auto_start)
    @staticmethod
    def load_config():
        for path in (CONFIG_PATH, LEGACY_CONFIG_PATH, APP_DIR / "config.json"):
            try:
                if path.exists(): return normalize_config(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError): pass
        return default_config()

    def save_config(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.config_data, indent=2), encoding="utf-8")

    def build_ui(self):
        self.range_var = tk.StringVar(value="Real-time")
        menu = tk.Menu(self); settings = tk.Menu(menu, tearoff=False)
        settings.add_command(label="Folder penyimpanan…", command=self.choose_folder)
        settings.add_command(label="Ekspor data CSV…", command=self.export_data)
        settings.add_command(label="Impor data lama…", command=self.import_old_data)
        settings.add_separator()
        settings.add_command(label="Keluar", command=self.on_close)
        menu.add_cascade(label="Pengaturan", menu=settings)
        graph_menu = tk.Menu(menu, tearoff=False)
        for label in ("Real-time", "1 Hari", "1 Minggu", "1 Bulan", "3 Bulan"):
            graph_menu.add_radiobutton(label=label, variable=self.range_var, value=label,
                                       command=self.set_graph_period)
        graph_menu.add_separator()
        graph_menu.add_command(label="Simpan grafik…", command=self.save_graph)
        menu.add_cascade(label="Grafik", menu=graph_menu)
        menu.add_command(label="Kalibrasi", command=self.calibrate)
        menu.add_command(label="Metadata", command=self.edit_metadata)
        menu.add_command(label="Tentang", command=lambda: messagebox.showinfo(
            "Tentang Tilty", f"Tilty: Tiltmeter TCP Monitor\nVersi: {APP_VERSION}\n\n"
            "Oleh: Sulistiyani\nTahun: 2026"))
        self.configure(menu=menu)
        header = ttk.Frame(self, padding=(12, 8)); header.pack(fill="x")
        try:
            self.logo_image = tk.PhotoImage(file=str(resource_path("tilty_logo_64.png")))
            ttk.Label(header, image=self.logo_image).pack(side="left", padx=(0, 10))
        except tk.TclError: self.logo_image = None
        title_box = ttk.Frame(header); title_box.pack(side="left")
        ttk.Label(title_box, text="Tilty", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(title_box, text="Multi-station Tiltmeter TCP Monitor").pack(anchor="w")
        self.connection_status = tk.StringVar(value="Koneksi: 0 terhubung · 0 menghubungkan · 0 berhenti")
        ttk.Label(header, textvariable=self.connection_status).pack(side="right", padx=(12, 4))
        panel = ttk.LabelFrame(self, text="Stasiun NPort (TCP Server)", padding=8); panel.pack(fill="x", padx=10, pady=10)
        columns = ("active", "station", "endpoint", "status", "latest", "count")
        self.tree = ttk.Treeview(panel, columns=columns, show="headings", height=6, selectmode="browse")
        for key, label, width in zip(columns, ("Aktif", "Nama stasiun", "Endpoint", "Status", "Data terakhir", "Sampel"),
                                     (55, 140, 165, 220, 410, 65)):
            self.tree.heading(key, text=label); self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="x"); self.tree.bind("<Double-1>", lambda _e: self.edit_station())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.load_selected_history())
        buttons = ttk.Frame(panel); buttons.pack(fill="x", pady=(8, 0))
        for label, cmd in (("Tambah", self.add_station), ("Edit", self.edit_station), ("Hapus", self.delete_station), ("Kalibrasi", self.calibrate)):
            ttk.Button(buttons, text=label, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Metadata", command=self.edit_metadata).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Mulai dipilih", command=self.start_selected).pack(side="left", padx=(18, 6))
        ttk.Button(buttons, text="Hentikan dipilih", command=self.stop_selected).pack(side="left", padx=(0, 6))
        self.start_button = ttk.Button(buttons, text="Mulai semua", command=self.start); self.start_button.pack(side="left", padx=(8, 6))
        self.stop_button = ttk.Button(buttons, text="Hentikan semua", command=self.stop, state="disabled"); self.stop_button.pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Ekspor CSV", command=self.export_data).pack(side="left", padx=(8, 6))
        ttk.Button(buttons, text="Buka folder data", command=self.open_folder).pack(side="left")
        self.folder_text = tk.StringVar(value=self.config_data["data_dir"]); ttk.Label(panel, textvariable=self.folder_text).pack(fill="x", pady=(7, 0))
        graph_controls = ttk.Frame(self); graph_controls.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(graph_controls, text="Rentang grafik:").pack(side="left")
        ranges = ttk.Combobox(graph_controls, textvariable=self.range_var, state="readonly", width=14,
                              values=("Real-time", "1 Hari", "1 Minggu", "1 Bulan", "3 Bulan"))
        ranges.pack(side="left", padx=6); ranges.bind("<<ComboboxSelected>>", lambda _e: self.set_graph_period())
        ttk.Button(graph_controls, text="Muat ulang", command=lambda: self.load_selected_history(True)).pack(side="left")
        ttk.Button(graph_controls, text="Simpan grafik", command=self.save_graph).pack(side="left", padx=6)
        self.graph_status = tk.StringVar(value="Menampilkan sampel real-time terakhir")
        ttk.Label(graph_controls, textvariable=self.graph_status).pack(side="left", padx=8)
        self.figure = Figure(figsize=(10, 5), dpi=100, constrained_layout=True); self.lines = []
        first_axis = self.figure.add_subplot(311)
        self.axes = [first_axis, self.figure.add_subplot(312, sharex=first_axis), self.figure.add_subplot(313, sharex=first_axis)]
        for axis, label in zip(self.axes, ("X (µrad)", "Y (µrad)", "Temperature (°C)")):
            axis.set_ylabel(label); axis.grid(True, linestyle="--", alpha=.4, zorder=1)
            self.lines.append(axis.plot([], [], linestyle="none", marker="o", markersize=2.5, color="black", alpha=.8, zorder=3)[0])
        self.axes[-1].set_xlabel("Date Time (UTC)")
        self.canvas = FigureCanvasTkAgg(self.figure, master=self); self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.toolbar = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
        self.toolbar.update(); self.toolbar.pack(fill="x", padx=10, pady=(0, 8))
    def selected(self):
        ids = self.tree.selection()
        return next((s for s in self.config_data["sensors"] if ids and s["id"] == ids[0]), None)

    def refresh_table(self):
        selected = self.tree.selection(); self.tree.delete(*self.tree.get_children())
        for s in self.config_data["sensors"]:
            self.tree.insert("", "end", iid=s["id"], values=("Ya" if s.get("enabled", True) else "Tidak", s["station"],
                f"{s['host']}:{s['port']}", self.status.get(s["id"], "Berhenti"), self.latest.get(s["id"], "—"), self.counts.get(s["id"], 0)))
        children = self.tree.get_children()
        if children: self.tree.selection_set(selected[0] if selected and selected[0] in children else children[0])
        self.update_connection_summary()

    def update_connection_summary(self):
        connected = sum(self.status.get(s["id"]) == "Terhubung" for s in self.config_data["sensors"])
        connecting = sum(s["id"] in self.workers and self.status.get(s["id"]) != "Terhubung"
                         for s in self.config_data["sensors"])
        stopped = len(self.config_data["sensors"]) - connected - connecting
        self.connection_status.set(
            f"Koneksi: {connected} terhubung · {connecting} menghubungkan · {stopped} berhenti")

    def editable(self):
        if self.workers: messagebox.showwarning("Akuisisi berjalan", "Hentikan akuisisi sebelum mengubah konfigurasi."); return False
        return True

    def add_station(self):
        if not self.editable():
            return
        dialog = SensorDialog(self, "Tambah stasiun")
        if not dialog.result:
            return
        sensor = dialog.result
        self.config_data["sensors"].append(sensor)
        self.save_config(); self.refresh_table()
        self.tree.selection_set(sensor["id"])
        self.open_metadata(sensor)

    def edit_station(self):
        sensor = self.selected()
        if self.editable() and sensor:
            d = SensorDialog(self, "Edit stasiun", sensor)
            if d.result: sensor.update(d.result); self.save_config(); self.refresh_table()

    def open_metadata(self, sensor):
        dialog = MetadataWizard(self, sensor)
        if not dialog.result:
            return
        sensor.update(dialog.result)
        self.save_config()
        try:
            current, history = write_metadata_snapshot(self.config_data["data_dir"], sensor)
            self.graph_status.set(f"Metadata disimpan: {current}")
        except OSError as exc:
            messagebox.showwarning("Metadata tersimpan sebagian",
                                   f"Konfigurasi tersimpan, tetapi file metadata gagal ditulis:\n{exc}",
                                   parent=self)
        self.refresh_table()

    def edit_metadata(self):
        sensor = self.selected()
        if not sensor:
            messagebox.showinfo("Metadata", "Pilih satu stasiun terlebih dahulu.")
            return
        if self.editable():
            self.open_metadata(sensor)

    def delete_station(self):
        sensor = self.selected()
        if self.editable() and sensor and messagebox.askyesno("Hapus", f"Hapus konfigurasi {sensor['station']}?\nData tidak dihapus."):
            self.config_data["sensors"].remove(sensor); self.save_config(); self.refresh_table()

    def calibrate(self):
        sensor = self.selected()
        if self.editable() and sensor:
            d = CalibrationDialog(self, sensor)
            if d.result: sensor.update(d.result); self.save_config(); self.refresh_table()
        elif not sensor and not self.workers: messagebox.showinfo("Kalibrasi", "Pilih satu stasiun terlebih dahulu.")

    def choose_folder(self):
        if not self.editable(): return
        path = filedialog.askdirectory(initialdir=self.config_data["data_dir"])
        if path: self.config_data["data_dir"] = path; self.folder_text.set(path); self.save_config()

    def check_time_setting(self):
        try:
            TimeSettingsRecorder(self.config_data["data_dir"]).record_if_needed()
        except OSError as exc:
            self.graph_status.set(f"Gagal mencatat setting waktu PC: {exc}")
        self.after(60_000, self.check_time_setting)

    def start(self):
        try:
            sensors = [s for s in self.config_data["sensors"] if s.get("enabled", True) and s["id"] not in self.workers]
            if not sensors: raise ValueError("Tidak ada stasiun aktif")
            all_running = [entry[0].sensor for entry in self.workers.values()] + sensors
            endpoints = [(s["host"].lower(), int(s["port"])) for s in all_running]
            names = [safe_station_name(s["station"]).lower() for s in all_running]
            if len(endpoints) != len(set(endpoints)): raise ValueError("Endpoint aktif harus berbeda")
            if len(names) != len(set(names)): raise ValueError("Nama stasiun aktif harus berbeda")
            data_dir = Path(self.config_data["data_dir"]); data_dir.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc: messagebox.showerror("Tidak dapat memulai", str(exc)); return
        self.save_config()
        for sensor in sensors:
            stop_event = threading.Event()
            worker = SensorWorker(sensor, data_dir, self.events, stop_event)
            self.workers[sensor["id"]] = (worker, stop_event)
            worker.start()
        self.stop_button.configure(state="normal")

    def auto_start(self):
        """Start enabled stations on launch for Windows Startup recovery."""
        if self.workers or not any(s.get("enabled", True) for s in self.config_data["sensors"]):
            return
        self.start()

    def start_selected(self):
        sensor = self.selected()
        if not sensor:
            messagebox.showinfo("Mulai stasiun", "Pilih satu stasiun terlebih dahulu."); return
        if not sensor.get("enabled", True):
            messagebox.showwarning("Stasiun nonaktif", "Aktifkan stasiun melalui tombol Edit sebelum memulai."); return
        if sensor["id"] in self.workers:
            messagebox.showinfo("Mulai stasiun", f"{sensor['station']} sudah berjalan."); return
        try:
            running = [entry[0].sensor for entry in self.workers.values()]
            endpoints = [(s["host"].lower(), int(s["port"])) for s in running + [sensor]]
            names = [safe_station_name(s["station"]).lower() for s in running + [sensor]]
            if len(endpoints) != len(set(endpoints)): raise ValueError("Endpoint sama dengan stasiun yang sedang berjalan")
            if len(names) != len(set(names)): raise ValueError("Nama sama dengan stasiun yang sedang berjalan")
            data_dir = Path(self.config_data["data_dir"]); data_dir.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Tidak dapat memulai", str(exc)); return
        stop_event = threading.Event()
        worker = SensorWorker(sensor, data_dir, self.events, stop_event)
        self.workers[sensor["id"]] = (worker, stop_event)
        worker.start(); self.stop_button.configure(state="normal")

    def stop_selected(self):
        sensor = self.selected()
        if not sensor:
            messagebox.showinfo("Hentikan stasiun", "Pilih satu stasiun terlebih dahulu."); return
        entry = self.workers.pop(sensor["id"], None)
        if not entry:
            messagebox.showinfo("Hentikan stasiun", f"{sensor['station']} tidak sedang berjalan."); return
        worker, stop_event = entry
        stop_event.set(); worker.close_socket(); worker.join(timeout=3)
        if not self.workers: self.stop_button.configure(state="disabled")

    def stop(self):
        entries, self.workers = list(self.workers.values()), {}
        for worker, stop_event in entries: stop_event.set(); worker.close_socket()
        for worker, _stop_event in entries: worker.join(timeout=3)
        self.stop_button.configure(state="disabled")

    def process_events(self):
        dirty = False
        try:
            while True:
                kind, sid, payload = self.events.get_nowait(); dirty = True
                if kind in ("status", "error"): self.status[sid] = payload
                elif kind == "history":
                    request_id, label, rows = payload
                    if request_id == self.history_request:
                        self.historical[(sid, label)] = rows
                        self.graph_status.set(f"{len(rows):,} titik historis ditampilkan")
                elif kind == "history_error":
                    request_id, error = payload
                    if request_id == self.history_request:
                        self.graph_status.set(f"Gagal membaca database: {error}")
                elif kind == "import_done":
                    self.historical.clear()
                    self.graph_status.set(f"Impor selesai: {payload['imported']:,} data baru")
                    messagebox.showinfo("Impor data lama",
                        f"File diperiksa: {payload['files']:,}\nData baru: {payload['imported']:,}\n"
                        f"Duplikat dilewati: {payload['duplicates']:,}\nBaris rusak: {payload['invalid']:,}\n"
                        f"Stasiun tidak dikenal: {payload['unknown']:,}")
                    self.load_selected_history(force=True)
                elif kind == "import_error":
                    self.graph_status.set("Impor data lama gagal")
                    messagebox.showerror("Impor data lama gagal", str(payload))
                elif kind == "reading":
                    h = self.history.setdefault(sid, {k: deque(maxlen=600) for k in ("t", "x", "y", "temp")})
                    for key, value in (("t", payload.timestamp), ("x", payload.x), ("y", payload.y), ("temp", payload.temperature)): h[key].append(value)
                    self.counts[sid] = self.counts.get(sid, 0) + 1
                    self.latest[sid] = f"X={payload.x:.2f} µrad, Y={payload.y:.2f} µrad, T={payload.temperature:.2f} °C"
        except queue.Empty: pass
        if dirty: self.refresh_table()
        self.after(100, self.process_events)

    def set_graph_period(self):
        self.load_selected_history()
        self.refresh_plot(schedule=False)

    def load_selected_history(self, force=False):
        sensor = self.selected()
        label = self.range_var.get()
        if not sensor or label == "Real-time":
            self.graph_status.set("Menampilkan sampel real-time terakhir")
            return
        cache_key = (sensor["id"], label)
        if not force and cache_key in self.historical:
            self.graph_status.set(f"{len(self.historical[cache_key]):,} titik historis ditampilkan")
            return
        self.history_request += 1
        request_id, station_id = self.history_request, sensor["id"]
        since = utc_now() - timedelta(days=self.RANGE_DAYS[label])
        self.graph_status.set("Memuat data historis…")

        def load():
            try:
                rows = query_history(self.config_data["data_dir"], station_id, since)
                self.events.put(("history", station_id, (request_id, label, rows)))
            except sqlite3.Error as exc:
                self.events.put(("history_error", station_id, (request_id, str(exc))))
        threading.Thread(target=load, name="D701-History", daemon=True).start()

    def refresh_plot(self, schedule=True):
        sensor = self.selected(); history = self.history.get(sensor["id"]) if sensor else None
        label = self.range_var.get()
        if sensor and label != "Real-time":
            rows = self.historical.get((sensor["id"], label), [])
            history = {"t": [r[0] for r in rows], "x": [r[1] for r in rows],
                       "y": [r[2] for r in rows], "temp": [r[3] for r in rows]}
        for line, key in zip(self.lines, ("x", "y", "temp")):
            line.set_data(list(history["t"]), list(history[key])) if history else line.set_data([], [])
        for axis in self.axes: axis.relim(); axis.autoscale_view()
        if label == "Real-time":
            formatter, rotation = DateFormatter("%H:%M:%S", tz=timezone.utc), 0
            period_text = "Real-time"
        else:
            formatter, rotation = DateFormatter("%Y-%m-%d", tz=timezone.utc), 90
            period_text = f"{label} terakhir"
            end = utc_now(); start = end - timedelta(days=self.RANGE_DAYS[label])
            for axis in self.axes: axis.set_xlim(start, end)
        self.axes[-1].xaxis.set_major_formatter(formatter)
        for tick in self.axes[-1].get_xticklabels():
            tick.set_rotation(rotation); tick.set_ha("center")
        title = f"Data Tilt dan Suhu Stasiun {sensor['station']}\nPeriode {period_text}" if sensor else "Data Tilt dan Suhu"
        self.figure.suptitle(title, fontsize=11, fontweight="bold")
        self.canvas.draw_idle()
        if schedule:
            self.after(max(500, int(self.config_data.get("refresh_seconds", 2)*1000)), self.refresh_plot)

    def save_graph(self):
        sensor = self.selected()
        if not sensor:
            messagebox.showinfo("Simpan grafik", "Pilih satu stasiun terlebih dahulu.")
            return
        dialog = CustomPeriodDialog(self, "Rentang Waktu Grafik")
        if not dialog.result:
            return
        start, end = dialog.result
        self.save_period = (start, end)
        try:
            rows = query_history(self.config_data["data_dir"], sensor["id"], start, max_points=5000, until=end)
        except sqlite3.Error as exc:
            messagebox.showerror("Gagal membaca data", str(exc)); return
        if not rows:
            messagebox.showinfo("Simpan grafik", "Tidak ada data pada rentang waktu tersebut.")
            return
        destination = filedialog.asksaveasfilename(
            title="Simpan grafik", defaultextension=".png",
            filetypes=(("PNG image", "*.png"), ("JPEG image", "*.jpg;*.jpeg")),
            initialfile=f"Grafik_{safe_station_name(sensor['station'])}_{start:%Y%m%d_%H%M}_{end:%Y%m%d_%H%M}.png")
        if not destination: return
        suffix = Path(destination).suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg"):
            destination += ".png"; suffix = ".png"
        export_figure = Figure(figsize=(9, 6), dpi=100, constrained_layout=True)
        first_axis = export_figure.add_subplot(311)
        axes = [first_axis, export_figure.add_subplot(312, sharex=first_axis), export_figure.add_subplot(313, sharex=first_axis)]
        times = [row[0] for row in rows]
        for axis, values, label in zip(axes, ([row[1] for row in rows], [row[2] for row in rows], [row[3] for row in rows]),
                                       ("X (µrad)", "Y (µrad)", "Temperature (°C)")):
            axis.scatter(times, values, color="black", s=6, alpha=.8, linewidths=0, rasterized=True, zorder=3)
            axis.set_ylabel(label); axis.grid(True, linestyle="--", alpha=.4, zorder=1); axis.set_xlim(start, end)
            axis.ticklabel_format(axis="y", style="plain", useOffset=False)
        for axis in axes[:-1]:
            axis.tick_params(axis="x", which="both", labelbottom=False)
        axes[-1].set_xlabel("Date (UTC)")
        axes[-1].xaxis.set_major_formatter(DateFormatter("%Y-%m-%d", tz=timezone.utc))
        for tick in axes[-1].get_xticklabels(): tick.set_rotation(90); tick.set_ha("center")
        export_figure.suptitle(f"Data Tilt dan Suhu Stasiun {sensor['station']}\nPeriode {start:%Y-%m-%d %H:%M} s.d. {end:%Y-%m-%d %H:%M} UTC",
                               fontsize=11, fontweight="bold")
        try:
            export_figure.savefig(destination, dpi=150, format="jpeg" if suffix in (".jpg", ".jpeg") else "png",
                                  bbox_inches="tight", facecolor="white")
            self.graph_status.set(f"Grafik disimpan: {Path(destination).name}")
            messagebox.showinfo("Simpan grafik", f"Grafik berhasil disimpan ke:\n{destination}")
        except OSError as exc:
            messagebox.showerror("Gagal menyimpan grafik", str(exc))
    def import_old_data(self):
        if not self.editable(): return
        single_file = messagebox.askyesno("Impor data lama",
            "Impor satu file CSV saja? Pilih Tidak untuk mengimpor satu folder beserta subfoldernya.")
        if single_file:
            source = filedialog.askopenfilename(title="Pilih file CSV lama",
                filetypes=(("CSV UTF-8", "*.csv"), ("Semua file", "*.*")))
            confirm_text = "File CSV yang dipilih akan diimpor. Lanjutkan?"
        else:
            source = filedialog.askdirectory(title="Pilih folder data lama")
            confirm_text = "Semua CSV format Tilty di dalam folder ini dan subfoldernya akan diperiksa. Lanjutkan?"
        if not source or not messagebox.askyesno("Impor data lama", confirm_text):
            return
        self.graph_status.set("Mengimpor data lama…")
        sensors = [sensor.copy() for sensor in self.config_data["sensors"]]
        data_dir = self.config_data["data_dir"]
        def run_import():
            try:
                result = import_legacy_csv(data_dir, source, sensors)
                self.events.put(("import_done", "", result))
            except (OSError, sqlite3.Error) as exc:
                self.events.put(("import_error", "", str(exc)))
        threading.Thread(target=run_import, name="Tilty-Legacy-Import", daemon=True).start()

    def export_data(self):
        sensor = self.selected()
        if not sensor:
            messagebox.showinfo("Ekspor data", "Pilih satu stasiun terlebih dahulu.")
            return
        label = self.range_var.get()
        days = self.RANGE_DAYS.get(label, 1)
        if label == "Real-time":
            if not messagebox.askyesno("Ekspor data", "Rentang grafik masih Real-time. Ekspor data 1 hari terakhir?"):
                return
            label = "1 Hari"
        destination = filedialog.asksaveasfilename(
            title="Ekspor data CSV", defaultextension=".csv",
            filetypes=(("CSV UTF-8", "*.csv"), ("Semua file", "*.*")),
            initialfile=f"{safe_station_name(sensor['station'])}_{label.replace(' ', '_')}_{utc_now():%Y%m%d}.csv")
        if not destination: return
        try:
            since = utc_now() - timedelta(days=days)
            rows = query_export_rows(self.config_data["data_dir"], sensor["id"], since)
            if not rows:
                messagebox.showinfo("Ekspor data", f"Tidak ada data {sensor['station']} untuk rentang {label}.")
                return
            with Path(destination).open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(("timestamp_utc", "station", "x_urad", "y_urad", "temperature_c", "status", "sample_count"))
                writer.writerows(rows)
            self.graph_status.set(f"Ekspor selesai: {len(rows):,} baris")
            messagebox.showinfo("Ekspor selesai", f"{len(rows):,} baris data disimpan ke:\n{destination}")
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("Ekspor gagal", str(exc))
    def open_folder(self):
        path = Path(self.config_data["data_dir"]); path.mkdir(parents=True, exist_ok=True); os.startfile(path)

    def on_close(self): self.stop(); self.destroy()


if __name__ == "__main__":
    MonitorApp().mainloop()
