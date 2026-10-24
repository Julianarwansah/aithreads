# 🚀 ThreadScope — Threads Natural Commenter Bot

ThreadScope adalah bot otomatisasi berbasis AI yang membaca timeline Threads, memahami konteks postingan, dan membalas dengan komentar natural layaknya manusia. Bot ini **tidak** menyisipkan link afiliasi, jualan, atau promosi dalam bentuk apapun.

Bot ini menggunakan pendekatan **Session-First** (menggunakan cookie browser) untuk menghindari pemblokiran *trust-layer* dari Meta.

## ✨ Fitur Utama

- **Session Injection:** Menghindari blokir login menggunakan cookie browser.
- **Qoder sebagai otak AI:** Semua teks dihasilkan lewat Qoder CLI yang sudah login di komputermu — tidak perlu API key Gemini/Groq sama sekali.
- **Anti-AI Writing:** Komentar dihasilkan se-natural mungkin seperti manusia, menghindari bahasa kaku.
- **Link Guard:** Setiap balasan yang mengandung link otomatis dibuang (SKIP), jadi tidak akan pernah ada link yang terkirim.
- **Trend Mode:** Menargetkan postingan yang lagi ramai (30+ likes, 3+ replies) supaya komentar lebih terlihat.
- **Auto Post:** Dapat melakukan *organic post* secara otomatis agar akun tetap terlihat aktif.

## 🛠️ Instalasi & Persiapan

1. **Clone repositori dan masuk ke direktori proyek:**
   ```bash
   git clone <repo-url>
   cd Threadscope
   ```

2. **Buat dan aktifkan virtual environment:**
   - **Windows:**
     ```bash
     python -m venv venv
     venv\Scripts\activate
     ```
   - **Mac/Linux:**
     ```bash
     python -m venv venv
     source venv/bin/activate
     ```

3. **Install semua dependensi:**
   ```bash
   pip install -r requirements.txt
   pip install playwright
   python -m playwright install chromium
   ```

4. **Pastikan Qoder CLI sudah login** (ini backend AI-nya, pengganti Gemini/Groq):
   ```bash
   qoder status
   ```
   Kalau belum login, jalankan `qoder login` dulu. Bot memanggil Qoder lewat
   mode headless (`qoder -p`) dengan semua tool dimatikan, jadi Qoder hanya
   menghasilkan teks dan tidak menyentuh file apapun.

5. **Siapkan konfigurasi `.env`:**
   Salin file `.env.example` menjadi `.env`.
   ```bash
   cp .env.example .env
   ```

## ⚙️ Konfigurasi

### 1. Pengaturan `.env`
Buka file `.env` dan lengkapi data berikut:

```env
# Session Cookies Threads (Ambil dari F12 > Application > Cookies di threads.net)
THREADS_SESSION_ID=
THREADS_CSRF_TOKEN=
THREADS_DS_USER_ID=
THREADS_MID=
THREADS_IG_DID=

# Mode Aplikasi
APP_MODE=normal      # Pilihan: normal (acak) | trend (postingan ramai)

# Backend AI (opsional — kosongkan untuk pakai default)
# QODER_BIN=/path/ke/qoder
# QODER_MODEL=
# QODER_TIMEOUT=180
# QODER_MAX_TOKENS=350
```
*(Catatan: Jika session expired, silakan login ulang di browser dan perbarui cookie di `.env`)*

## 🚀 Cara Menjalankan

Pilih skrip yang ingin Anda jalankan sesuai kebutuhan:

1. **Bot Komentar Utama:**
   Bot akan membalas postingan di timeline dengan komentar natural dan jeda waktu yang manusiawi.
   ```bash
   python auto_commenter.py
   ```

2. **Bot Posting Otomatis:**
   Untuk membuat akun terlihat hidup dengan postingan organik.
   ```bash
   python threads_poster.py
   ```

3. **Network & Endpoint Discovery (Riset):**
   ```bash
   python main.py
   ```

## ⚠️ Disclaimer

Bot ini dibuat murni untuk keperluan **riset dan edukasi**. Penggunaan bot ini untuk melakukan spamming hingga menyebabkan penangguhan akun (suspend) sepenuhnya merupakan **tanggung jawab pengguna**. Harap patuhi Ketentuan Layanan (ToS) dari Meta.
