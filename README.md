# VPS Monitor Dashboard // Mission Control

Sistem pemantauan (*monitoring*) dan visualisasi performa infrastruktur server VPS Oracle Cloud ARM64 (`digitalneeds.my.id`) yang ringan (< 30 MB RAM), berkecepatan tinggi, dan hemat sumber daya (*zero bloatware*).

---

## 🚀 Fitur Utama

1. **Keamanan Berbasis PIN**:
   - Dilindungi oleh keypad autentikasi PIN 6-digit.
   - Sesi terlindungi via cryptographic signed HTTP-only cookie.
2. **Telemetri Sistem Real-time**:
   - Utilisasi CPU (total dan per-core 2 OCPU Ampere ARM64).
   - Breakdown Memori RAM (12 GB total, used, available, buffers/cached).
   - Utilisasi disk root boot volume (46 GB).
   - Throughput jaringan real-time (TX/RX rate dalam KB/s dan total transfer MB).
   - Uptime sistem dan sinkronisasi waktu lokal WIB (UTC+7).
3. **Grafik Visual Interaktif**:
   - Grafik riwayat utilisasi CPU dan RAM secara langsung menggunakan Chart.js.
4. **Docker Container Fleet Manager**:
   - Memantau seluruh kontainer (`caddy-proxy`, `graduance_app`, `graduance_mysql`, `uptime-kuma`, `vps-monitor-dashboard`).
   - Status kesehatan (Healthy/Unhealthy/Running), restart count, port exposure, konsumsi CPU% dan RAM MB per kontainer.
   - **Live Log Viewer**: Menampilkan log kontainer langsung (hingga 60 baris terakhir) melalui modal terminal interaktif.
5. **Audit Kepatuhan Backup SLA**:
   - Memeriksa file cadangan harian GFS di `/opt/backups/daily/`.
   - Menghitung usia backup terakhir (SLA batas aman <= 26 jam).
   - Menampilkan ringkasan ukuran dump database MySQL.
6. **Pintasan Cepat & Direktori 9 Domain Kanonik**:
   - Menampilkan 3 pintasan utama (`Gateway Utama`, `Graduance Portal`, `Halaman Status Publik`) serta direktori terstruktur untuk seluruh 9 domain produksi (`Situs dan aplikasi`, `Operasional`, `Integrasi dan API`) setelah autentikasi PIN ([`docs/service-shortcuts.md`](docs/service-shortcuts.md)).
   - Respons `GET /` anonim disanitasi di sisi server sehingga tidak membocorkan shell dashboard maupun daftar domain internal sebelum PIN diverifikasi.
7. **Ringkasan Status Publik Uptime Kuma**:
   - Mengambil dan memvalidasi ringkasan status publik dari Uptime Kuma (`/api/status-page/default` dan `/api/status-page/heartbeat/default`) dengan *caching* 45 detik, batas waktu 3 detik, serta *fallback* aman `Tidak diketahui` (`UNKNOWN`) apabila Uptime Kuma tidak dapat dihubungi ([`docs/monitoring-integration.md`](docs/monitoring-integration.md)).

---

## 🛠️ Tech Stack

- **Backend**: Python 3.12, FastAPI, Uvicorn, psutil, Docker SDK, itsdangerous
- **Frontend**: HTML5, Tailwind CSS, Alpine.js, Chart.js (Zero node_modules bloat)
- **Deployment**: Docker Compose, Caddy v2 Reverse Proxy, Cloudflare Anycast SSL
- **CI/CD**: GitHub Actions (`appleboy/ssh-action`) dengan Git as Single Source of Truth

---

## 💻 Menjalankan Secara Lokal (Development)

```bash
# 1. Buat virtual environment Python
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/macOS

# 2. Instal dependensi
pip install -r requirements.txt

# 3. Jalankan server lokal
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
Buka browser di `http://127.0.0.1:8000`. Untuk penggunaan di luar mesin lokal, atur `DASHBOARD_PIN` dan `SECRET_KEY` sendiri sebelum aplikasi dijalankan. Nilai bawaan di kode dan Compose saat ini hanya cocok untuk pengembangan lokal.

---

## 🌐 Deployment di Server Produksi (VPS)

1. Pastikan subdomain `dashboard.digitalneeds.my.id` telah didaftarkan pada `/opt/infrastructure/reverse-proxy/Caddyfile`:
   ```caddyfile
   dashboard.digitalneeds.my.id {
       tls internal
       reverse_proxy vps-monitor-dashboard:8000
       encode gzip zstd

       header {
           Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
           X-Content-Type-Options "nosniff"
           X-Frame-Options "SAMEORIGIN"
           Referrer-Policy "strict-origin-when-cross-origin"
       }
   }
   ```
2. Reload konfigurasi Caddy:
   ```bash
   docker exec caddy-proxy caddy reload --config /etc/caddy/Caddyfile
   ```
3. Bangun dan jalankan kontainer:
   ```bash
   docker compose up -d --build
   ```

## Cara memeriksa

- `python -m compileall -q app` memeriksa sintaks Python.
- `python -m unittest discover -s tests -v` menjalankan pengujian kontrak Uptime Kuma, katalog 9 domain, dan sanitasi respons anonim vs terautentikasi.
- `docker compose config --quiet` memeriksa format konfigurasi Compose. Pemeriksaan ini tidak membuktikan bahwa konfigurasi aman untuk produksi.
- Setelah aplikasi berjalan, `GET /api/ping` memeriksa respons proses HTTP. Periksa metrik, status backup, dan daftar kontainer secara terpisah pada host tujuan sebelum mengandalkannya.

## Acuan proyek

- [`docs/monitoring-integration.md`](docs/monitoring-integration.md) berisi hasil penyelidikan rute Uptime Kuma dan arahan integrasi untuk Antigravity.
- [`docs/service-shortcuts.md`](docs/service-shortcuts.md) berisi inventaris domain aktif dan kriteria pintasan semua layanan.

- [`anti-slop/audit-001-2026-09-09.md`](anti-slop/audit-001-2026-09-09.md) dan [`anti-slop/audit-002-2026-09-09.md`](anti-slop/audit-002-2026-09-09.md) berisi audit antarmuka sebelumnya dan tindak lanjutnya.
- [`AGENTS.md`](AGENTS.md) memuat aturan universal agent; [`.agents/rules/master-mind.md`](.agents/rules/master-mind.md) menghubungkannya ke Antigravity.

## Aturan khusus proyek

- Pertahankan stack FastAPI, template HTML, Alpine.js, Chart.js, Docker Compose, dan jaringan proxy eksternal selama tidak ada keputusan arsitektur baru.
- Sumber data yang dituju adalah host VPS, Docker Engine, dan direktori backup `/opt/backups`. Validasi bahwa setiap metrik benar-benar mewakili host sebelum dipakai untuk keputusan operasional.
- Perubahan deployment harus mempertimbangkan workflow [`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml), yang mendeploy setiap push ke `main` setelah pemeriksaan lint.
