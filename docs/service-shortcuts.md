# Arahan fitur pintasan seluruh domain

Status: arahan implementasi untuk Antigravity, 2026-09-24. Cakupan: direktori tautan pada VPS Monitor Dashboard privat; belum mengubah aplikasi atau konfigurasi VPS.

## Tujuan dan keputusan UX

Tambahkan pintasan untuk setiap domain kanonis yang tercatat aktif di Second Brain. Semua pintasan tersedia setelah pengguna masuk ke VPS Monitor Dashboard. Pertahankan tiga tautan cepat yang sudah ada pada area utama, lalu sediakan bagian "Semua layanan" agar sembilan layanan dapat ditemukan tanpa memenuhi bagian atas layar. Kelompokkan layanan berdasarkan tujuan. Domain alias www.digitalneeds.my.id tidak perlu menjadi kartu kedua karena menuju layanan yang sama dengan domain utama.

Tautan adalah navigasi, bukan indikator kesehatan. Jangan memberi badge "Online" hanya karena URL tercantum atau karena respons HTTP sesaat. Ringkasan uptime yang direncanakan pada docs/monitoring-integration.md tetap berasal dari Uptime Kuma.

Halaman / saat ini mengirim template HTML juga kepada pengunjung sebelum PIN dimasukkan. Menyembunyikan daftar dengan Alpine x-show saja tidak menjaga daftar domain tetap privat. Jika direktori layanan ini dimaksudkan hanya untuk pemilik, kirim katalog lewat endpoint yang memakai require_auth setelah login, atau render server-side hanya untuk sesi valid. Jangan taruh daftar lengkap di HTML respons anonim.

## Inventaris tautan

Sumber kanonikal: D:/Apps/Obsidian/SecondBrain/03 Resources/VPS Infrastructure/1. Overview & System Specs.md, bagian "Alokasi Domain & Subdomain Aktif" (diperbarui 2026-09-23). Respons akar domain diperiksa tanpa login pada 2026-09-24. Kode 401 pada layanan berautentikasi adalah respons yang diharapkan, bukan bukti layanan mati; kode 200 saja juga belum membuktikan seluruh fitur sehat.

| Kelompok | Nama kartu | URL kanonis | Jenis akses dan respons akar saat diperiksa | Perlakuan di dashboard |
| --- | --- | --- | --- | --- |
| Situs dan aplikasi | DigitalNeeds | https://digitalneeds.my.id | Halaman publik, 200 | Tautan utama. |
| Situs dan aplikasi | Graduance | https://graduance.digitalneeds.my.id | Aplikasi web, 200 | Tautan utama. |
| Situs dan aplikasi | CV Builder | https://cv.digitalneeds.my.id | Aplikasi web, 200 | Tampilkan di semua layanan. |
| Operasional | Uptime Kuma | https://status.digitalneeds.my.id | Akar 302 ke /dashboard | Tautan utama; beri label rute admin saat ini. Setelah halaman status publik diperbaiki, arahkan ke URL status yang terverifikasi. |
| Operasional | Dashboard ini | https://dashboard.digitalneeds.my.id | Dashboard privat, 200 | Tandai sebagai halaman saat ini; tautan tetap tersedia untuk konsistensi inventaris. |
| Operasional | 9Router | https://router.digitalneeds.my.id | Akar 307 ke /dashboard | Label sebagai dashboard layanan yang mungkin meminta login. |
| Operasional | Vaultwarden | https://vault.digitalneeds.my.id | Pengelola kata sandi, 200 | Tautan ke aplikasi, tanpa membawa kredensial dari dashboard. |
| Integrasi dan API | Obsidian Sync | https://sync.digitalneeds.my.id | CouchDB, 401 Auth | Label sebagai endpoint sinkronisasi/API; membuka URL mungkin menampilkan respons autentikasi, bukan antarmuka manusia. |
| Integrasi dan API | Second Brain Gateway | https://brain.digitalneeds.my.id | Gateway API, 200 | Label sebagai API; jangan menganggap akar domain sebagai halaman penggunaan utama. |

## Langkah implementasi untuk Antigravity

1. Cocokkan kembali inventaris di atas dengan Second Brain dan konfigurasi Caddy sebelum mengubah kode. Jika ada layanan baru atau pensiun, perbarui sumber kanonikal serta daftar pintasan pada perubahan yang sama.
2. Buat satu katalog data tautan dengan id stabil, nama, URL HTTPS, kategori, deskripsi singkat, dan jenis akses (web, admin, API). Hindari daftar URL yang digandakan di beberapa komponen. Seluruh URL harus tetap dan berasal dari konfigurasi tepercaya, bukan input pengunjung. Sajikan daftar lengkap hanya sesudah autentikasi melalui endpoint terlindungi atau render server-side yang memeriksa sesi.
3. Kembangkan bagian "Infrastructure Quick Access Bar" di app/templates/index.html. Tiga tautan utama tetap cepat terlihat; bagian "Semua layanan" menampilkan kesembilan entri dalam kelompok yang bisa dipindai pada desktop dan ponsel. Gunakan elemen tautan semantik, fokus keyboard yang tampak, dan target sentuh minimal 44 x 44 piksel.
4. Buka domain eksternal dengan HTTPS pada tab baru dan rel="noopener noreferrer". Jangan menambahkan token, PIN, atau kredensial dalam URL. Untuk "Dashboard ini", tampilkan penanda halaman aktif agar klik yang kembali ke halaman yang sama tidak membingungkan.
5. Gunakan label dan deskripsi jujur untuk layanan API/admin. Jangan melakukan polling ke kesembilan domain hanya untuk mengisi kartu pintasan; status layanan akan diambil terpisah dari Kuma sesuai rencana integrasi.
6. Sesuaikan URL kartu Uptime Kuma setelah rute status publik benar-benar diverifikasi. Sampai saat itu, jangan menyebut tautan akar status sebagai "halaman status publik" di UI.

## Kriteria selesai

- Semua 9 domain kanonis pada tabel dapat ditemukan sesudah login, tidak muncul di respons HTML anonim, dan alias www tidak menjadi duplikat.
- Link, label, dan kategori cocok dengan tujuan masing-masing layanan; pengguna mendapat petunjuk saat sebuah tujuan adalah API atau halaman yang memerlukan login.
- Tampilan tetap nyaman pada lebar ponsel, tombol mudah disentuh, dan seluruh tautan bisa dicapai lewat keyboard.
- Semua tautan memakai HTTPS, dapat dibuka tanpa mengirim rahasia dari dashboard, dan tetap berfungsi ketika layanan tujuan meminta autentikasi.
- Tidak ada badge kesehatan palsu; bila ringkasan Kuma ditambahkan, kegagalannya tidak menghilangkan direktori pintasan.
- Pengujian memeriksa jumlah entri, URL kanonis, tidak adanya duplikat, atribut keamanan tautan, dan perilaku rute status setelah perbaikan Kuma.

## Hubungan dengan rencana monitoring

Laksanakan bersama docs/monitoring-integration.md. Direktori tautan dapat dikerjakan lebih dulu, tetapi perubahan label dan URL Uptime Kuma harus mengikuti hasil verifikasi halaman status publik. Pemeriksaan dari luar VPS tetap diperlukan untuk mendeteksi kegagalan total host.

## Status Implementasi & Hasil Verifikasi

1. **Katalog 9 Domain Kanonik (`app/services/service_catalog.py`)**:
   - Seluruh 9 domain (`digitalneeds.my.id`, `graduance.digitalneeds.my.id`, `cv.digitalneeds.my.id`, `status.digitalneeds.my.id`, `dashboard.digitalneeds.my.id`, `router.digitalneeds.my.id`, `vault.digitalneeds.my.id`, `sync.digitalneeds.my.id`, `brain.digitalneeds.my.id`) telah dikelompokkan ke dalam `Situs dan aplikasi`, `Operasional`, dan `Integrasi dan API` tanpa duplikasi `www.`.
   - `dashboard.digitalneeds.my.id` ditandai sebagai halaman aktif (`Dashboard ini` / `is_current=True`).
   - Seluruh tautan eksternal memiliki atribut `target="_blank" rel="noopener noreferrer"`, target sentuh `min-h-[48px]`, dan label jenis akses jujur tanpa indikator status palsu.
2. **Sanitasi Privasi Pengguna Anonim (`app/main.py` & `app/templates/index.html`)**:
   - Respons `GET /` sebelum autentikasi PIN hanya merender layar kunci PIN (`{% if not is_authenticated %}`) dan tidak memuat shell dashboard maupun daftar 9 domain internal.
   - Endpoint `/api/services` dilindungi oleh `Depends(require_auth)`.
   - Diuji melalui `tests/test_dashboard.py` (`TestServiceCatalog` & `TestDashboardPrivacyAndRoutes`).
