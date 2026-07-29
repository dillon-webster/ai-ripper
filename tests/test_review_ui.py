import json
import re
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import Config
from modules import identify, review_ui
from modules.review_ui import ReviewDecision


@pytest.fixture(autouse=True)
def notify_calls(monkeypatch):
    """request_review pings Discord (with retries) once the server is up — stub it
    so tests never attempt real webhook posts, and record the advertised URLs."""
    calls = []
    monkeypatch.setattr(review_ui.notifier, "send_review_ready",
                        lambda url, show, season, config: calls.append(url))
    return calls


def _config(tmp_path, **over):
    base = dict(
        anthropic_api_key="sk-ant-test",
        server_ip="1.2.3.4",
        server_user="dillon",
        jellyfin_url="http://1.2.3.4:8096",
        jellyfin_api_key="jf-key",
        discord_webhook_url="https://discord.com/api/webhooks/test",
        temp_dir=tmp_path,
        media_root="/media",
        review_ui_port=0,  # ephemeral — tests must not fight over a fixed port
    )
    base.update(over)
    return Config(**base)


def _guide():
    return [
        {"index": 1, "index_end": None, "name": "The Dundies", "runtime_secs": 1320},
        {"index": 2, "index_end": None, "name": "Sexual Harassment", "runtime_secs": 1330},
        {"index": 3, "index_end": 4, "name": "Office Olympics", "runtime_secs": 2640},
    ]


def _all_titles():
    return [
        {"path": Path("/tmp/title_t00.mkv"), "title_index": 0, "duration_secs": 1320},
        {"path": Path("/tmp/title_t01.mkv"), "title_index": 1, "duration_secs": 1330},
        {"path": Path("/tmp/title_t02.mkv"), "title_index": 2, "duration_secs": 2688},
    ]


def _named():
    return [
        {"path": Path("/tmp/title_t00.mkv"), "title_index": 0, "duration_secs": 1320,
         "episode": 1, "jellyfin_filename": "The.Office.S02E01.mkv",
         "episode_name": "The Dundies", "method": "subtitles", "confidence": 0.98},
    ]


def _dropped():
    return [
        {"path": Path("/tmp/title_t02.mkv"), "title_index": 2, "duration_secs": 2688,
         "episode": None, "method": "skipped",
         "drop_reason": "Play-All/omnibus (44 min — longer than any episode)"},
    ]


# --- thumb_timestamps --------------------------------------------------------

def test_thumb_timestamps_spread_within_bounds():
    ts = review_ui.thumb_timestamps(1320, 12)
    assert len(ts) == 12
    assert ts == sorted(ts)
    assert ts[0] >= 40          # head skip — past recap/montage
    assert ts[-1] < 1320 * 0.97  # tail skip — not into credits/black


def test_thumb_timestamps_short_title_still_returns_frames():
    ts = review_ui.thumb_timestamps(30, 12)
    assert ts
    assert all(0 <= t < 30 for t in ts)


def test_thumb_timestamps_unknown_duration_falls_back():
    assert review_ui.thumb_timestamps(None, 12) == [review_ui._THUMB_FALLBACK_SECS]
    assert review_ui.thumb_timestamps(0, 12) == [review_ui._THUMB_FALLBACK_SECS]


# --- build_page_model --------------------------------------------------------

def test_page_model_shows_every_ripped_title():
    model = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                       _guide(), "The Office", 2)
    assert [t["title_index"] for t in model["titles"]] == [0, 1, 2]


def test_page_model_kept_title_has_guess_and_label():
    model = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                       _guide(), "The Office", 2)
    kept = model["titles"][0]
    assert kept["status"] == "kept"
    assert kept["guess"] == 1
    assert "The.Office.S02E01.mkv" in kept["guess_label"]
    assert "The Dundies" in kept["guess_label"]
    assert "subtitles 0.98" in kept["guess_label"]


def test_page_model_dropped_title_shows_reason_and_no_guess():
    model = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                       _guide(), "The Office", 2)
    dropped = model["titles"][2]
    assert dropped["status"] == "dropped"
    assert dropped["guess"] is None
    assert "Play-All" in dropped["guess_label"]


def test_page_model_unproposed_title_is_unassigned():
    model = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                       _guide(), "The Office", 2)
    other = model["titles"][1]
    assert other["status"] == "unassigned"
    assert other["guess"] is None


def test_page_model_guess_from_legacy_filename_without_episode_key():
    # The legacy namer sets only jellyfin_filename — the guess must parse from it.
    legacy = [{"path": Path("/tmp/title_t01.mkv"), "title_index": 1,
               "duration_secs": 1330, "jellyfin_filename": "The.Office.S02E02.mkv"}]
    model = review_ui.build_page_model(_all_titles(), legacy, [],
                                       _guide(), "The Office", 2)
    assert model["titles"][1]["guess"] == 2


def test_page_model_guess_outside_guide_is_cleared():
    named = [{**_named()[0], "episode": 99}]
    model = review_ui.build_page_model(_all_titles(), named, [],
                                       _guide(), "The Office", 2)
    assert model["titles"][0]["guess"] is None


def test_page_model_is_json_serializable():
    model = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                       _guide(), "The Office", 2)
    json.dumps(model)  # Paths etc. must already be plain types


def test_page_model_nonce_unique_per_build():
    # Discs reuse title indices and timestamps; only the nonce keeps two review
    # sessions' thumb URLs distinct, so it must differ every build.
    m1 = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                    _guide(), "The Office", 2)
    m2 = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                    _guide(), "The Office", 2)
    assert m1["nonce"] and m2["nonce"]
    assert m1["nonce"] != m2["nonce"]


def test_page_model_explicit_nonce_is_kept():
    model = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                       _guide(), "The Office", 2, nonce="abc123")
    assert model["nonce"] == "abc123"


# --- build_curated_named -----------------------------------------------------

def test_curated_named_builds_transfer_ready_titles():
    named = review_ui.build_curated_named(
        {"0": 1, "1": 2, "2": None}, _all_titles(), _guide(), "The Office", 2)
    assert [t["jellyfin_filename"] for t in named] == [
        "The.Office.S02E01.mkv", "The.Office.S02E02.mkv"]
    assert named[0]["episode_name"] == "The Dundies"
    assert named[0]["media_type"] == "tv"
    assert named[0]["destination"] == "tvshows"
    assert named[0]["is_extra"] is False
    assert named[0]["path"] == Path("/tmp/title_t00.mkv")


def test_curated_named_null_and_absent_titles_are_excluded():
    named = review_ui.build_curated_named({"0": 1}, _all_titles(), _guide(),
                                          "The Office", 2)
    assert len(named) == 1
    assert named[0]["title_index"] == 0


def test_curated_named_double_episode_slot_carries_index_end():
    named = review_ui.build_curated_named({"2": 3}, _all_titles(), _guide(),
                                          "The Office", 2)
    assert named[0]["jellyfin_filename"] == "The.Office.S02E03-E04.mkv"


def test_curated_named_rejects_duplicate_episode():
    with pytest.raises(ValueError, match="assigned to two titles"):
        review_ui.build_curated_named({"0": 1, "1": 1}, _all_titles(), _guide(),
                                      "The Office", 2)


def test_curated_named_rejects_unknown_title_index():
    with pytest.raises(ValueError, match="unknown title index"):
        review_ui.build_curated_named({"9": 1}, _all_titles(), _guide(),
                                      "The Office", 2)


def test_curated_named_rejects_episode_not_in_guide():
    with pytest.raises(ValueError, match="not in the season guide"):
        review_ui.build_curated_named({"0": 99}, _all_titles(), _guide(),
                                      "The Office", 2)


def test_curated_named_rejects_non_dict_assignments():
    with pytest.raises(ValueError):
        review_ui.build_curated_named([1, 2], _all_titles(), _guide(),
                                      "The Office", 2)


def test_curated_named_empty_selection_is_valid_and_empty():
    assert review_ui.build_curated_named({"0": None}, _all_titles(), _guide(),
                                         "The Office", 2) == []


# --- render_page -------------------------------------------------------------

def test_render_page_embeds_model_and_is_self_contained():
    model = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                       _guide(), "The Office", 2)
    html = review_ui.render_page(model)
    assert "The Dundies" in html
    assert "http://" not in html and "https://" not in html  # no external assets
    assert "</script>" in html


def test_render_page_neutralizes_closing_tags_in_data():
    model = {"show": "X</script><script>alert(1)", "season": 1,
             "episodes": [], "titles": []}
    html = review_ui.render_page(model)
    assert "X</script><script>alert(1)" not in html


# --- _advertised_host ---------------------------------------------------------

def test_advertised_host_override_wins(tmp_path):
    cfg = _config(tmp_path, review_ui_advertise_host="ripbox.tail1234.ts.net")
    assert review_ui._advertised_host(cfg) == "ripbox.tail1234.ts.net"


def test_advertised_host_prefers_tailscale_ip(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        assert cmd[0] == "tailscale"
        return SimpleNamespace(returncode=0, stdout="100.64.0.7\n")
    monkeypatch.setattr(review_ui.subprocess, "run", fake_run)
    assert review_ui._advertised_host(_config(tmp_path)) == "100.64.0.7"


def test_advertised_host_falls_back_to_hostname(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("tailscale not installed")
    monkeypatch.setattr(review_ui.subprocess, "run", fake_run)
    assert review_ui._advertised_host(_config(tmp_path)) == review_ui.socket.gethostname()


# --- request_review failure paths (never raises) ------------------------------

def test_request_review_holds_without_guide(tmp_path):
    d = review_ui.request_review(_all_titles(), _named(), _dropped(), [],
                                 "The Office", 2, _config(tmp_path))
    assert isinstance(d, ReviewDecision)
    assert d.approved is False
    assert "guide" in d.reason


def test_request_review_times_out_and_holds(tmp_path):
    d = review_ui.request_review(_all_titles(), _named(), _dropped(), _guide(),
                                 "The Office", 2, _config(tmp_path), timeout=0.2)
    assert d.approved is False
    assert "timed out" in d.reason


def test_request_review_holds_when_port_is_taken(tmp_path):
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        d = review_ui.request_review(_all_titles(), _named(), _dropped(), _guide(),
                                     "The Office", 2,
                                     _config(tmp_path, review_ui_port=port),
                                     timeout=2)
    finally:
        blocker.close()
    assert d.approved is False
    assert "error" in d.reason


def test_request_review_cleans_thumb_cache_dir(tmp_path):
    review_ui.request_review(_all_titles(), _named(), _dropped(), _guide(),
                             "The Office", 2, _config(tmp_path), timeout=0.2)
    assert not (tmp_path / "review-thumbs").exists()


def test_request_review_wipes_stale_thumbs_on_start(tmp_path):
    # A killed run (Ctrl-C, power loss) skips the finally-cleanup; a leftover frame
    # keyed the same way would otherwise be served as the NEW disc's frame.
    stale = tmp_path / "review-thumbs" / "t0_100.jpg"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"frame from a previous disc")
    seen = {}

    def on_start(host, port):
        seen["stale_exists"] = stale.exists()

    review_ui.request_review(_all_titles(), _named(), _dropped(), _guide(),
                             "The Office", 2, _config(tmp_path), timeout=0.2,
                             _on_start=on_start)
    assert seen["stale_exists"] is False


# --- request_review round trip over real HTTP ---------------------------------

def _submit(port, assignments, path="/submit"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps({"assignments": assignments}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=5)


def test_request_review_round_trip_returns_curated_titles(tmp_path, notify_calls):
    started = threading.Event()
    bound = {}

    def on_start(host, port):
        bound["port"] = port
        started.set()

    result = {}

    def run():
        result["decision"] = review_ui.request_review(
            _all_titles(), _named(), _dropped(), _guide(), "The Office", 2,
            _config(tmp_path), timeout=10, _on_start=on_start)

    t = threading.Thread(target=run)
    t.start()
    assert started.wait(5)
    port = bound["port"]

    # The Discord ping carried the review link (host may be a tailscale IP or the
    # LAN hostname depending on the box — the port pins it to this server).
    assert len(notify_calls) == 1
    assert notify_calls[0].startswith("http://") and f":{port}/" in notify_calls[0]

    # The page serves with the model embedded.
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as res:
        page = res.read().decode()
    assert "The Dundies" in page

    # A duplicate assignment must be rejected server-side (400), review stays open.
    with pytest.raises(urllib.error.HTTPError) as exc:
        _submit(port, {"0": 1, "1": 1})
    assert exc.value.code == 400

    # A good submission resolves the review with the curated list.
    with _submit(port, {"0": 2, "1": 1, "2": None}) as res:
        assert res.status == 200
    t.join(timeout=5)
    assert not t.is_alive()

    d = result["decision"]
    assert d.approved is True
    assert [x["jellyfin_filename"] for x in d.titles] == [
        "The.Office.S02E01.mkv", "The.Office.S02E02.mkv"]
    # Reassignment honored: episode 1 now comes from title #1.
    assert d.titles[0]["title_index"] == 1


def test_thumb_endpoint_rejects_unknown_title_and_stale_nonce(tmp_path):
    started = threading.Event()
    bound = {}

    def on_start(host, port):
        bound["port"] = port
        started.set()

    result = {}

    def run():
        result["decision"] = review_ui.request_review(
            _all_titles(), _named(), _dropped(), _guide(), "The Office", 2,
            _config(tmp_path), timeout=10, _on_start=on_start)

    t = threading.Thread(target=run)
    t.start()
    assert started.wait(5)
    port = bound["port"]

    # The served page embeds this session's nonce in the model.
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as res:
        page = res.read().decode()
    m = re.search(r'"nonce":\s*"([0-9a-f]+)"', page)
    assert m, "page model must embed the session nonce"
    nonce = m.group(1)

    # Right session, unknown title index → 404.
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/thumb/{nonce}/99?t=10", timeout=5)
    assert exc.value.code == 404

    # A tab left over from a previous disc (wrong nonce) → 404, valid index or not.
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/thumb/deadbeefdeadbeef/0?t=10", timeout=5)
    assert exc.value.code == 404

    # The old nonce-less URL shape is gone too.
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/thumb/0?t=10", timeout=5)
    assert exc.value.code == 404

    # Unblock the review so the thread exits.
    with _submit(port, {"0": 1}):
        pass
    t.join(timeout=5)
    assert result["decision"].approved is True


# --- volume discs: two seasons of slots on one page --------------------------

def _volume_guide():
    """A disc from a volume box set: the tail of S04 and the head of S05. Note E01
    exists in BOTH seasons — the collision the episode key exists to break."""
    return [
        {"season": 4, "index": 30, "index_end": None, "name": "Brian Sings", "runtime_secs": 1320},
        {"season": 5, "index": 1, "index_end": None, "name": "Stewie Loves Lois", "runtime_secs": 1330},
        {"season": 5, "index": 2, "index_end": None, "name": "Mother Tucker", "runtime_secs": 1320},
    ]


def test_page_model_offers_slots_from_every_season_on_the_disc():
    model = review_ui.build_page_model(_all_titles(), [], [], _volume_guide(),
                                       "Family Guy", [4, 5])
    assert model["multi_season"] is True
    assert model["season_heading"] == "Seasons 4 + 5"
    assert [(e["season"], e["index"]) for e in model["episodes"]] == [(4, 30), (5, 1), (5, 2)]
    # Every slot is addressed by a distinct key, so S04E30 and S05E01 can't collide.
    assert len({e["key"] for e in model["episodes"]}) == 3


def test_page_model_preselects_the_slot_in_the_season_the_title_matched():
    named = [{"path": Path("/tmp/title_t00.mkv"), "title_index": 0, "duration_secs": 1330,
              "season": 5, "episode": 1, "jellyfin_filename": "Family.Guy.S05E01.mkv",
              "method": "subtitles", "confidence": 0.95}]
    model = review_ui.build_page_model(_all_titles(), named, [], _volume_guide(),
                                       "Family Guy", [4, 5])
    guess = model["titles"][0]["guess"]
    assert guess == identify.episode_key(5, 1)
    assert guess != identify.episode_key(4, 1)  # not the other season's E01


def test_page_model_single_season_heading_is_unchanged():
    model = review_ui.build_page_model(_all_titles(), _named(), _dropped(),
                                       _guide(), "The Office", 2)
    assert model["multi_season"] is False
    assert model["season_heading"] == "Season 2"


def test_curated_named_files_each_assignment_under_its_own_season():
    """A volume disc reviewed by hand: title 0 → S04E30, title 1 → S05E01."""
    guide = _volume_guide()
    named = review_ui.build_curated_named(
        {"0": identify.episode_key(4, 30), "1": identify.episode_key(5, 1)},
        _all_titles(), guide, "Family Guy", [4, 5])
    assert [t["jellyfin_filename"] for t in named] == \
        ["Family.Guy.S04E30.mkv", "Family.Guy.S05E01.mkv"]
    assert [t["episode_name"] for t in named] == ["Brian Sings", "Stewie Loves Lois"]


def test_curated_named_allows_the_same_episode_number_in_each_season():
    """S04E01 and S05E01 are different slots — assigning both is not a duplicate."""
    guide = [{"season": 4, "index": 1, "index_end": None, "name": "a", "runtime_secs": 1320},
             {"season": 5, "index": 1, "index_end": None, "name": "b", "runtime_secs": 1320}]
    named = review_ui.build_curated_named(
        {"0": identify.episode_key(4, 1), "1": identify.episode_key(5, 1)},
        _all_titles(), guide, "Family Guy", [4, 5])
    assert [t["jellyfin_filename"] for t in named] == \
        ["Family.Guy.S04E01.mkv", "Family.Guy.S05E01.mkv"]


def test_curated_named_still_rejects_the_same_slot_twice_on_a_volume_disc():
    with pytest.raises(ValueError, match="assigned to two titles"):
        review_ui.build_curated_named(
            {"0": identify.episode_key(5, 1), "1": identify.episode_key(5, 1)},
            _all_titles(), _volume_guide(), "Family Guy", [4, 5])


def test_rendered_page_labels_volume_slots_with_their_season():
    model = review_ui.build_page_model(_all_titles(), [], [], _volume_guide(),
                                       "Family Guy", [4, 5])
    html = review_ui.render_page(model)
    assert "volume disc" in html          # the page says why there are two seasons
    assert str(identify.episode_key(5, 1)) in html   # slots addressed by key
