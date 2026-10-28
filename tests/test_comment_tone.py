"""
Test untuk dua penyebab komentar terdengar allay/amatir.

1. Label versi bocor ke publik. Prompt meminta "2 versi komentar" dan Qoder
   mengembalikan tiap versi dengan kepala seperti:
       **Versi 2** — react, kena mental
       kena nih, selama ini gue salahkan tool-nya ternyata emang gue
   _strip_markdown_structure hanya membuang baris yang SELURUHNYA label
   markdown, jadi kepala versi ikut terkirim ke Threads. Bukti produksi: 3 dari
   6 komentar terakhir dimulai "**Versi 2** — ...".

2. Pembuka reaksi-kaget tidak dilarang. Daftar pembuka terlarang dan
   _AI_SOUNDING_PATTERNS tidak mencakup "serius?", "ga nyangka", "anjir", dll,
   padahal prompt mengajarkan contoh bergaya itu -- jadi model menirunya.
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def ac(monkeypatch, tmp_path):
    monkeypatch.setenv("PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "path", [PROJECT_ROOT] + sys.path)
    import auto_commenter
    return auto_commenter


# ── 1. label versi harus dibuang ──────────────────────────────────────────────

DRAF_PRODUKSI = [
    ("**Versi 2** — react, kena mental\nkena nih, selama ini gue salahkan tool-nya ternyata emang gue",
     "kena nih, selama ini gue salahkan tool-nya ternyata emang gue"),
    ("**Versi 1** — react, kaget campur relate\nanjir ini workflow impian gue, tp punya gue masih banyak manualnya",
     "anjir ini workflow impian gue, tp punya gue masih banyak manualnya"),
    ("*Versi 1* — nanya penasaran\nini udah solved blm?",
     "ini udah solved blm?"),
    ("Versi 2 — share singkat\ngue kemarin 3 hari stuck di sini",
     "gue kemarin 3 hari stuck di sini"),
]


@pytest.mark.parametrize("draf,isi", DRAF_PRODUKSI)
def test_label_versi_dibuang_isi_dipertahankan(ac, draf, isi):
    hasil = ac._strip_markdown_structure(draf)

    assert "versi" not in hasil.lower(), f"label versi bocor: {hasil!r}"
    assert isi in hasil, f"isi komentar hilang: {hasil!r}"


@pytest.mark.parametrize("draf,isi", DRAF_PRODUKSI)
def test_parse_ai_reply_menghasilkan_komentar_bersih(ac, draf, isi):
    """Jalur yang benar-benar dipakai sebelum posting."""
    hasil = ac.parse_ai_reply(draf)

    assert hasil != "SKIP"
    assert "versi" not in hasil.lower()
    assert isi in hasil


def test_komentar_tanpa_label_tidak_berubah(ac):
    teks = "bukan lo doang sih, gue jg lagi ngalamin ini"

    assert ac._strip_markdown_structure(teks) == teks


def test_kata_versi_di_tengah_kalimat_tidak_dibuang(ac):
    """"versi" sebagai isi komentar (bukan label) harus selamat."""
    teks = "gue malah masih pakai versi lama, takut migrate"

    assert ac._strip_markdown_structure(teks) == teks


# ── 2. pembuka reaksi-kaget harus ditolak gerbang kualitas ────────────────────

PEMBUKA_ALLAY = [
    "serius? gue kira hasilnya bakal mirip2 aja",
    "ga nyangka semudah itu ternyata",
    "nggak nyangka bagian itu bisa di-automate",
    "anjir sama persis kayak punya gue",
    "kaget sih ini dapet segitu",
    "gila rapi banget hasilnya",
    "buset ini sih diluar ekspektasi",
    "wow cepet banget selesai",
]


@pytest.mark.parametrize("teks", PEMBUKA_ALLAY)
def test_pembuka_kaget_ditolak_quality_check(ac, teks):
    assert ac._quality_check(teks) is False, (
        f"pembuka allay lolos gerbang kualitas: {teks!r}"
    )


SUARA_TENANG = [
    "gue kemarin 3 hari stuck di bagian yang sama",
    "ini gue banget, akhirnya gue tinggalin aja dulu",
    "coba cek log-nya dulu sih, biasanya ketahuan dari situ",
    "kira2 berapa budget buat sales di SaaS yg usernya masih segelintir?",
]


@pytest.mark.parametrize("teks", SUARA_TENANG)
def test_suara_tenang_tetap_lolos(ac, teks):
    """Gerbang yang terlalu ketat sama buruknya: membungkam komentar wajar."""
    assert ac._quality_check(teks) is True, f"komentar wajar ikut ditolak: {teks!r}"
