# Tilty

**Tilty** adalah aplikasi Windows untuk menerima data tiltmeter Jewell D701 dari beberapa Moxa NPort 5110 dalam TCP Server Mode. Setiap stasiun memiliki thread, socket, buffer, parser, agregator, dan file sendiri sehingga gangguan satu stasiun tidak mencampur atau menghentikan stasiun lain.

## Fitur

- Daftar stasiun dinamis: tambah, edit, hapus, dan aktif/nonaktif.
- Akuisisi dapat dimulai/dihentikan untuk stasiun yang dipilih atau semua stasiun sekaligus.
- Reconnect otomatis per stasiun.
- Grafik real-time dan historis X, Y, dan temperatur.
- Menu kalibrasi per stasiun.
- Raw log dan rata-rata per menit dalam CSV.
- SQLite sebagai indeks internal untuk grafik historis.
- Identitas aplikasi, executable, shortcut, dan installer menggunakan nama **Tilty** dan ikon maskot Tilty.
- Konfigurasi disimpan di `%LOCALAPPDATA%\Tilty\config.json`.
- Folder data default: `Documents\Tilty Data`.

## Kalibrasi dan satuan

D701 mengirim tilt dalam derajat. Aplikasi mengubah setiap sumbu menjadi mikroradian:

```text
tilt_urad = (tilt_derajat - zero_offset_derajat) * faktor_urad_per_derajat
```

Faktor standar adalah `π / 180 × 1.000.000 = 17453,2925199433 µrad/°`. Zero dan faktor X/Y dapat diatur melalui tombol atau menu **Kalibrasi** sebelum akuisisi dimulai.

## Keluaran

```text
Documents\Tilty Data\NAMA_STASIUN\
  raw\2026\2026-09-08_NAMA_STASIUN_raw.log
  minute\2026\2026-09-08_NAMA_STASIUN.csv
```

Setiap stasiun menghasilkan satu file CSV per tanggal UTC. CSV berisi `timestamp_utc, station, x_urad, y_urad, temperature_c, status, sample_count`. `sample_count` menunjukkan jumlah sampel valid pembentuk rata-rata satu menit.

Untuk menjalankan satu stasiun, klik baris stasiunnya lalu pilih **Mulai dipilih**. Tombol **Hentikan dipilih** hanya menghentikan stasiun tersebut. **Mulai semua** menjalankan seluruh stasiun aktif yang belum berjalan, sedangkan **Hentikan semua** menghentikan seluruh worker.

## Timestamp dan setting waktu PC

D701 tidak mengirim timestamp. Tilty menggunakan jam komputer ketika satu baris data lengkap diterima, kemudian menyimpan timestamp data dalam UTC. Karena itu sinkronisasi waktu otomatis/NTP pada Windows harus diaktifkan.

Tilty juga membuat file terpisah `pc_time_settings.csv` di folder data. Kolomnya adalah:

```text
observed_at_utc,local_date,local_time,windows_timezone,timezone_name,utc_offset,dst_active
```

Aplikasi memeriksa setting waktu PC setiap 60 detik. Satu record ditulis saat aplikasi pertama kali berjalan pada suatu hari. Jika zona waktu, nama zona, offset UTC, atau status daylight-saving berubah pada hari yang sama, record baru ditambahkan. Record historis tidak ditimpa. Contoh offset Indonesia barat adalah `UTC+07:00`; PC yang disetel ke GMT/UTC tercatat sebagai `UTC+00:00`.

File `d701_history.sqlite3` berada langsung di folder data. Database ini dipakai aplikasi untuk mengambil grafik historis dengan cepat. CSV tetap merupakan keluaran utama dan dapat digunakan untuk pengolahan di aplikasi lain.

## Grafik

Klik baris stasiun yang ingin dilihat, lalu pilih rentang:

- **Real-time**: maksimum 600 sampel terbaru dari memori.
- **1 Hari**
- **1 Minggu**
- **1 Bulan**
- **3 Bulan**

Data historis berasal dari rata-rata per menit di SQLite. Query dijalankan di background agar koneksi TCP tidak terhambat. Jika hasil melebihi 1.500 titik, aplikasi melakukan downsampling sebelum menggambar. Gunakan tombol **Muat ulang** untuk mengambil data terbaru. Data lebih lama dari tiga bulan tetap ada di CSV dan SQLite, tetapi tidak ditawarkan pada grafik agar tampilan tetap ringan.

## Auto reconnect

Reconnect selalu aktif dan independen untuk setiap stasiun. Jeda dimulai dari 2 detik, lalu 4, 8, 16, hingga maksimum 30 detik. Setelah koneksi berhasil, jeda kembali menjadi 2 detik.

## Tentang

Tilty: Tiltmeter TCP Monitor

Output tilt: microradian.

Didesain dan dikembangkan oleh Sulistiyani (`soelistiyani@gmail.com`).

## Menjalankan dari source

```bat
python -m pip install -r requirements.txt
python d701_monitor.py
```

Pengujian lokal:

```bat
python simulate_d701.py
python -m unittest -v test_d701_monitor.py
```

Simulator memakai `127.0.0.1:4001` dan `127.0.0.1:4002`.

## Membuat installer

Install Inno Setup 6, kemudian jalankan PowerShell:

```powershell
.\build_installer.ps1
```

Hasilnya berada di `output\Tilty-Setup.exe`.
