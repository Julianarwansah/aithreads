# Changelog

Semua perubahan dan update penting pada project Threadscope akan dicatat pada file ini.

## [Unreleased] / [Terbaru]

### 🔄 Backend AI Pindah ke Qoder CLI (Breaking Change)
- **[llm_router.py] Qoder menggantikan Groq & Gemini**: `generate()` kini memanggil Qoder CLI secara headless (`qoder -p`) lewat subprocess. Semua tool Qoder dimatikan (`--tools ""`) dan session tidak disimpan (`--no-session-persistence`), jadi Qoder hanya menghasilkan teks dan tidak menyentuh filesystem. Tidak perlu API key lagi — cukup `qoder login`.
- **[gemini_manager.py dihapus]**: Rotasi key Gemini/Groq, quota lock, dan cooldown 20 detik tidak diperlukan lagi. Bagian cache-nya dipindah ke modul baru `reply_cache.py` (file cache sekarang `reply_cache.json`, TTL tetap 24 jam).
- **[auto_commenter.py & threads_poster.py]**: Cek `GEMINI_API_KEY` dihapus, diganti cek keberadaan Qoder CLI saat startup (`llm_router.is_available()`) supaya bot tidak berputar sia-sia kalau Qoder belum terinstall.
- **[auto_commenter.py] Perbaikan bug balasan error ikut ter-post**: Sebelumnya kalau LLM gagal, `generate_reply()` mengembalikan string `"Error AI: ..."` yang lalu **benar-benar terkirim sebagai komentar** ke Threads (hanya `"SKIP"` yang disaring). Sekarang kegagalan LLM menghasilkan `SKIP`.
- **[stats_tracker.py]**: Key `gemini_errors` diganti `llm_errors`. Counter `llm_router_errors` sebelumnya tidak pernah tercatat karena tidak terdaftar di `DEFAULT_STATS`.
- **[requirements.txt]**: `google-genai` dihapus.
- **[.env.example & .gitignore ditambahkan]**: `.env.example` sebelumnya dirujuk README tapi tidak ada. `.gitignore` dibuat supaya `.env` (berisi session cookie Threads) tidak pernah ikut ter-commit.
- **[Konfigurasi]**: `ENGAGEMENT_MODE` dihapus karena hanya dipakai `ai_engagement_engine.py` yang sudah dihapus.

### 🗑️ Penghapusan Fitur Afiliasi (Breaking Change)
- **[Global] Bot tidak lagi berafiliasi**: Seluruh jalur afiliasi Shopee dihapus. Bot kini hanya menghasilkan komentar natural layaknya manusia di postingan orang lain, tanpa link, nama produk, atau promosi dalam bentuk apapun.
- **[auto_commenter.py] Prompt & parser disederhanakan**: Format `AFFILIATE:` / `NO_AFFILIATE:` dihapus. `generate_reply(caption)` tidak lagi menerima parameter produk, dan penyisipan link paksa di akhir komentar dihilangkan.
- **[auto_commenter.py] Link Guard**: Balasan AI yang masih mengandung URL otomatis dibuang (SKIP) agar tidak pernah ada link yang terkirim ke Threads. Prefix `AFFILIATE:` dari cache lama juga ikut dibersihkan.
- **[auto_commenter.py] `APP_MODE`**: Nilai `affiliate` dan `trend_affiliate` dihapus. Pilihannya sekarang `normal` (acak) atau `trend` (postingan ramai, dinilai murni dari likes & replies tanpa skor relevansi produk).
- **[threads_poster.py] Posting organik murni**: Cabang afiliasi dihapus, `threads_poster.py` selalu membuat opini tanpa link.
- **[File dihapus]**: `services/affiliate_strategy.py`, `services/shopee_generator.py`, `services/shopeeAuth.js`, `ai_engagement_engine.py`, dan `shopee_affiliate_links.json`.

### 🚀 Fitur Baru & Peningkatan (Enhancements)
- **[auto_commenter.py] Konfigurasi Fallback Otomatis**: Menambahkan konfigurasi *GraphQL (Default Fallback)* bawaan. Sekarang, jika pengguna tidak memiliki file `graphql_dump.json` atau gagal melakukan *capture*, bot tidak akan *crash*, melainkan otomatis menggunakan konfigurasi *default* yang sudah teruji.
- **[auto_commenter.py] Sistem Log Debug Transparan**: Menambahkan pesan `[DEBUG]` pada tahapan `process_timeline`, `fetch_timeline`, dan `extract_posts`. Sebelumnya, jika bot gagal mengambil timeline (karena token kadaluarsa atau koneksi diblokir), bot akan seakan-akan *hang* (diam dan *sleep* 60 detik tanpa pesan). Sekarang, bot akan mencetak alasan pasti kenapa gagal.
- **[main.py] Pembersihan Kode (Code Cleanup)**: Menghapus fungsionalitas *testing* khusus Shopee Affiliate dari `main.py`. Skrip `main.py` kini lebih ringan dan 100% difokuskan hanya untuk validasi *Session Threads* dan *Network Discovery*.

### 🐛 Perbaikan Bug (Bug Fixes)
- **[services/network_discovery.py] Perbaikan Parsing Payload URL-Encoded**: Memperbaiki masalah fatal di mana skrip `main.py` sering kali menghasilkan file `graphql_dump.json` yang kosong. Hal ini disebabkan karena sebelumnya skrip tidak bisa membaca payload GraphQL dari Threads yang menggunakan format *URL-encoded*. Skrip kini sudah ditambal agar bisa melakukan parsing pada format JSON murni maupun *URL-encoded* dengan sempurna.
- **[auto_commenter.py] Pencegahan Crash pada Format JSON**: Meningkatkan keandalan fungsi `load_latest_query_config()`. Script kini kebal terhadap berbagai variasi format isi dari `graphql_dump.json` (seperti perubahan struktur tipe data) sehingga tidak akan terjadi error *Type Error* atau *JSONDecodeError*.
