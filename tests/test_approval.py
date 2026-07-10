from pathlib import Path

from config import Config
from modules import approval
from modules.approval import Decision


def _config(**over):
    base = dict(
        anthropic_api_key="sk-ant-test",
        server_ip="1.2.3.4",
        server_user="dillon",
        jellyfin_url="http://1.2.3.4:8096",
        jellyfin_api_key="jf-key",
        discord_webhook_url="https://discord.com/api/webhooks/test",
        temp_dir=Path("/tmp/ai-ripper"),
        media_root="/media",
    )
    base.update(over)
    return Config(**base)


def _named():
    return [
        {"path": Path("/tmp/title_t00.mkv"), "title_index": 0, "duration_secs": 1320,
         "episode": 1, "jellyfin_filename": "The.Office.S01E01.mkv",
         "episode_name": "Pilot", "method": "subtitles", "confidence": 0.98},
        {"path": Path("/tmp/title_t03.mkv"), "title_index": 3, "duration_secs": 1330,
         "episode": 2, "jellyfin_filename": "The.Office.S01E02.mkv",
         "episode_name": "Diversity Day", "method": "subtitles", "confidence": 0.97},
    ]


def _dropped():
    return [
        {"path": Path("/tmp/title_t06.mkv"), "title_index": 6, "duration_secs": 2688,
         "drop_reason": "duplicate of E05 (longer — 'Play All')"},
    ]


# --- pure formatting -------------------------------------------------------

def test_format_mapping_sorted_by_episode_with_name_and_confidence():
    lines = approval.format_mapping(list(reversed(_named())))
    assert lines[0] == "title_t00.mkv → The.Office.S01E01.mkv — Pilot  [subtitles, 0.98]"
    assert lines[1].startswith("title_t03.mkv → The.Office.S01E02.mkv — Diversity Day")


def test_format_dropped_includes_duration_and_reason():
    line = approval.format_dropped(_dropped())[0]
    assert "title_t06.mkv (44:48)" in line
    assert "Play All" in line


def test_format_proposal_lists_episodes_and_dropped():
    text = approval.format_proposal(_named(), _dropped())
    assert "**Proposed episodes:**" in text
    assert "**Dropped (not transferred):**" in text
    assert "The.Office.S01E01.mkv" in text


def test_format_proposal_omits_dropped_section_when_empty():
    text = approval.format_proposal(_named(), [])
    assert "Dropped" not in text


# --- request_approval gating ----------------------------------------------

def test_request_approval_holds_when_bot_not_configured():
    decision = approval.request_approval(_named(), _dropped(), _config())
    assert isinstance(decision, Decision)
    assert decision.approved is False
    assert "not configured" in decision.reason


def test_request_approval_never_raises_on_driver_failure(monkeypatch):
    # Bot IS configured, but the async driver blows up — must degrade, not raise.
    def boom(*a, **k):
        raise RuntimeError("gateway down")
    monkeypatch.setattr(approval, "_drive", boom)
    decision = approval.request_approval(
        _named(), _dropped(),
        _config(discord_bot_token="tok", discord_channel_id="123"),
    )
    assert decision.approved is False
    assert "gateway down" in decision.reason
