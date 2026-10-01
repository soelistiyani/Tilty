# Tilty

**Tilty** adalah aplikasi Windows untuk menerima data tiltmeter Jewell D701 dari beberapa Moxa NPort 5110 dalam TCP Server Mode. Setiap stasiun memiliki thread, socket, buffer, parser, agregator, dan file sendiri sehingga gangguan satu stasiun tidak mencampur atau menghentikan stasiun lain.

## Fitur

- Daftar stasiun dinamis: tambah, edit, hapus, dan aktif/nonaktif.
- Wizard metadata stasiun bertahap dengan validasi, ringkasan, dan penyimpanan draf.
- Akuisisi dapat dimulai/dihentikan untuk stasiun yang dipilih atau semua stasiun sekaligus.
- Reconnect otomatis per stasiun.
- Watchdog tanpa data, TCP keepalive, panel aktivitas penerimaan, dan log diagnosis per stasiun.
- Grafik real-time dan historis X, Y, dan temperatur.
- Ringkasan status koneksi terkini serta status detail per stasiun.
- Ekspor data historis stasiun ke CSV UTF-8.
- Penyimpanan grafik berdasarkan rentang waktu pilihan pengguna ke PNG atau JPG.
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

Grafik ditampilkan sebagai tiga panel vertikal untuk X, Y, dan temperatur. Gunakan toolbar navigasi di bawah grafik untuk **Zoom**, **Pan**, dan **Home/Reset** tampilan. Gunakan menu **Grafik > Simpan grafik…** atau tombol **Simpan grafik** untuk membuat gambar. Masukkan waktu mulai dan selesai dalam format `YYYY-MM-DD HH:MM` (UTC), kemudian pilih format PNG, JPG, atau JPEG. File gambar dibuat pada 150 DPI.

## Ekspor data

Pilih stasiun dan rentang grafik, lalu gunakan **Pengaturan > Ekspor data CSV…** atau tombol **Ekspor CSV**. Pilihan historis yang tersedia adalah 1 hari, 1 minggu, 1 bulan, dan 3 bulan. Jika tampilan masih Real-time, aplikasi menawarkan ekspor data 1 hari terakhir. Hasil ekspor berformat CSV UTF-8 dan tidak terkena downsampling grafik.

## Impor data lama

Gunakan **Pengaturan > Impor data lama…**, lalu pilih **satu file CSV** atau **folder** yang berisi file CSV lama berformat Tilty. Jika memilih folder, semua subfolder akan dipindai. Nama stasiun pada CSV harus cocok dengan nama stasiun yang dikonfigurasi di aplikasi. Data valid dimasukkan ke `d701_history.sqlite3` sehingga dapat ditampilkan pada grafik dan diekspor kembali. Data dengan kombinasi stasiun dan timestamp yang sudah ada dianggap duplikat dan tidak dimasukkan dua kali. Setelah selesai, aplikasi menampilkan jumlah file, data baru, duplikat, baris rusak, dan stasiun yang tidak dikenal.
## Status koneksi

Bagian kanan header menampilkan ringkasan jumlah stasiun yang **terhubung**, **menghubungkan**, dan **berhenti**. Kolom **Status** pada tabel menampilkan keadaan terkini masing-masing stasiun, termasuk proses koneksi, reconnect, atau pesan koneksi terputus.

## Mulai otomatis saat Windows menyala

Tilty otomatis memulai semua stasiun yang berstatus aktif saat aplikasi dibuka. Untuk menjalankan Tilty setelah PC restart atau listrik kembali, buat shortcut `Tilty.exe` di folder Startup Windows:

1. Tekan `Win+R`.
2. Ketik `shell:startup`, lalu tekan Enter.
3. Salin shortcut Tilty ke folder tersebut.

Jika NPort atau jaringan belum siap, worker akan mencoba terhubung kembali dengan jeda 2, 4, 8, 16, hingga maksimum 30 detik.
## Auto reconnect

Monitoring data memakai waktu monotonic: peringatan setelah 30 detik tanpa byte dan reconnect setelah 120 detik tanpa byte. TCP keepalive diaktifkan. Byte yang terus masuk tanpa sampel valid memunculkan peringatan format/buffer tanpa reconnect otomatis.

Panel diagnosis menampilkan waktu byte, baris lengkap, dan sampel valid terakhir (UTC), jumlah byte, baris rusak, ukuran buffer, reconnect, serta keadaan worker. Penghitung dimulai ulang saat worker baru dimulai. Log koneksi, kesehatan data, dan error disimpan di `NAMA_STASIUN/diagnostics/YYYY/YYYY-MM-DD_diagnostics.jsonl`.

Batas dapat diatur per stasiun dalam konfigurasi aktif `%LOCALAPPDATA%\Tilty\config.json`: `data_warning_seconds` (30) dan `data_reconnect_seconds` (120). Tutup aplikasi sebelum mengedit. Harus 0 < peringatan < reconnect; sesuaikan dengan interval normal sensor. Reconnect tidak mengembalikan data yang sudah hilang.

Contoh field tambahan pada objek stasiun dalam array `sensors`:

```json
"data_warning_seconds": 30,
"data_reconnect_seconds": 120
```

Konfigurasi lama otomatis memakai batas default. Watchdog menggunakan waktu monotonic, sehingga perubahan jam komputer tidak mengubah perhitungan durasi tanpa byte. Jeda reconnect kembali ke 2 detik setelah sampel valid diterima; koneksi TCP yang berhasil dibuka tetapi tetap diam tidak mereset jeda.

## Diagnosis data bolong

Ping yang berhasil hanya menunjukkan NPort merespons jaringan pada saat pemeriksaan. Sensor, jalur serial, dan sesi TCP tetap perlu diperiksa. Klik stasiun untuk melihat panel diagnosis di bawah tabel.

| Status atau indikator | Arti dan langkah pemeriksaan |
| --- | --- |
| TCP tersambung, menunggu data | Socket terbuka, tetapi sesi ini belum menerima sampel valid. |
| TCP tersambung, tanpa byte | Tidak ada byte masuk melewati batas peringatan; watchdog akan reconnect ketika batas reconnect tercapai. Periksa aktivitas RX serial dan koneksi aktif NPort. |
| Byte masuk, belum ada sampel valid | Periksa format payload, pemisah baris CR/LF, pengaturan serial, dan data packing NPort. |
| Buffer bertambah, waktu baris tidak berubah | Byte masuk belum membentuk baris lengkap. Buffer melebihi 64 KiB dibuang dan dicatat sebagai error. |
| Worker berhenti tidak terduga | Worker sudah tidak berjalan; periksa log diagnosis, lalu hentikan dan mulai kembali stasiun tersebut. |

Waktu byte, baris, dan sampel valid terakhir ditampilkan dalam UTC. Nilai terakhir tetap terlihat ketika koneksi terputus; periksa status koneksi dan waktu tersebut bersama-sama. Jumlah reconnect mencatat gangguan yang memicu percobaan ulang, bukan jumlah koneksi ulang yang berhasil.

Setiap baris log diagnosis adalah satu objek JSON dengan waktu UTC, identitas stasiun, endpoint, jenis kejadian, pesan, dan kondisi penerimaan. Jenis kejadian meliputi `connected`, `disconnected`, `data_health`, `error`, `worker_dead`, dan `stopped`. Log mencatat transisi kesehatan, bukan setiap paket; payload byte parsial tidak disimpan di log diagnosis. Jika penulisan log gagal, pesan ditampilkan di aplikasi.

Raw log baru ditulis setelah menerima baris lengkap yang tidak kosong, sebelum parsing. Karena itu raw kosong belum membuktikan tidak ada byte masuk. Bandingkan waktu byte terakhir dengan waktu baris terakhir. Error akses raw/CSV dapat memicu reconnect; error SQLite dicatat sementara CSV tetap menjadi keluaran utama.

Saat gangguan terjadi, catat waktu dan indikator diagnosis, periksa RX serial serta client aktif di NPort, lalu bila perlu capture trafik secara pasif dengan Wireshark. Hindari membuka client TCP tambahan sebelum memeriksa batas `Max connection`. Monitoring ini tidak melakukan ping otomatis atau mengubah konfigurasi NPort.

Reconnect selalu aktif dan independen untuk setiap stasiun. Jeda dimulai dari 2 detik, lalu 4, 8, 16, hingga maksimum 30 detik. Setelah sampel valid diterima, jeda kembali menjadi 2 detik.

## Tentang

```text
Tilty: Tiltmeter TCP Monitor
Versi: 1.7.0

Oleh: Sulistiyani
Tahun: 2026
```

## Metadata stasiun

Setiap baris pada tabel mewakili satu stasiun. Klik **Tambah** untuk mengisi koneksi TCP; setelah koneksi disimpan, wizard metadata untuk stasiun baru terbuka otomatis. Untuk stasiun yang sudah ada, pilih barisnya lalu klik **Metadata** atau gunakan menu **Metadata**. Form dibagi menjadi lima langkah agar informasi teknis tidak tampil sekaligus:

1. **Stasiun**: nama, kode, tanggal mulai/selesai, dan jenis lokasi.
2. **Lokasi**: koordinat, elevasi, datum, kedalaman, dan orientasi sensor.
3. **Perangkat**: merek, model, serial number, firmware, konfigurasi internal (`N_SAMP`, gain, filter), dan output unit.
4. **Akuisisi**: identitas data logger, sumber timestamp, timezone, dan sinkronisasi waktu.
5. **Tinjau**: ringkasan sebelum metadata disimpan.

Gunakan **Simpan draf** jika metadata belum lengkap. Tombol **Simpan metadata** pada langkah terakhir memeriksa field utama, format tanggal `YYYY-MM-DD`, rentang koordinat, dan azimuth. Untuk arah positif, gunakan keterangan eksplisit, misalnya `+X menuju puncak (azimuth 315°); +Y 90° searah jarum jam dari +X`.

Metadata disimpan bersama konfigurasi sensor di `%LOCALAPPDATA%\Tilty\config.json`. Setiap penyimpanan juga membuat:

- `NAMA_STASIUN\NAMA_STASIUN_metadata.txt`: snapshot metadata terbaru.
- `NAMA_STASIUN\NAMA_STASIUN_metadata_history.log`: riwayat append-only setiap perubahan metadata.

Contoh untuk stasiun **Wolorona**:

```text
Documents\Tilty Data\Wolorona\Wolorona_metadata.txt
Documents\Tilty Data\Wolorona\Wolorona_metadata_history.log
```

Spasi dan karakter yang tidak aman pada nama stasiun diganti dengan garis bawah. Contohnya, stasiun `Lewotobi Barat` menghasilkan `Lewotobi_Barat_metadata.txt`.

## Kompatibilitas dan upgrade

Penambahan metadata tidak mengubah skema `d701_history.sqlite3`. Database SQLite, CSV, raw log, konfigurasi koneksi, dan kalibrasi dari versi lama tetap dapat digunakan. Konfigurasi lama otomatis mendapatkan struktur metadata kosong saat dibaca.

Untuk memperbarui instalasi lama:

1. Klik **Hentikan semua**, lalu tutup Tilty.
2. Cadangkan `%LOCALAPPDATA%\Tilty\config.json` dan seluruh folder data, biasanya `Documents\Tilty Data`.
3. Jalankan installer baru di lokasi instalasi yang sama. Tidak perlu menghapus versi lama terlebih dahulu.
4. Buka Tilty dan periksa daftar stasiun, folder penyimpanan, serta grafik historis.
5. Pilih setiap stasiun dan lengkapi metadata baru secara bertahap.

Mengganti nama stasiun tidak memutus riwayat SQLite karena data dicari menggunakan ID stasiun. Namun, raw log dan CSV baru akan menggunakan folder berdasarkan nama stasiun yang baru.

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

Untuk membuat rilis `1.7.0`, pastikan versi pada `installer.iss`:

```iss
#define MyAppVersion "1.7.0"
```

Install Inno Setup 6, kemudian jalankan PowerShell:

```powershell
.\build_installer.ps1
```

Hasilnya berada di `output\Tilty-Setup.exe`.
