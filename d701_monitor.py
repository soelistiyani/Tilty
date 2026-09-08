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

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.dates import DateFormatter
from matplotlib.figure import Figure

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "Tilty"
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
            "zero_x_deg": 0.0, "zero_y_deg": 0.0, "factor_x": DEG_TO_URAD, "factor_y": DEG_TO_URAD}


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


def query_history(data_dir, station_id, since, max_points=1500):
    path = Path(data_dir) / "d701_history.sqlite3"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path, timeout=10)) as db:
        rows = db.execute("""SELECT timestamp_utc, x_urad, y_urad, temperature_c
            FROM minute_readings WHERE station_id = ? AND timestamp_utc >= ?
            ORDER BY timestamp_utc""", (station_id, iso_utc(since))).fetchall()
    parsed = [(datetime.fromisoformat(row[0].replace("Z", "+00:00")), row[1], row[2], row[3]) for row in rows]
    return downsample_rows(parsed, max_points)


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
        self.historical = {}; self.history_request = 0
        self.build_ui(); self.refresh_table(); self.after(100, self.process_events); self.after(1000, self.refresh_plot)
        self.after(1200, self.check_time_setting)

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
        menu = tk.Menu(self); settings = tk.Menu(menu, tearoff=False)
        settings.add_command(label="Folder penyimpanan…", command=self.choose_folder)
        settings.add_command(label="Keluar", command=self.on_close)
        menu.add_cascade(label="Pengaturan", menu=settings)
        menu.add_command(label="Kalibrasi", command=self.calibrate)
        menu.add_command(label="Tentang", command=lambda: messagebox.showinfo(
            "Tentang Tilty", "Tilty: Tiltmeter TCP Monitor\nOutput tilt: microradian.\n\n"
            "Didesain dan dikembangkan oleh Sulistiyani\nsoelistiyani@gmail.com"))
        self.configure(menu=menu)
        header = ttk.Frame(self, padding=(12, 8)); header.pack(fill="x")
        try:
            self.logo_image = tk.PhotoImage(file=str(resource_path("tilty_logo_64.png")))
            ttk.Label(header, image=self.logo_image).pack(side="left", padx=(0, 10))
        except tk.TclError: self.logo_image = None
        title_box = ttk.Frame(header); title_box.pack(side="left")
        ttk.Label(title_box, text="Tilty", font=("Segoe UI", 22, "bold")).pack(anchor="w")
        ttk.Label(title_box, text="Multi-station Tiltmeter TCP Monitor").pack(anchor="w")
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
        ttk.Button(buttons, text="Mulai dipilih", command=self.start_selected).pack(side="left", padx=(18, 6))
        ttk.Button(buttons, text="Hentikan dipilih", command=self.stop_selected).pack(side="left", padx=(0, 6))
        self.start_button = ttk.Button(buttons, text="Mulai semua", command=self.start); self.start_button.pack(side="left", padx=(8, 6))
        self.stop_button = ttk.Button(buttons, text="Hentikan semua", command=self.stop, state="disabled"); self.stop_button.pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Buka folder data", command=self.open_folder).pack(side="left")
        self.folder_text = tk.StringVar(value=self.config_data["data_dir"]); ttk.Label(panel, textvariable=self.folder_text).pack(fill="x", pady=(7, 0))
        graph_controls = ttk.Frame(self); graph_controls.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(graph_controls, text="Rentang grafik:").pack(side="left")
        self.range_var = tk.StringVar(value="Real-time")
        ranges = ttk.Combobox(graph_controls, textvariable=self.range_var, state="readonly", width=14,
                              values=("Real-time", "1 Hari", "1 Minggu", "1 Bulan", "3 Bulan"))
        ranges.pack(side="left", padx=6); ranges.bind("<<ComboboxSelected>>", lambda _e: self.load_selected_history())
        ttk.Button(graph_controls, text="Muat ulang", command=lambda: self.load_selected_history(True)).pack(side="left")
        self.graph_status = tk.StringVar(value="Menampilkan sampel real-time terakhir")
        ttk.Label(graph_controls, textvariable=self.graph_status).pack(side="left", padx=8)
        self.figure = Figure(figsize=(10, 5), dpi=100, constrained_layout=True); self.lines = []
        self.axes = [self.figure.add_subplot(311), self.figure.add_subplot(312), self.figure.add_subplot(313)]
        for axis, title, unit in zip(self.axes, ("Tilt X", "Tilt Y", "Temperatur"), ("µrad", "µrad", "°C")):
            axis.set_title(title); axis.set_ylabel(unit); axis.grid(True, alpha=.25)
            self.lines.append(axis.plot([], [], color="#2463eb", linewidth=1)[0])
        self.axes[-1].set_xlabel("Waktu UTC"); self.axes[-1].xaxis.set_major_formatter(DateFormatter("%H:%M:%S", tz=timezone.utc))
        self.canvas = FigureCanvasTkAgg(self.figure, master=self); self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(0, 10))

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

    def editable(self):
        if self.workers: messagebox.showwarning("Akuisisi berjalan", "Hentikan akuisisi sebelum mengubah konfigurasi."); return False
        return True

    def add_station(self):
        if self.editable():
            d = SensorDialog(self, "Tambah stasiun")
            if d.result: self.config_data["sensors"].append(d.result); self.save_config(); self.refresh_table()

    def edit_station(self):
        sensor = self.selected()
        if self.editable() and sensor:
            d = SensorDialog(self, "Edit stasiun", sensor)
            if d.result: sensor.update(d.result); self.save_config(); self.refresh_table()

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
                elif kind == "reading":
                    h = self.history.setdefault(sid, {k: deque(maxlen=600) for k in ("t", "x", "y", "temp")})
                    for key, value in (("t", payload.timestamp), ("x", payload.x), ("y", payload.y), ("temp", payload.temperature)): h[key].append(value)
                    self.counts[sid] = self.counts.get(sid, 0) + 1
                    self.latest[sid] = f"X={payload.x:.2f} µrad, Y={payload.y:.2f} µrad, T={payload.temperature:.2f} °C"
        except queue.Empty: pass
        if dirty: self.refresh_table()
        self.after(100, self.process_events)

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

    def refresh_plot(self):
        sensor = self.selected(); history = self.history.get(sensor["id"]) if sensor else None
        if sensor: self.figure.suptitle(sensor["station"])
        if sensor and self.range_var.get() != "Real-time":
            rows = self.historical.get((sensor["id"], self.range_var.get()), [])
            values = {"t": [r[0] for r in rows], "x": [r[1] for r in rows],
                      "y": [r[2] for r in rows], "temp": [r[3] for r in rows]}
            history = values
        for line, key in zip(self.lines, ("x", "y", "temp")): line.set_data(list(history["t"]), list(history[key])) if history else line.set_data([], [])
        for axis in self.axes: axis.relim(); axis.autoscale_view()
        self.canvas.draw_idle(); self.after(max(500, int(self.config_data.get("refresh_seconds", 2)*1000)), self.refresh_plot)

    def open_folder(self):
        path = Path(self.config_data["data_dir"]); path.mkdir(parents=True, exist_ok=True); os.startfile(path)

    def on_close(self): self.stop(); self.destroy()


if __name__ == "__main__":
    MonitorApp().mainloop()
