import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from config import Config


def _make_config(tmp_path):
    return Config(
        anthropic_api_key="sk-ant-test",
        server_ip="100.100.212.32",
        server_user="dillon",
        jellyfin_url="http://100.100.212.32:8096",
        jellyfin_api_key="jf-key",
        discord_webhook_url="https://discord.com/api/webhooks/test",
        temp_dir=tmp_path / "temp",
        media_root="/media",
    )


def _make_named_titles(tmp_path):
    f = tmp_path / "title_t00.mkv"
    f.write_bytes(b"")
    return [{
        "path": f,
        "duration_secs": 6127,
        "title_index": 0,
        "jellyfin_filename": "Friends.S01E01.mkv",
        "media_type": "tv",
        "destination": "tvshows",
    }]


def test_main_loop_runs_full_pipeline(tmp_path):
    config = _make_config(tmp_path)
    disc_path = tmp_path / "FRIENDS_S1D1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    named_titles = _make_named_titles(tmp_path)

    # Patch load_config to return our config, then raise StopIteration to break the while True
    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc", side_effect=[("FRIENDS_S1D1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.namer.identify", return_value=named_titles), \
         patch("ripper.transfer.send_all", return_value=["remote/path"]), \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main()

    mock_discord.assert_called_once_with(
        ["Friends.S01E01.mkv"], success=True, config=config
    )


def test_main_loop_drops_movie_extras_before_transfer(tmp_path):
    config = _make_config(tmp_path)
    disc_path = tmp_path / "INCEPTION"
    disc_path.mkdir()

    raw_titles = [
        {"path": disc_path / "title_t00.mkv", "duration_secs": 6127, "title_index": 0},
        {"path": disc_path / "title_t01.mkv", "duration_secs": 6127, "title_index": 1},
    ]
    feature = {
        "path": tmp_path / "title_t00.mkv", "duration_secs": 6127, "title_index": 0,
        "jellyfin_filename": "Inception.2010.mkv", "media_type": "movie",
        "destination": "movies", "is_extra": False,
    }
    commentary = {
        "path": tmp_path / "title_t01.mkv", "duration_secs": 6127, "title_index": 1,
        "jellyfin_filename": "Inception.2010.Commentary.mkv", "media_type": "movie",
        "destination": "movies", "is_extra": True,
    }

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc", side_effect=[("INCEPTION", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.namer.identify", return_value=[feature, commentary]), \
         patch("ripper.transfer.send_all", return_value=["remote/path"]) as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main()

    # Only the main feature should be transferred; the commentary extra is dropped.
    sent = mock_send.call_args[0][0]
    assert [t["jellyfin_filename"] for t in sent] == ["Inception.2010.mkv"]
    mock_discord.assert_called_once_with(
        ["Inception.2010.mkv"], success=True, config=config
    )


def test_main_loop_stages_bluray_locally_instead_of_transferring(tmp_path):
    """A Blu-ray disc (BDMV/) is staged to the local staging dir for encoding —
    NOT sent to the server, and no Jellyfin scan is triggered. The Discord notice
    is the staged variant."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "INCEPTION_BLURAY"
    disc_path.mkdir()
    (disc_path / "BDMV").mkdir()  # marks it as a Blu-ray

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 8400, "title_index": 0}]
    movie = {
        "path": tmp_path / "title_t00.mkv", "duration_secs": 8400, "title_index": 0,
        "jellyfin_filename": "Inception.2010.mkv", "media_type": "movie",
        "destination": "movies", "is_extra": False,
    }

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("INCEPTION_BLURAY", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.namer.identify", return_value=[movie]), \
         patch("ripper.transfer.stage_local", return_value=[tmp_path / "staged.mkv"]) as mock_stage, \
         patch("ripper.transfer.send_all") as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan") as mock_scan, \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main()

    mock_stage.assert_called_once()
    mock_send.assert_not_called()          # never touches the server
    mock_scan.assert_not_called()          # nothing to scan on the server
    mock_discord.assert_called_once_with(
        ["Inception.2010.mkv"], success=True, config=config, staged=True
    )


def test_main_loop_fetches_guide_before_rip_and_caps_title_length(tmp_path):
    """The episode guide is fetched BEFORE ripping so its longest runtime (×1.5)
    caps rip-time title length — Play-All titles are skipped, not ripped+dropped."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    named_titles = _make_named_titles(tmp_path)
    guide = [
        {"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404},
        {"index": 2, "index_end": None, "name": "Diversity Day", "runtime_secs": 1332},
    ]
    order = []

    def fake_guide(*args, **kwargs):
        order.append("guide")
        return guide

    def fake_rip(*args, **kwargs):
        order.append("rip")
        return raw_titles

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", side_effect=fake_rip) as mock_rip, \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", side_effect=fake_guide), \
         patch("ripper.namer.identify", return_value=named_titles), \
         patch("ripper.transfer.send_all", return_value=["remote/path"]), \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=1, show="The Office")

    assert order == ["guide", "rip"]
    assert mock_rip.call_args.kwargs["max_title_secs"] == 2106  # 1404s × 1.5


def test_main_loop_uses_content_id_when_enabled(tmp_path):
    """With --content-id, naming comes from identify.name_by_content (guide-matched),
    not the legacy playback-order namer."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [
        {"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0},
        {"path": disc_path / "title_t01.mkv", "duration_secs": 1320, "title_index": 1},
    ]
    guide = [
        {"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404},
        {"index": 2, "index_end": None, "name": "Diversity Day", "runtime_secs": 1332},
    ]
    # Content-ID resolves them E01/E02 regardless of disc order.
    identified = [
        {**raw_titles[0], "episode": 1, "index_end": None, "confidence": 0.9, "method": "frames"},
        {**raw_titles[1], "episode": 2, "index_end": None, "confidence": 0.9, "method": "frames"},
    ]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.namer.identify") as mock_namer, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]) as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=1, show="The Office", content_id=True)

    mock_namer.assert_not_called()  # legacy namer bypassed
    sent = mock_send.call_args[0][0]
    assert [t["jellyfin_filename"] for t in sent] == ["The.Office.S01E01.mkv", "The.Office.S01E02.mkv"]


def test_main_loop_content_id_falls_back_to_namer_when_nothing_matches(tmp_path):
    """If content-ID keeps nothing (all unmatched), fall back to the legacy namer
    rather than transfer nothing."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    guide = [{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}]
    named_titles = _make_named_titles(tmp_path)
    unmatched = [{**raw_titles[0], "episode": None, "index_end": None, "confidence": 0.0, "method": "none"}]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=unmatched), \
         patch("ripper.namer.identify", return_value=named_titles) as mock_namer, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]) as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=1, show="The Office", content_id=True)

    mock_namer.assert_called_once()  # fell back to legacy naming
    sent = mock_send.call_args[0][0]
    assert [t["jellyfin_filename"] for t in sent] == ["Friends.S01E01.mkv"]


def test_main_loop_dry_run_stops_before_transfer(tmp_path):
    """--dry-run rips + names but must not transfer, scan, eject, or delete the rip;
    it returns after one disc instead of looping."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    guide = [{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}]
    identified = [{**raw_titles[0], "episode": 1, "index_end": None,
                   "confidence": 0.9, "method": "subtitles"}]

    # No StopIteration needed: dry-run returns after the single disc.
    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc", return_value=("THE_OFFICE_DISC1", disc_path)), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.transfer.send_all") as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan") as mock_scan, \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp") as mock_cleanup, \
         patch("ripper.eject_disc") as mock_eject:
        import ripper
        ripper.main(season=1, show="The Office", content_id=True, dry_run=True)

    mock_send.assert_not_called()      # nothing transferred
    mock_scan.assert_not_called()      # no Jellyfin scan
    mock_discord.assert_not_called()   # no success ping
    mock_cleanup.assert_not_called()   # rip kept for a later real transfer
    mock_eject.assert_not_called()     # disc left in the drive


def test_main_loop_approve_transfers_on_approval(tmp_path):
    """--approve posts the mapping for approval; on Approve it transfers as normal."""
    from modules.approval import Decision
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    guide = [{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}]
    identified = [{**raw_titles[0], "episode": 1, "index_end": None,
                   "confidence": 0.98, "method": "subtitles"}]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.approval.request_approval",
               return_value=Decision(True, "approved by dillon")) as mock_approve, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]) as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp") as mock_cleanup, \
         patch("ripper.eject_disc") as mock_eject:
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=1, show="The Office", content_id=True, approve=True)

    mock_approve.assert_called_once()
    sent = mock_send.call_args[0][0]
    assert [t["jellyfin_filename"] for t in sent] == ["The.Office.S01E01.mkv"]
    mock_cleanup.assert_called_once()  # approved run cleans up + ejects normally
    mock_eject.assert_called_once()


def test_main_loop_approve_holds_files_when_declined(tmp_path):
    """On Fix/timeout/misconfig the mapping must NOT transfer, and the rip + disc are
    held (no cleanup, no eject) for manual handling."""
    from modules.approval import Decision
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    guide = [{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}]
    identified = [{**raw_titles[0], "episode": 1, "index_end": None,
                   "confidence": 0.98, "method": "subtitles"}]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.approval.request_approval",
               return_value=Decision(False, "approval timed out")), \
         patch("ripper.transfer.send_all") as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan") as mock_scan, \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp") as mock_cleanup, \
         patch("ripper.eject_disc") as mock_eject:
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=1, show="The Office", content_id=True, approve=True)

    mock_send.assert_not_called()     # nothing transferred
    mock_scan.assert_not_called()     # no Jellyfin scan
    mock_cleanup.assert_not_called()  # rip held for manual handling
    mock_eject.assert_not_called()    # disc left in the drive
    mock_discord.assert_called_once()  # posts a "held" failure notice
    assert mock_discord.call_args.kwargs["success"] is False


def test_main_loop_sends_failure_discord_on_rip_error(tmp_path):
    from modules.ripper import RipError
    config = _make_config(tmp_path)
    disc_path = tmp_path / "BAD_DISC"
    disc_path.mkdir()

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc", side_effect=[("BAD_DISC", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", side_effect=RipError("makemkvcon crashed")), \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main()

    mock_discord.assert_called_once_with(
        [], success=False, error="makemkvcon crashed", config=config
    )


def test_main_loop_review_ui_transfers_curated_titles(tmp_path):
    """--review-ui replaces the proposal with the user's curated list and transfers
    exactly that — including a title the pipeline had dropped."""
    from modules.review_ui import ReviewDecision
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    guide = [{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}]
    identified = [{**raw_titles[0], "episode": 1, "index_end": None,
                   "confidence": 0.98, "method": "subtitles"}]
    curated = [{**raw_titles[0], "episode": 1, "jellyfin_filename": "The.Office.S01E01.mkv",
                "episode_name": "Pilot", "media_type": "tv", "destination": "tvshows",
                "is_extra": False, "method": "review-ui", "confidence": 1.0}]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.review_ui_mod.request_review",
               return_value=ReviewDecision(True, "reviewed via web UI", titles=curated)) as mock_review, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]) as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp") as mock_cleanup, \
         patch("ripper.eject_disc") as mock_eject:
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=1, show="The Office", content_id=True, review_ui=True)

    # The review got the FULL rip list plus the proposal and the guide.
    args = mock_review.call_args[0]
    assert args[0] == raw_titles          # all_titles
    # The guide reaches the review page season-tagged (so a volume disc can show two
    # seasons of slots), and the season override travels as a list.
    assert args[3] == [{**e, "season": 1} for e in guide]
    assert args[4] == "The Office"
    assert args[5] == [1]
    # And exactly the curated list transferred.
    assert mock_send.call_args[0][0] is curated
    mock_cleanup.assert_called_once()
    mock_eject.assert_called_once()


def test_main_loop_review_ui_holds_files_on_timeout(tmp_path):
    """Review timeout/failure must hold like an approval decline: no transfer, no
    cleanup, no eject, and a failure notice posted."""
    from modules.review_ui import ReviewDecision
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    guide = [{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}]
    identified = [{**raw_titles[0], "episode": 1, "index_end": None,
                   "confidence": 0.98, "method": "subtitles"}]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.review_ui_mod.request_review",
               return_value=ReviewDecision(False, "review timed out after 1800s")), \
         patch("ripper.transfer.send_all") as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan") as mock_scan, \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp") as mock_cleanup, \
         patch("ripper.eject_disc") as mock_eject:
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=1, show="The Office", content_id=True, review_ui=True)

    mock_send.assert_not_called()
    mock_scan.assert_not_called()
    mock_cleanup.assert_not_called()  # rip held for manual handling
    mock_eject.assert_not_called()    # disc left in the drive
    mock_discord.assert_called_once()
    assert mock_discord.call_args.kwargs["success"] is False


def test_main_loop_review_ui_skips_movie_disc_and_transfers(tmp_path):
    """A movie disc has no episode slots to curate, so --review-ui is skipped: the
    review server is never launched and the namer's feature transfers directly."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "INCEPTION"
    disc_path.mkdir()

    raw_titles = [
        {"path": disc_path / "title_t00.mkv", "duration_secs": 6127, "title_index": 0},
        {"path": disc_path / "title_t01.mkv", "duration_secs": 300, "title_index": 1},
    ]
    feature = {
        "path": tmp_path / "title_t00.mkv", "duration_secs": 6127, "title_index": 0,
        "jellyfin_filename": "Inception.2010.mkv", "media_type": "movie",
        "destination": "movies", "is_extra": False,
    }
    trailer = {
        "path": tmp_path / "title_t01.mkv", "duration_secs": 300, "title_index": 1,
        "jellyfin_filename": "Inception.2010.Trailer.mkv", "media_type": "movie",
        "destination": "movies", "is_extra": True,
    }

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("INCEPTION", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.namer.identify", return_value=[feature, trailer]), \
         patch("ripper.review_ui_mod.request_review") as mock_review, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]) as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord") as mock_discord, \
         patch("ripper.cleanup_temp") as mock_cleanup, \
         patch("ripper.eject_disc") as mock_eject:
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(review_ui=True)

    mock_review.assert_not_called()  # no review page for a movie
    sent = mock_send.call_args[0][0]
    assert [t["jellyfin_filename"] for t in sent] == ["Inception.2010.mkv"]
    mock_discord.assert_called_once_with(
        ["Inception.2010.mkv"], success=True, config=config
    )
    mock_cleanup.assert_called_once()  # transferred run cleans up + ejects normally
    mock_eject.assert_called_once()


def test_main_loop_review_ui_supersedes_approve(tmp_path):
    """When both flags are passed, the web review runs and the Discord approval
    gate is skipped for the run."""
    from modules.review_ui import ReviewDecision
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    guide = [{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}]
    identified = [{**raw_titles[0], "episode": 1, "index_end": None,
                   "confidence": 0.98, "method": "subtitles"}]
    curated = [{**raw_titles[0], "episode": 1, "jellyfin_filename": "The.Office.S01E01.mkv",
                "media_type": "tv", "destination": "tvshows", "is_extra": False}]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.review_ui_mod.request_review",
               return_value=ReviewDecision(True, "reviewed via web UI", titles=curated)), \
         patch("ripper.approval.request_approval") as mock_approve, \
         patch("ripper.transfer.send_all") as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=1, show="The Office", content_id=True,
                        approve=True, review_ui=True)

    mock_approve.assert_not_called()  # Discord gate skipped this run
    mock_send.assert_called_once()


def test_main_loop_once_exits_after_a_single_disc(tmp_path):
    """--once handles one disc and returns, instead of looping to wait for another.

    Without it a one-off run with --show/--season keeps that override in force, so
    the NEXT disc inserted gets named as an episode of this season."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "FRIENDS_S1D1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    named_titles = _make_named_titles(tmp_path)

    # Two discs are queued. A looping ripper would pick up the second one; --once
    # must leave it untouched.
    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("FRIENDS_S1D1", disc_path), ("FRIENDS_S1D2", disc_path)]) as mock_wait, \
         patch("ripper.disc_ripper.rip", return_value=raw_titles) as mock_rip, \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.namer.identify", return_value=named_titles), \
         patch("ripper.transfer.send_all"), \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc") as mock_eject:
        import ripper
        ripper.main(once=True)  # returns on its own — no StopIteration needed

    assert mock_wait.call_count == 1
    assert mock_rip.call_count == 1
    mock_eject.assert_called_once()  # the disc it did handle still gets ejected


def test_main_loop_once_exits_even_when_the_rip_is_held(tmp_path):
    """A held review/approval keeps the files and the disc — and with --once it
    must still end the run rather than sit waiting for a disc that can't come out."""
    from modules.review_ui import ReviewDecision
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_DISC1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    guide = [{"index": 1, "index_end": None, "name": "Pilot", "runtime_secs": 1404}]
    identified = [{**raw_titles[0], "episode": 1, "index_end": None,
                   "confidence": 0.9, "method": "subtitles"}]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_DISC1", disc_path), ("SOME_MOVIE", disc_path)]) as mock_wait, \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=guide), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.review_ui_mod.request_review",
               return_value=ReviewDecision(False, "timed out", titles=[])), \
         patch("ripper.transfer.send_all") as mock_send, \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp") as mock_cleanup, \
         patch("ripper.eject_disc") as mock_eject:
        import ripper
        ripper.main(season=1, show="The Office", content_id=True, review_ui=True, once=True)

    assert mock_wait.call_count == 1
    mock_send.assert_not_called()
    mock_cleanup.assert_not_called()  # held: files kept for manual handling
    mock_eject.assert_not_called()    # held: disc left in the drive


def test_main_loop_without_once_keeps_watching_after_a_disc(tmp_path):
    """The daemon's default is unchanged: one disc done, straight back to waiting."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "FRIENDS_S1D1"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    named_titles = _make_named_titles(tmp_path)

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("FRIENDS_S1D1", disc_path), StopIteration]) as mock_wait, \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.namer.identify", return_value=named_titles), \
         patch("ripper.transfer.send_all"), \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main()

    assert mock_wait.call_count == 2  # went back for another disc


# --- volume discs (two seasons on one disc) ----------------------------------

def test_seasons_arg_accepts_one_season_a_list_and_a_range():
    import ripper
    assert ripper._seasons_arg("4") == [4]
    assert ripper._seasons_arg("4,5") == [4, 5]
    assert ripper._seasons_arg("4-6") == [4, 5, 6]
    assert ripper._seasons_arg("5,4,4") == [4, 5]   # sorted + deduped
    assert ripper._seasons_arg("4,") == [4]         # trailing comma is unambiguous


def test_seasons_arg_refuses_anything_ambiguous():
    import argparse

    import ripper
    # A mistyped season silently misnames a whole disc — refuse rather than guess.
    for bad in ("", "abc", "6-4", "4,x", "100", "-1", "4,5,6,7"):
        with pytest.raises(argparse.ArgumentTypeError):
            ripper._seasons_arg(bad)


def test_as_seasons_normalizes_int_list_and_none():
    import ripper
    assert ripper.as_seasons(4) == [4]
    assert ripper.as_seasons([5, 4]) == [4, 5]
    assert ripper.as_seasons(None) == []


def test_fetch_guide_pools_both_seasons_in_broadcast_order(tmp_path):
    """A volume disc's candidate pool is both seasons at once, each entry tagged with
    the season it came from."""
    import ripper
    config = _make_config(tmp_path)
    by_season = {
        4: [{"index": 29, "name": "PTV"}, {"index": 30, "name": "Brian Sings"}],
        5: [{"index": 1, "name": "Stewie Loves Lois"}],
    }
    with patch("ripper.episode_guide.get_season_episodes",
               side_effect=lambda show, s, cfg: by_season[s]):
        guide = ripper.fetch_guide("Family Guy", [4, 5], config)

    assert [(e["season"], e["index"]) for e in guide] == [(4, 29), (4, 30), (5, 1)]


def test_fetch_guide_keeps_the_seasons_it_can_when_one_lookup_fails(tmp_path):
    """Half a pool still matches half the disc; the rest surfaces as unmatched at the
    review gate instead of sinking the whole guide."""
    import ripper
    from modules.episode_guide import EpisodeGuideError
    config = _make_config(tmp_path)

    def fake(show, s, cfg):
        if s == 5:
            raise EpisodeGuideError("TMDB down")
        return [{"index": 29, "name": "PTV"}]

    with patch("ripper.episode_guide.get_season_episodes", side_effect=fake):
        guide = ripper.fetch_guide("Family Guy", [4, 5], config)
    assert [(e["season"], e["index"]) for e in guide] == [(4, 29)]

    with patch("ripper.episode_guide.get_season_episodes",
               side_effect=EpisodeGuideError("all down")):
        assert ripper.fetch_guide("Family Guy", [4, 5], config) is None


def test_main_loop_names_a_volume_disc_from_both_seasons(tmp_path):
    """The whole point: one disc, two seasons, and each title filed under the season
    of the episode it actually matched — not all forced into the first one."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "FAMILY_GUY_VOL4_D3"
    disc_path.mkdir()

    raw_titles = [
        {"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0},
        {"path": disc_path / "title_t01.mkv", "duration_secs": 1320, "title_index": 1},
    ]
    by_season = {
        4: [{"index": 30, "index_end": None, "name": "Brian Sings", "runtime_secs": 1320}],
        5: [{"index": 1, "index_end": None, "name": "Stewie Loves Lois", "runtime_secs": 1320}],
    }
    # The disc straddles the boundary: one title from each season.
    identified = [
        {**raw_titles[0], "season": 4, "episode": 30, "index_end": None,
         "confidence": 0.95, "method": "subtitles"},
        {**raw_titles[1], "season": 5, "episode": 1, "index_end": None,
         "confidence": 0.95, "method": "subtitles"},
    ]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("FAMILY_GUY_VOL4_D3", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes",
               side_effect=lambda show, s, cfg: by_season[s]), \
         patch("ripper.identify.identify_title", side_effect=identified), \
         patch("ripper.namer.identify") as mock_namer, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]) as mock_send, \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=[4, 5], show="Family Guy", content_id=True)

    mock_namer.assert_not_called()
    sent = mock_send.call_args[0][0]
    assert [t["jellyfin_filename"] for t in sent] == \
        ["Family.Guy.S04E30.mkv", "Family.Guy.S05E01.mkv"]
    # The episode names come from the right season's guide, too.
    assert [t["episode_name"] for t in sent] == ["Brian Sings", "Stewie Loves Lois"]


def test_main_loop_volume_disc_identifies_against_both_seasons_at_once(tmp_path):
    """Every title is offered the whole pool — a disc that turns out to be all one
    season still comes out right, it just considered more candidates."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "FAMILY_GUY_VOL4_D3"
    disc_path.mkdir()
    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    by_season = {
        4: [{"index": 30, "index_end": None, "name": "Brian Sings", "runtime_secs": 1320}],
        5: [{"index": 1, "index_end": None, "name": "Stewie Loves Lois", "runtime_secs": 1320}],
    }
    identified = [{**raw_titles[0], "season": 4, "episode": 30, "index_end": None,
                   "confidence": 0.95, "method": "subtitles"}]

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("FAMILY_GUY_VOL4_D3", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes",
               side_effect=lambda show, s, cfg: by_season[s]), \
         patch("ripper.identify.identify_title", side_effect=identified) as mock_identify, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]), \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=[4, 5], show="Family Guy", content_id=True)

    candidates = mock_identify.call_args[0][1]
    assert [(c["season"], c["index"]) for c in candidates] == [(4, 30), (5, 1)]


def test_volume_disc_withholds_the_disc_number_from_the_legacy_namer(tmp_path):
    """Disc 2 of Volume 8 is not disc 2 of season 7. The legacy namer reads --disc as
    'disc N of THAT season', so on a volume disc the hint is withheld rather than
    asserted falsely."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "FAMILY_GUY_VOL8_D2"
    disc_path.mkdir()

    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    by_season = {
        7: [{"index": 16, "index_end": None, "name": "a", "runtime_secs": 1320}],
        8: [{"index": 1, "index_end": None, "name": "b", "runtime_secs": 1320}],
    }
    # Content-ID matches nothing → the legacy namer takes over.
    unmatched = [{**raw_titles[0], "season": None, "episode": None, "index_end": None,
                  "confidence": 0.0, "method": "none"}]
    named_titles = _make_named_titles(tmp_path)

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("FAMILY_GUY_VOL8_D2", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes",
               side_effect=lambda show, s, cfg: by_season[s]), \
         patch("ripper.identify.identify_title", side_effect=unmatched), \
         patch("ripper.namer.identify", return_value=named_titles) as mock_namer, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]), \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=[7, 8], disc=2, show="Family Guy", content_id=True)

    assert mock_namer.call_args.kwargs["disc"] is None
    assert mock_namer.call_args.kwargs["season"] == 7   # still the lower season


def test_single_season_disc_still_passes_the_disc_number(tmp_path):
    """The hint is real when the disc IS one season's — that path is untouched."""
    config = _make_config(tmp_path)
    disc_path = tmp_path / "THE_OFFICE_S2D2"
    disc_path.mkdir()
    raw_titles = [{"path": disc_path / "title_t00.mkv", "duration_secs": 1320, "title_index": 0}]
    named_titles = _make_named_titles(tmp_path)

    with patch("ripper.load_config", return_value=config), \
         patch("ripper.disc_watcher.wait_for_disc",
               side_effect=[("THE_OFFICE_S2D2", disc_path), StopIteration]), \
         patch("ripper.disc_ripper.rip", return_value=raw_titles), \
         patch("ripper.transfer.list_existing_episodes", return_value=[]), \
         patch("ripper.episode_guide.get_season_episodes", return_value=[]), \
         patch("ripper.namer.identify", return_value=named_titles) as mock_namer, \
         patch("ripper.transfer.send_all", return_value=["remote/path"]), \
         patch("ripper.notifier.trigger_jellyfin_scan"), \
         patch("ripper.notifier.send_discord"), \
         patch("ripper.cleanup_temp"), \
         patch("ripper.eject_disc"):
        with pytest.raises(StopIteration):
            import ripper
            ripper.main(season=2, disc=2, show="The Office")

    assert mock_namer.call_args.kwargs["disc"] == 2
