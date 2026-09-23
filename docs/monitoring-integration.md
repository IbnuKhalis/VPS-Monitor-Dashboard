# Rencana integrasi Uptime Kuma dan VPS Monitor Dashboard

Status: arahan implementasi untuk Antigravity, 2026-09-23. Belum ada perubahan aplikasi atau konfigurasi produksi dari dokumen ini.

## Keputusan arsitektur

Pertahankan Uptime Kuma dan VPS Monitor Dashboard sebagai dua layanan di proxy-network. Uptime Kuma menjadi sumber data ketersediaan layanan, riwayat uptime, dan halaman status publik. VPS Monitor Dashboard tetap menjadi ruang privat untuk metrik host, Docker, log, dan backup. Satukan pengalaman pengguna dengan tautan dan ringkasan status Kuma di dashboard privat. Jangan memindahkan monitor Kuma ke kode dashboard atau menampilkan log serta rincian infrastruktur pada halaman publik.

Pemisahan ini sesuai dengan catatan kanonikal Second Brain: D:/Apps/Obsidian/SecondBrain/01 Projects/Antigravity/VPS-Monitor-Dashboard/Overview.md (bagian Ringkasan Proyek) dan D:/Apps/Obsidian/SecondBrain/03 Resources/VPS Infrastructure/2. Architecture & Network Flow.md. Struktur produksi yang dicatat menggunakan Caddy, jaringan Docker proxy-network, dan dua kontainer terpisah.

## Hasil penyelidikan rute saat ini

Pemeriksaan HTTP tanpa login pada 2026-09-23:

| Permintaan | Hasil teramati | Arti yang dapat dipastikan |
| --- | --- | --- |
| GET https://status.digitalneeds.my.id/ | 302, Location: /dashboard | Akar domain saat ini mengarahkan pengunjung ke rute dashboard Kuma. |
| GET https://status.digitalneeds.my.id/dashboard | 200, HTML shell berjudul Uptime Kuma | Rute ada; respons HTML saja belum membuktikan isi setelah JavaScript berjalan atau status login. |
| GET https://status.digitalneeds.my.id/status dan /status/default | 404 | Halaman status pada dua rute tersebut belum tersedia untuk pengunjung tanpa login. |
| GET https://status.digitalneeds.my.id/api/entry-page | 200, {"type":"entryPage","entryPage":null} | Tidak ada halaman status yang terpilih sebagai entry page dalam respons publik ini. |

Catatan Second Brain pada D:/Apps/Obsidian/SecondBrain/03 Resources/VPS Infrastructure/1. Overview & System Specs.md juga mencatat redirect 302 ke /dashboard, tetapi menyebut domain ini sebagai public status page. Dengan bukti saat ini, fungsi public status page di akar domain belum terverifikasi. Masih mungkin ada halaman terbit dengan slug lain; daftar slug dan pengaturan akses perlu diperiksa di admin Kuma. Percobaan GET /api/status-page/default mengalami timeout, sehingga hasil endpoint itu tidak dipakai sebagai dasar kesimpulan.

## Urutan kerja untuk Antigravity

### 1. Inventaris tanpa mengubah produksi

- Periksa versi Uptime Kuma, daftar status page beserta slug dan status publikasinya, pilihan Entry Page, domain mapping, monitor yang ditampilkan, serta notifikasi. Gunakan UI admin atau akses server baca saja. Catat hasil tanpa menyalin kredensial, token, atau URL monitor yang bersifat privat ke repo.
- Cocokkan daftar layanan yang pantas diumumkan dengan pemilik. Nama kontainer, log, endpoint internal, dan status backup tidak otomatis layak dipublikasikan.
- Catat URL halaman status yang benar jika sudah ada. Jangan menebak slug dari /status/default.

### 2. Jadikan domain status sebuah halaman status publik

- Gunakan status page yang sudah ada bila sesuai. Jika belum ada, buat dan terbitkan satu halaman berisi monitor layanan yang aman untuk publik.
- Atur domain status.digitalneeds.my.id pada pengaturan domain status page Kuma atau pilih status page sebagai Entry Page. Pertahankan Caddy sebagai reverse proxy dan port Kuma tetap internal; hindari redirect buatan di Caddy sebelum pengaturan native Kuma diperiksa.
- Uji melalui browser tanpa sesi login: akar domain menampilkan status layanan, /status/{slug} tersedia, dan /dashboard tetap meminta autentikasi admin Kuma.
- Periksa hasil GET /api/entry-page dan GET /api/status-page/{slug} setelah publikasi. Endpoint status page harus hanya memuat informasi yang memang sengaja dipublikasikan.

### 3. Tambahkan ringkasan ke dashboard privat

- Perbarui pintasan Kuma pada app/templates/index.html agar menuju halaman status publik yang benar. Untuk direktori kesembilan domain, ikuti docs/service-shortcuts.md.
- Tambahkan kartu ringkas: status keseluruhan, layanan yang terganggu, waktu pembaruan, dan tautan ke halaman Kuma. Tampilkan keadaan "Tidak diketahui" ketika sumber data gagal atau terlalu lama, bukan "Sehat".
- Ambil data dari halaman status yang diterbitkan, bukan API admin atau basis data Kuma. Pilihan implementasi: backend FastAPI membaca GET /api/status-page/{slug} dan GET /api/status-page/heartbeat/{slug} melalui alamat internal tetap, misalnya http://uptime-kuma:3001 pada proxy-network. Slug berasal dari konfigurasi deployment, bukan input pengunjung. Gunakan timeout singkat, cache sekitar 30-60 detik, validasi bentuk respons, dan respons yang aman saat Kuma tidak tersedia.
- Jangan mengubah autentikasi dashboard atau menampilkan telemetri privat melalui endpoint status baru. Jangan memakai iframe Kuma: respons saat ini memasang X-Frame-Options: SAMEORIGIN.
- API publik status page didokumentasikan di wiki Kuma tetapi API internalnya tidak dijamin stabil untuk integrasi pihak ketiga. Catat versi Kuma dan buat pemeriksaan kontrak saat upgrade. Bila data API tidak konsisten, pertahankan tautan saja sampai adaptornya aman.

### 4. Tambahkan pemeriksaan dari luar VPS

Kuma dan dashboard berjalan pada VPS yang sama. Kegagalan total VPS, jaringan, atau Caddy dapat membuat keduanya tidak dapat diakses. Untuk deteksi dan notifikasi pada kondisi itu, gunakan probe dari lokasi lain yang memeriksa domain publik. Tentukan penyedia dan saluran notifikasi secara terpisah; jangan memasang probe kedua di VPS yang sama.

## Kriteria selesai

- Browser tanpa login membuka status.digitalneeds.my.id dan melihat status layanan, bukan rute dashboard admin.
- /dashboard tetap memerlukan autentikasi Kuma; halaman publik tidak menampilkan log, metrik host terperinci, endpoint internal, atau rahasia.
- Ringkasan di dashboard privat cocok dengan status Kuma dan menampilkan "Tidak diketahui" saat Kuma gagal dihubungi atau respons kedaluwarsa.
- Tidak ada monitor ganda yang harus dipelihara di dua aplikasi. Kegagalan pembacaan Kuma tidak menghambat metrik, Docker, atau backup pada dashboard privat.
- Pengujian mencakup respons normal, satu layanan down, timeout/404, dan perubahan bentuk respons Kuma. Verifikasi rute publik dilakukan lagi setelah deployment.
- Catatan Second Brain tentang public status page diperbarui sesuai kondisi yang benar-benar terverifikasi.

## Rujukan eksternal

- Uptime Kuma Status Page: https://github.com/louislam/uptime-kuma/wiki/Status-Page
- Uptime Kuma API Documentation (API internal dapat berubah): https://github.com/louislam/uptime-kuma/wiki/API-Documentation

## Status Implementasi & Hasil Verifikasi

1. **Konfigurasi Uptime Kuma (`1.23.17`) pada `vps-main`**:
   - Halaman status publik `default` (`title: "DigitalNeeds System Status"`) telah dipublikasikan dengan pemetaan domain `status.digitalneeds.my.id` (`status_page_cname`) dan pengaturan `entryPage: "statusPage-default"`.
   - Grup publik terdiri atas **Situs & Aplikasi Publik** (`Main Domain`, `Graduance`, `ATS CV Builder` dengan `sendUrl=1`) dan **Layanan Operasional** (`Mission Control Dashboard` dengan `sendUrl=0`).
2. **Hasil Pemeriksaan HTTP Tanpa Login**:
   - `GET https://status.digitalneeds.my.id/` -> `200 OK` (menampilkan halaman status publik tanpa pengalihan `302` ke `/dashboard`).
   - `GET https://status.digitalneeds.my.id/status` dan `/status/default` -> `200 OK`.
   - `GET https://status.digitalneeds.my.id/api/entry-page` -> `200 OK` (`{"type":"statusPageMatchedDomain","statusPageSlug":"default"}`).
   - `GET https://status.digitalneeds.my.id/api/status-page/default` dan `/api/status-page/heartbeat/default` -> `200 OK` JSON publik.
   - `GET https://status.digitalneeds.my.id/dashboard` -> tetap menjadi rute dashboard admin yang memerlukan login Uptime Kuma.
3. **Integrasi Dashboard Privat (`app/services/kuma_service.py`)**:
   - Endpoint `/api/kuma-summary` (dilindungi `require_auth`) mengambil data dari `http://uptime-kuma:3001` (dengan fallback `https://status.digitalneeds.my.id`), batas waktu `3.0` detik, cache `45` detik, dan menghasilkan status `UNKNOWN` (`"Tidak diketahui"`) bila koneksi gagal atau struktur respons berubah.
   - Diuji melalui `tests/test_dashboard.py` (`python -m unittest discover -s tests -v`).
