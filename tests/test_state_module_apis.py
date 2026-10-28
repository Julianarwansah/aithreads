"""
Regresi: refactor safe_store pernah menghapus `_lock` dari comment_memory.py
sementara 10 fungsi analisis masih memakai `with _lock:` -> NameError saat runtime.
Test pytest yang ada hanya memanggil save(), jadi bug itu lolos.

Test ini memanggil setiap API publik jalur-baca supaya NameError/ImportError di
modul state mana pun langsung ketahuan.
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "profile"
    data.mkdir()
    monkeypatch.setenv("PROFILE_DIR", str(data))
    return str(data)


def test_semua_api_baca_comment_memory_bisa_dipanggil(isolated_data_dir, monkeypatch):
    monkeypatch.setattr(sys, "path", [PROJECT_ROOT] + sys.path)
    import comment_memory
    monkeypatch.setattr(comment_memory, "MEMORY_FILE",
                        os.path.join(isolated_data_dir, "comment_memory.json"))
    monkeypatch.setattr(comment_memory, "REJECTED_FILE",
                        os.path.join(isolated_data_dir, "rejected_comments.json"))

    comment_memory.save("caption contoh tentang AI", "balasan contoh",
                        natural=8, clarity=7, politeness=9)
    comment_memory.save_rejected("caption contoh", "balasan buruk",
                                 {"natural": 2, "clarity": 3, "politeness": 4})
    comment_memory.mark_engagement("balasan contoh", "caption contoh tentang AI")

    for fn in (
        comment_memory.get_style_counts,
        comment_memory.get_overused_style,
        comment_memory.build_memory_prompt,
        comment_memory.get_recent_topics,
        comment_memory.get_overused_topic,
        comment_memory.get_engagement_stats,
        comment_memory.get_topic_engagement_stats,
        comment_memory.get_recent_replies,
        comment_memory.get_engaged_comments,
        comment_memory.get_negative_examples,
        comment_memory.get_summary,
        comment_memory.total,
    ):
        fn()          # melempar NameError kalau _lock atau helper lain hilang


def test_semua_api_reply_cache_bisa_dipanggil(isolated_data_dir, monkeypatch):
    import reply_cache
    monkeypatch.setattr(reply_cache, "CACHE_FILE",
                        os.path.join(isolated_data_dir, "reply_cache.json"))

    reply_cache.save_to_cache("caption", "balasan", post_id="p1", comment_id="c1")
    assert reply_cache.check_cache("caption") == "balasan"

    reply_cache.get_activity_by_hour()
    reply_cache.get_peak_hours()
    reply_cache.get_recent_post_ids()


def test_api_stats_tracker_bisa_dipanggil(isolated_data_dir, monkeypatch):
    import stats_tracker
    monkeypatch.setattr(stats_tracker, "STATS_FILE",
                        os.path.join(isolated_data_dir, "stats.json"))

    stats_tracker.init_system_start()
    stats_tracker.update_heartbeat(1.0)
    stats_tracker.increment("comments_sent")
    stats_tracker.log_comment("contoh")
    stats_tracker.log_error("contoh error")

    stats = stats_tracker.get_stats()
    assert stats["comments_sent"] == 1
    assert stats["last_error"] == "contoh error"


def test_api_config_loader_bisa_dipanggil(isolated_data_dir, monkeypatch):
    import config_loader
    monkeypatch.setattr(config_loader, "SETTINGS_FILE",
                        os.path.join(isolated_data_dir, "config.json"))

    assert config_loader.save({"bot_persona": "persona uji"}) is True
    assert config_loader.get("bot_persona") == "persona uji"
    assert config_loader.load()["bot_persona"] == "persona uji"
