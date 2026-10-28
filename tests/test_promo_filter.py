"""
Test untuk filter promosi/jualan pada dua alur.

Permintaan user: "untuk orang yang jualan atau promosi tidak perlu di-comment
atau di-reply."

Kondisi sebelum perbaikan:
  - Sisi komentar: kategori "promosi/iklan" di is_sensitive_content() hanya
    mengenal frasa sempit (harga promo, diskon N%, link bio, order sekarang...).
    Frasa jual-beli sehari-hari di Threads Indonesia -- open order, ready stock,
    jasa titip, keranjang kuning, open endorse, pre order -- lolos semua.
  - Sisi balasan: klasifikasi hanya punya 3 token (jelas / tidak_jelas /
    tidak_sopan). Orang yang membalas komentar bot dengan jualan tetap dibalas.

Batas yang sengaja dipilih: post yang SEKADAR menyebut marketplace dalam rangka
diskusi ("beli keyboard di shopee, worth gak?") TIDAK dianggap promosi -- itu
percakapan asli, bukan orang jualan.
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    data = tmp_path / "profile"
    data.mkdir()
    monkeypatch.setenv("PROFILE_DIR", str(data))
    monkeypatch.setattr(sys, "path", [PROJECT_ROOT] + sys.path)
    return str(data)


PROMO_POSTS = [
    "open order jasa titip bulan ini, slot terbatas ya kak",
    "ready stock semua warna, minat dm aja",
    "jasa follow threads murah banget, testimoni lengkap",
    "checkout keranjang kuning sekarang, gratis ongkir se-indonesia",
    "open endorse slot minggu ini, dm for rate card",
    "pre order dulu ya, harga mulai 50rb aja",
    "aku jual template notion productivity, link di bio ya",
]

DISKUSI_BUKAN_PROMOSI = [
    "gue beli keyboard mekanikal di shopee kemarin, worth it gak sih buat ngoding?",
    "review jujur setelah 3 bulan pakai laptop ini untuk ngoding",
    "worth gak sih langganan cloud setahun langsung dibanding bulanan?",
]


# ── sisi komentar ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("caption", PROMO_POSTS)
def test_post_jualan_ditolak_sebagai_promosi(isolated, caption):
    import auto_commenter as ac

    kategori = ac.is_sensitive_content(caption)

    assert kategori == "promosi/iklan", (
        f"post jualan lolos filter: {caption!r} -> {kategori!r}"
    )


@pytest.mark.parametrize("caption", PROMO_POSTS)
def test_should_comment_menolak_post_jualan(isolated, caption):
    import auto_commenter as ac

    layak, alasan = ac.should_comment(caption)

    assert layak is False
    assert "promosi" in alasan, f"alasan harus menyebut promosi, dapat {alasan!r}"


@pytest.mark.parametrize("caption", DISKUSI_BUKAN_PROMOSI)
def test_diskusi_yang_menyebut_marketplace_bukan_promosi(isolated, caption):
    """False positive di sini membuang percakapan asli -- sama buruknya dengan
    mengomentari orang jualan."""
    import auto_commenter as ac

    assert ac.is_sensitive_content(caption) == "", (
        f"diskusi biasa salah ditandai promosi: {caption!r}"
    )


def test_promo_keywords_extra_bisa_ditambah_dari_config(isolated, monkeypatch):
    """User harus bisa menambah kata sendiri tanpa menyentuh kode."""
    import config_loader
    import auto_commenter as ac

    config_loader.save({"promo_keywords_extra": ["jualanku"]})

    layak, alasan = ac.should_comment("intip jualanku yang baru ya kak")

    assert layak is False
    assert "promosi" in alasan


# ── sisi balasan ──────────────────────────────────────────────────────────────

def test_klasifikasi_mengenali_token_promosi(isolated, monkeypatch):
    import reply_watcher as rw

    monkeypatch.setattr(rw.llm_router, "generate", lambda *a, **k: "promosi")

    assert rw._classify_reply("kak aku ada jasa follow murah nih, minat?") == "promosi"


def test_parse_menerima_promosi_dalam_berbagai_bentuk(isolated):
    import reply_watcher as rw

    assert rw._parse_classify_response("promosi") == "promosi"
    assert rw._parse_classify_response("Promosi") == "promosi"
    assert rw._parse_classify_response("jawabannya: promosi") == "promosi"


def test_klasifikasi_gagal_tetap_fail_open_ke_jelas(isolated, monkeypatch):
    """Perilaku lama tidak boleh berubah: LLM error = balasan orang sungguhan
    tetap dibalas."""
    import reply_watcher as rw

    def _boom(*a, **k):
        raise RuntimeError("llm mati")

    monkeypatch.setattr(rw.llm_router, "generate", _boom)

    assert rw._classify_reply("bang gimana cara setupnya?") == "jelas"
