# DVD Auto-Ripper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a persistent macOS background service that monitors for disc insertion, rips all titles with MakeMKV, names them with the Anthropic API, transfers to a home server via SCP, triggers a Jellyfin scan, and sends a Discord notification.

**Architecture:** A thin orchestrator (`ripper.py`) drives a linear pipeline of focused modules — disc_watcher → ripper → namer → transfer → notifier — each with a single public function and clear input/output contract. The orchestrator owns all retry logic and error handling, calling into modules that raise typed exceptions on failure.

**Tech Stack:** Python 3.11+, `anthropic` SDK, `python-dotenv`, `subprocess` for SCP/MakeMKV/drutil, `urllib` for HTTP calls (no extra HTTP library needed), `pytest` + `unittest.mock` for tests.

**System dependencies (must be installed separately):**
- `makemkvcon` — MakeMKV CLI: https://www.makemkv.com/download/
- `scp` — ships with macOS
- `drutil` — ships with macOS

---

## File Map

| File | Responsibility |
|---|---|
| `ripper.py` | Entry point; main polling loop; orchestrates pipeline; handles errors |
| `config.py` | Loads `.env`, validates required keys, returns typed `Config` dataclass |
| `modules/__init__.py` | Empty |
| `modules/disc_watcher.py` | Polls `/Volumes/` every 5s; returns first detected optical disc |
| `modules/ripper.py` | Runs `makemkvcon`; parses title info; returns list of title dicts |
| `modules/namer.py` | Calls Anthropic API; returns Jellyfin-compatible filenames merged onto title dicts |
| `modules/transfer.py` | SCPs each file to home server; retries 3× with backoff |
| `modules/notifier.py` | POSTs Jellyfin scan + Discord webhook; retries; never raises |
| `tests/test_config.py` | Tests for config validation |
| `tests/test_disc_watcher.py` | Tests for disc detection logic |
| `tests/test_ripper.py` | Tests for MakeMKV parsing and rip orchestration |
| `tests/test_namer.py` | Tests for Anthropic API call, JSON parsing, retry |
| `tests/test_transfer.py` | Tests for SCP invocation and retry logic |
| `tests/test_notifier.py` | Tests for Jellyfin and Discord HTTP calls |
| `.env.example` | Config template |
| `requirements.txt` | Python dependencies |
| `pytest.ini` | Pytest configuration |
| `com.dillon.ripper.plist` | launchd plist template |
| `install.sh` | Registers plist with launchd |

---

## Task 1: Project Skeleton

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `pytest.ini`
- Create: `modules/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create `requirements.txt`**

```
anthropic
python-dotenv
pytest
```

- [ ] **Step 2: Install dependencies**

```bash
pip install -r requirements.txt
```

Expected: all packages install without error.

- [ ] **Step 3: Create `.env.example`**

```
ANTHROPIC_API_KEY=
SERVER_IP=100.100.212.32
SERVER_USER=dillon
JELLYFIN_URL=http://100.100.212.32:8096
JELLYFIN_API_KEY=
DISCORD_WEBHOOK_URL=
TEMP_DIR=/tmp/ai-ripper
```

- [ ] **Step 4: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
```

- [ ] **Step 5: Create empty `modules/__init__.py` and `tests/__init__.py`**

Both files are empty. Just `touch` them.

```bash
mkdir -p modules tests
touch modules/__init__.py tests/__init__.py
```

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .env.example pytest.ini modules/__init__.py tests/__init__.py
git commit -m "feat: project skeleton"
```

---

## Task 2: `config.py`

**Files:**
- Create: `config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
import os
import pytest
from unittest.mock import patch


def test_load_config_returns_config_with_all_fields():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "SERVER_IP": "100.100.212.32",
        "SERVER_USER": "dillon",
        "JELLYFIN_URL": "http://100.100.212.32:8096",
        "JELLYFIN_API_KEY": "jellyfin-key",
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test",
        "TEMP_DIR": "/tmp/ai-ripper",
    }
    with patch.dict(os.environ, env, clear=True):
        from config import load_config
        config = load_config()
    assert config.anthropic_api_key == "sk-ant-test"
    assert config.server_ip == "100.100.212.32"
    assert config.server_user == "dillon"
    assert config.jellyfin_url == "http://100.100.212.32:8096"
    assert config.jellyfin_api_key == "jellyfin-key"
    assert config.discord_webhook_url == "https://discord.com/api/webhooks/test"
    assert str(config.temp_dir) == "/tmp/ai-ripper"


def test_load_config_raises_on_missing_key():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        # SERVER_IP intentionally missing
        "SERVER_USER": "dillon",
        "JELLYFIN_URL": "http://100.100.212.32:8096",
        "JELLYFIN_API_KEY": "jellyfin-key",
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test",
        "TEMP_DIR": "/tmp/ai-ripper",
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match="SERVER_IP"):
            from config import load_config
            load_config()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_config.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `config` doesn't exist yet.

- [ ] **Step 3: Write `config.py`**

```python
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


@dataclass
class Config:
    anthropic_api_key: str
    server_ip: str
    server_user: str
    jellyfin_url: str
    jellyfin_api_key: str
    discord_webhook_url: str
    temp_dir: Path


def load_config() -> Config:
    load_dotenv()

    required = [
        "ANTHROPIC_API_KEY",
        "SERVER_IP",
        "SERVER_USER",
        "JELLYFIN_URL",
        "JELLYFIN_API_KEY",
        "DISCORD_WEBHOOK_URL",
        "TEMP_DIR",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return Config(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        server_ip=os.environ["SERVER_IP"],
        server_user=os.environ["SERVER_USER"],
        jellyfin_url=os.environ["JELLYFIN_URL"],
        jellyfin_api_key=os.environ["JELLYFIN_API_KEY"],
        discord_webhook_url=os.environ["DISCORD_WEBHOOK_URL"],
        temp_dir=Path(os.environ["TEMP_DIR"]),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: config loader with validation"
```

---

## Task 3: `modules/disc_watcher.py`

**Files:**
- Create: `modules/disc_watcher.py`
- Create: `tests/test_disc_watcher.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_disc_watcher.py`:

```python
from pathlib import Path
from unittest.mock import patch
from modules.disc_watcher import _is_optical_disc, wait_for_disc


def test_is_optical_disc_dvd(tmp_path):
    (tmp_path / "VIDEO_TS").mkdir()
    assert _is_optical_disc(tmp_path) is True


def test_is_optical_disc_bluray(tmp_path):
    (tmp_path / "BDMV").mkdir()
    assert _is_optical_disc(tmp_path) is True


def test_is_optical_disc_usb_drive(tmp_path):
    # No VIDEO_TS or BDMV
    (tmp_path / "Documents").mkdir()
    assert _is_optical_disc(tmp_path) is False


def test_wait_for_disc_returns_on_new_optical_volume(tmp_path):
    # Simulate: first poll sees only existing volumes, second poll sees disc added
    disc_path = tmp_path / "FRIENDS_S1D2"
    disc_path.mkdir()
    (disc_path / "VIDEO_TS").mkdir()

    existing = tmp_path / "Macintosh HD"
    existing.mkdir()

    # _list_volumes is called twice: initial snapshot, then first poll
    volume_snapshots = [
        {existing},              # initial known set
        {existing, disc_path},   # poll finds new disc
    ]

    with patch("modules.disc_watcher._list_volumes", side_effect=volume_snapshots), \
         patch("modules.disc_watcher.POLL_INTERVAL", 0):
        name, path = wait_for_disc()

    assert name == "FRIENDS_S1D2"
    assert path == disc_path
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_disc_watcher.py -v
```

Expected: `ImportError` — module doesn't exist yet.

- [ ] **Step 3: Write `modules/disc_watcher.py`**

```python
import time
from pathlib import Path
from typing import Set, Tuple

VOLUMES_PATH = Path("/Volumes")
POLL_INTERVAL = 5  # seconds


def _list_volumes() -> Set[Path]:
    """Return current set of /Volumes entries. Isolated for testability."""
    return set(VOLUMES_PATH.iterdir())


def _is_optical_disc(volume_path: Path) -> bool:
    return (volume_path / "VIDEO_TS").exists() or (volume_path / "BDMV").exists()


def wait_for_disc() -> Tuple[str, Path]:
    """Block until an optical disc is inserted. Returns (volume_name, volume_path)."""
    known = _list_volumes()
    while True:
        current = _list_volumes()
        new_volumes = current - known
        for path in new_volumes:
            if _is_optical_disc(path):
                return path.name, path
        known = current
        time.sleep(POLL_INTERVAL)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_disc_watcher.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add modules/disc_watcher.py tests/test_disc_watcher.py
git commit -m "feat: disc watcher polls /Volumes for optical disc insertion"
```

---

## Task 4: `modules/ripper.py`

**Files:**
- Create: `modules/ripper.py`
- Create: `tests/test_ripper.py`

MakeMKV is invoked in two phases:
1. `makemkvcon -r info disc:0` — robot-mode structured output, parse title durations
2. `makemkvcon mkv disc:0 all <temp_dir>` — rip all titles, streaming output

MakeMKV info output format (one line per field):
```
TINFO:<title_idx>,<code>,<flags>,"<value>"
```
Code 9 = duration as `H:MM:SS`. MakeMKV names output files `title_t00.mkv`, `title_t01.mkv`, etc., matching the title index.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ripper.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from modules.ripper import _parse_info, _hms_to_secs, _parse_title_index, rip, RipError


def test_hms_to_secs_standard():
    assert _hms_to_secs("1:42:07") == 6127


def test_hms_to_secs_zero_hours():
    assert _hms_to_secs("0:22:15") == 1335


def test_parse_info_extracts_durations():
    output = (
        'MSG:1005,0,0,"MakeMKV started"\n'
        'TCOUNT:2\n'
        'TINFO:0,9,0,"1:42:07"\n'
        'TINFO:0,27,0,"Some Movie"\n'
        'TINFO:1,9,0,"0:22:15"\n'
        'TINFO:1,27,0,"Bonus Feature"\n'
    )
    result = _parse_info(output)
    assert result[0]["duration_secs"] == 6127
    assert result[1]["duration_secs"] == 1335


def test_parse_title_index_from_filename():
    assert _parse_title_index("title_t00.mkv") == 0
    assert _parse_title_index("title_t03.mkv") == 3
    assert _parse_title_index("title_t12.mkv") == 12


def test_rip_returns_titles_with_duration(tmp_path):
    # title_t00: 1:42:07 = 6127s (kept), title_t01: 0:04:15 = 255s < 300s threshold (filtered)
    info_output = (
        'TINFO:0,9,0,"1:42:07"\n'
        'TINFO:1,9,0,"0:04:15"\n'
    )

    # Create fake output files
    (tmp_path / "title_t00.mkv").write_bytes(b"fake mkv data")
    (tmp_path / "title_t01.mkv").write_bytes(b"fake mkv data")

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        if "info" in cmd:
            mock.stdout = info_output
        return mock

    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        titles = rip(Path("/Volumes/FAKE_DISC"), tmp_path)

    assert len(titles) == 1  # title_t01 is 255s < 300s minimum, filtered out
    assert titles[0]["title_index"] == 0
    assert titles[0]["duration_secs"] == 6127
    assert titles[0]["path"] == tmp_path / "title_t00.mkv"


def test_rip_raises_on_makemkv_failure(tmp_path):
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        if "info" in cmd:
            mock.returncode = 0
            mock.stdout = ""
        else:
            mock.returncode = 1
        return mock

    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        with pytest.raises(RipError, match="exit code 1"):
            rip(Path("/Volumes/FAKE_DISC"), tmp_path)


def test_rip_raises_when_no_mkv_files_produced(tmp_path):
    info_output = 'TINFO:0,9,0,"1:42:07"\n'

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = info_output
        return mock

    # Don't create any .mkv files in tmp_path
    with patch("modules.ripper.subprocess.run", side_effect=fake_run):
        with pytest.raises(RipError, match="No MKV files"):
            rip(Path("/Volumes/FAKE_DISC"), tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_ripper.py -v
```

Expected: `ImportError` — module doesn't exist yet.

- [ ] **Step 3: Write `modules/ripper.py`**

```python
import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

MIN_TITLE_DURATION_SECS = 300  # 5 minutes — skip extras/menus


class RipError(Exception):
    pass


def _hms_to_secs(hms: str) -> int:
    """Convert 'H:MM:SS' or 'MM:SS' to total seconds."""
    parts = hms.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0


def _parse_info(output: str) -> Dict[int, Dict]:
    """Parse 'makemkvcon -r info' output into {title_idx: {duration_secs}}."""
    titles: Dict[int, Dict] = {}
    for line in output.splitlines():
        m = re.match(r'TINFO:(\d+),(\d+),\d+,"(.*)"', line)
        if not m:
            continue
        title_idx, code = int(m.group(1)), int(m.group(2))
        value = m.group(3)
        if title_idx not in titles:
            titles[title_idx] = {}
        if code == 9:  # duration field
            titles[title_idx]["duration_secs"] = _hms_to_secs(value)
    return titles


def _parse_title_index(filename: str) -> int:
    """Extract title index from MakeMKV output name like 'title_t03.mkv'."""
    m = re.search(r"title_t(\d+)", filename)
    return int(m.group(1)) if m else -1


def rip(volume_path: Path, temp_dir: Path) -> List[Dict]:
    """
    Rip all titles from the disc to temp_dir.
    Returns list of dicts: [{path, duration_secs, title_index}].
    Titles shorter than MIN_TITLE_DURATION_SECS are excluded.
    Raises RipError on failure.
    """
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: get title metadata
    log.info("Querying disc info...")
    info_result = subprocess.run(
        ["makemkvcon", "-r", "info", "disc:0"],
        capture_output=True,
        text=True,
    )
    title_info = _parse_info(info_result.stdout)

    # Phase 2: rip all titles
    log.info(f"Ripping disc to {temp_dir}...")
    rip_result = subprocess.run(
        ["makemkvcon", "mkv", "disc:0", "all", str(temp_dir)],
        check=False,
    )
    if rip_result.returncode != 0:
        raise RipError(f"makemkvcon exited with exit code {rip_result.returncode}")

    mkv_files = sorted(temp_dir.glob("*.mkv"))
    if not mkv_files:
        raise RipError("No MKV files produced by makemkvcon")

    # Match output files to title info by index parsed from filename
    output = []
    for mkv in mkv_files:
        idx = _parse_title_index(mkv.name)
        info = title_info.get(idx, {})
        duration_secs = info.get("duration_secs", 0)

        if duration_secs < MIN_TITLE_DURATION_SECS:
            log.info(f"Skipping {mkv.name} (duration {duration_secs}s < {MIN_TITLE_DURATION_SECS}s)")
            continue

        output.append({
            "path": mkv,
            "duration_secs": duration_secs,
            "title_index": idx,
        })

    if not output:
        raise RipError("No valid titles found after filtering short titles")

    return output
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_ripper.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add modules/ripper.py tests/test_ripper.py
git commit -m "feat: ripper module wraps makemkvcon and parses title metadata"
```

---

## Task 5: `modules/namer.py`

**Files:**
- Create: `modules/namer.py`
- Create: `tests/test_namer.py`

The Anthropic API call receives the disc volume label and a list of title metadata, and returns a JSON array with Jellyfin-compatible filenames. If the JSON is malformed, one retry is attempted using a multi-turn correction prompt.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_namer.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from modules.namer import _duration_hms, identify, NamerError


def test_duration_hms_formatting():
    assert _duration_hms(6127) == "1:42:07"
    assert _duration_hms(1335) == "0:22:15"
    assert _duration_hms(3600) == "1:00:00"


def _make_titles(tmp_path):
    f1 = tmp_path / "title_t00.mkv"
    f2 = tmp_path / "title_t01.mkv"
    f1.write_bytes(b"")
    f2.write_bytes(b"")
    return [
        {"path": f1, "duration_secs": 1320, "title_index": 0},
        {"path": f2, "duration_secs": 1290, "title_index": 1},
    ]


def _mock_anthropic_response(text: str):
    mock_client = MagicMock()
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=text)]
    mock_client.messages.create.return_value = mock_message
    return mock_client


def test_identify_returns_named_titles(tmp_path):
    titles = _make_titles(tmp_path)
    api_response = (
        '[{"index": 0, "jellyfin_filename": "Friends.S01E01.mkv", "media_type": "tv", "destination": "tvshows"},'
        ' {"index": 1, "jellyfin_filename": "Friends.S01E02.mkv", "media_type": "tv", "destination": "tvshows"}]'
    )
    mock_client = _mock_anthropic_response(api_response)

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        result = identify("FRIENDS_S1D1", titles, "sk-ant-test")

    assert len(result) == 2
    assert result[0]["jellyfin_filename"] == "Friends.S01E01.mkv"
    assert result[0]["destination"] == "tvshows"
    assert result[0]["path"] == titles[0]["path"]
    assert result[1]["jellyfin_filename"] == "Friends.S01E02.mkv"


def test_identify_retries_on_malformed_json(tmp_path):
    titles = _make_titles(tmp_path)
    bad_response = "Here is the JSON: [invalid json"
    good_response = '[{"index": 0, "jellyfin_filename": "Friends.S01E01.mkv", "media_type": "tv", "destination": "tvshows"}]'

    mock_client = MagicMock()
    call_count = 0

    def fake_create(**kwargs):
        nonlocal call_count
        call_count += 1
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=bad_response if call_count == 1 else good_response)]
        return mock_message

    mock_client.messages.create.side_effect = fake_create

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        result = identify("FRIENDS_S1D1", titles, "sk-ant-test")

    assert call_count == 2
    assert len(result) == 1
    assert result[0]["jellyfin_filename"] == "Friends.S01E01.mkv"


def test_identify_raises_after_two_malformed_responses(tmp_path):
    titles = _make_titles(tmp_path)
    mock_client = _mock_anthropic_response("not json at all")

    with patch("modules.namer.anthropic.Anthropic", return_value=mock_client):
        with pytest.raises(NamerError, match="parse"):
            identify("FRIENDS_S1D1", titles, "sk-ant-test")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_namer.py -v
```

Expected: `ImportError` — module doesn't exist yet.

- [ ] **Step 3: Write `modules/namer.py`**

```python
import json
import logging
from pathlib import Path
from typing import Dict, List

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"


class NamerError(Exception):
    pass


def _duration_hms(secs: int) -> str:
    """Convert seconds to 'H:MM:SS' string."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _build_prompt(volume_name: str, titles: List[Dict]) -> str:
    title_list = [
        {
            "index": t["title_index"],
            "filename": t["path"].name,
            "duration": _duration_hms(t["duration_secs"]),
        }
        for t in titles
    ]
    return f"""You are identifying content from a DVD/Blu-ray disc for Jellyfin media server organization.

Disc volume label: {volume_name}
Titles on disc:
{json.dumps(title_list, indent=2)}

For each title, determine what movie or TV show episode it contains and return the correct Jellyfin-compatible filename.

Return ONLY a valid JSON array with no other text, markdown, or explanation:
[
  {{
    "index": <title_index as integer>,
    "jellyfin_filename": "<name>.mkv",
    "media_type": "movie" or "tv",
    "destination": "movies" or "tvshows"
  }}
]

Jellyfin filename conventions:
- TV shows: Show.Name.S01E01.mkv (use dots not spaces, season+episode zero-padded)
- Movies: Movie.Name.2023.mkv (include year if known, use dots not spaces)"""


def identify(volume_name: str, titles: List[Dict], api_key: str) -> List[Dict]:
    """
    Call Anthropic API to identify titles and generate Jellyfin-compatible filenames.
    Returns original title dicts merged with naming fields.
    Raises NamerError if JSON parsing fails after one retry.
    """
    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_prompt(volume_name, titles)

    messages = [{"role": "user", "content": prompt}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=messages,
    )
    raw = response.content[0].text.strip()

    naming_data = None
    try:
        naming_data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Malformed JSON from Anthropic; retrying with correction prompt")
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": "Your response was not valid JSON. Return ONLY the JSON array with no other text.",
        })
        retry_response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=messages,
        )
        raw2 = retry_response.content[0].text.strip()
        try:
            naming_data = json.loads(raw2)
        except json.JSONDecodeError as e:
            raise NamerError(f"Failed to parse naming response after retry: {e}") from e

    naming_by_index = {entry["index"]: entry for entry in naming_data}

    result = []
    for t in titles:
        idx = t["title_index"]
        if idx in naming_by_index:
            result.append({**t, **naming_by_index[idx]})
        else:
            log.warning(f"Title index {idx} not found in naming response; skipping")

    if not result:
        raise NamerError("No titles were matched in the naming API response")

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_namer.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add modules/namer.py tests/test_namer.py
git commit -m "feat: namer module calls Anthropic API for Jellyfin-compatible filenames"
```

---

## Task 6: `modules/transfer.py`

**Files:**
- Create: `modules/transfer.py`
- Create: `tests/test_transfer.py`

Each file is SCPed with up to 3 retries (4 total attempts) using 10s / 30s / 60s backoff. Uses Python's `for...else` loop: `else` fires when the loop exits without `break`, meaning all retries were exhausted.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transfer.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from config import Config
from modules.transfer import send_all, TransferError


def _make_config():
    return Config(
        anthropic_api_key="",
        server_ip="100.100.212.32",
        server_user="dillon",
        jellyfin_url="",
        jellyfin_api_key="",
        discord_webhook_url="",
        temp_dir=Path("/tmp/ai-ripper"),
    )


def _make_titled(tmp_path):
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


def test_send_all_calls_scp_with_correct_args(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch("modules.transfer.subprocess.run", return_value=mock_result) as mock_run:
        result = send_all(titles, config)

    expected_remote = "dillon@100.100.212.32:/home/dillon/jellyfin/media/tvshows/Friends.S01E01.mkv"
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "scp"
    assert str(titles[0]["path"]) in args
    assert expected_remote in args
    assert result == [expected_remote]


def test_send_all_retries_on_scp_failure(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    fail = MagicMock()
    fail.returncode = 1
    fail.stderr = "Connection refused"

    succeed = MagicMock()
    succeed.returncode = 0

    # Fail twice, succeed on third attempt
    side_effects = [fail, fail, succeed]

    with patch("modules.transfer.subprocess.run", side_effect=side_effects), \
         patch("modules.transfer.time.sleep"):
        result = send_all(titles, config)

    assert len(result) == 1


def test_send_all_raises_after_all_retries_fail(tmp_path):
    titles = _make_titled(tmp_path)
    config = _make_config()

    fail = MagicMock()
    fail.returncode = 1
    fail.stderr = "Connection refused"

    with patch("modules.transfer.subprocess.run", return_value=fail), \
         patch("modules.transfer.time.sleep"):
        with pytest.raises(TransferError, match="Friends.S01E01.mkv"):
            send_all(titles, config)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_transfer.py -v
```

Expected: `ImportError` — module doesn't exist yet.

- [ ] **Step 3: Write `modules/transfer.py`**

```python
import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)

RETRY_DELAYS = [10, 30, 60]  # seconds between attempts 1→2, 2→3, 3→4


class TransferError(Exception):
    pass


def _scp(local_path: Path, remote: str) -> None:
    result = subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", str(local_path), remote],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TransferError(f"scp failed (exit {result.returncode}): {result.stderr.strip()}")


def send_all(named_titles: List[Dict], config) -> List[str]:
    """
    SCP each title to the home server.
    Returns list of remote paths for successfully transferred files.
    Raises TransferError if any file fails all retries.
    """
    remote_paths = []

    for title in named_titles:
        filename = title["jellyfin_filename"]
        destination = title["destination"]
        remote = (
            f"{config.server_user}@{config.server_ip}:"
            f"/home/{config.server_user}/jellyfin/media/{destination}/{filename}"
        )

        last_error = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            if attempt > 0:
                delay = RETRY_DELAYS[attempt - 1]
                log.warning(f"Retrying transfer of {filename} in {delay}s (attempt {attempt + 1})")
                time.sleep(delay)
            try:
                _scp(title["path"], remote)
                remote_paths.append(remote)
                log.info(f"Transferred: {filename} → {remote}")
                break
            except TransferError as e:
                last_error = e
                log.warning(f"Transfer attempt {attempt + 1} failed: {e}")
        else:
            raise TransferError(
                f"Failed to transfer {filename} after {len(RETRY_DELAYS) + 1} attempts: {last_error}"
            )

    return remote_paths
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_transfer.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add modules/transfer.py tests/test_transfer.py
git commit -m "feat: transfer module SCPs files with retry backoff"
```

---

## Task 7: `modules/notifier.py`

**Files:**
- Create: `modules/notifier.py`
- Create: `tests/test_notifier.py`

Both `trigger_jellyfin_scan` and `send_discord` retry up to 3 times but **never raise** — failures are logged as warnings so a Jellyfin hiccup or a Discord outage doesn't block the pipeline.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notifier.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from config import Config
from modules.notifier import trigger_jellyfin_scan, send_discord


def _make_config():
    return Config(
        anthropic_api_key="",
        server_ip="",
        server_user="",
        jellyfin_url="http://100.100.212.32:8096",
        jellyfin_api_key="test-jellyfin-key",
        discord_webhook_url="https://discord.com/api/webhooks/test",
        temp_dir=Path("/tmp"),
    )


def test_trigger_jellyfin_scan_posts_to_correct_url():
    config = _make_config()
    with patch("modules.notifier.urllib.request.urlopen") as mock_urlopen:
        trigger_jellyfin_scan(config)

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://100.100.212.32:8096/Library/Refresh"
    assert req.get_header("X-emby-token") == "test-jellyfin-key"
    assert req.get_method() == "POST"


def test_trigger_jellyfin_scan_retries_on_failure():
    config = _make_config()
    import urllib.error

    call_count = 0

    def fake_urlopen(req, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise urllib.error.URLError("connection refused")

    with patch("modules.notifier.urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("modules.notifier.time.sleep"):
        trigger_jellyfin_scan(config)  # Should NOT raise

    assert call_count == 3


def test_trigger_jellyfin_scan_does_not_raise_after_all_retries_fail():
    config = _make_config()
    import urllib.error

    with patch("modules.notifier.urllib.request.urlopen",
               side_effect=urllib.error.URLError("timeout")), \
         patch("modules.notifier.time.sleep"):
        trigger_jellyfin_scan(config)  # Must not raise


def test_send_discord_success_message_contains_titles():
    config = _make_config()
    with patch("modules.notifier.urllib.request.urlopen") as mock_urlopen:
        send_discord(["Friends.S01E01.mkv", "Friends.S01E02.mkv"], success=True, config=config)

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    import json
    body = json.loads(req.data.decode())
    assert "✅" in body["content"]
    assert "Friends.S01E01.mkv" in body["content"]
    assert "Friends.S01E02.mkv" in body["content"]


def test_send_discord_failure_message_contains_error():
    config = _make_config()
    with patch("modules.notifier.urllib.request.urlopen") as mock_urlopen:
        send_discord([], success=False, config=config, error="makemkvcon crashed")

    req = mock_urlopen.call_args[0][0]
    import json
    body = json.loads(req.data.decode())
    assert "❌" in body["content"]
    assert "makemkvcon crashed" in body["content"]


def test_send_discord_does_not_raise_after_all_retries_fail():
    config = _make_config()
    import urllib.error

    with patch("modules.notifier.urllib.request.urlopen",
               side_effect=urllib.error.URLError("timeout")), \
         patch("modules.notifier.time.sleep"):
        send_discord(["Friends.S01E01.mkv"], success=True, config=config)  # Must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_notifier.py -v
```

Expected: `ImportError` — module doesn't exist yet.

- [ ] **Step 3: Write `modules/notifier.py`**

```python
import json
import logging
import time
import urllib.error
import urllib.request
from typing import List

log = logging.getLogger(__name__)

JELLYFIN_RETRY_DELAYS = [5, 10, 20]
DISCORD_RETRY_DELAYS = [5, 10, 20]


def _post(url: str, headers: dict, body: bytes | None = None) -> None:
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=headers,
    )
    urllib.request.urlopen(req, timeout=10)


def _with_retry(fn, delays: List[int], label: str) -> None:
    """Call fn(); if it raises URLError, retry with backoff. Never raises."""
    for attempt in range(len(delays) + 1):
        if attempt > 0:
            time.sleep(delays[attempt - 1])
        try:
            fn()
            return
        except urllib.error.URLError as e:
            log.warning(f"{label} attempt {attempt + 1} failed: {e}")
    log.warning(f"{label} failed after {len(delays) + 1} attempts — continuing")


def trigger_jellyfin_scan(config) -> None:
    """POST to Jellyfin Library/Refresh. Retries 3×. Never raises."""
    def do():
        _post(
            f"{config.jellyfin_url}/Library/Refresh",
            headers={"X-Emby-Token": config.jellyfin_api_key},
        )

    _with_retry(do, JELLYFIN_RETRY_DELAYS, "Jellyfin scan")
    log.info("Jellyfin library scan triggered")


def send_discord(titles: List[str], success: bool, config, error: str = "") -> None:
    """Send Discord webhook notification. Retries 3×. Never raises."""
    if success:
        title_lines = "\n".join(f"• {t}" for t in titles)
        content = f"✅ Rip complete! Added to Jellyfin:\n{title_lines}\n\nInsert next disc."
    else:
        content = f"❌ Ripper failed: {error}"

    payload = json.dumps({"content": content}).encode()

    def do():
        _post(
            config.discord_webhook_url,
            headers={"Content-Type": "application/json"},
            body=payload,
        )

    _with_retry(do, DISCORD_RETRY_DELAYS, "Discord webhook")
    log.info(f"Discord notification sent (success={success})")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_notifier.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add modules/notifier.py tests/test_notifier.py
git commit -m "feat: notifier posts Jellyfin scan and Discord webhook with retry"
```

---

## Task 8: `ripper.py` — Orchestrator

**Files:**
- Create: `ripper.py`
- Create: `tests/test_ripper_main.py`

The orchestrator imports `modules.ripper` as `disc_ripper` to avoid a name collision with the top-level `ripper.py` entry point.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ripper_main.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_ripper_main.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `ripper.py` doesn't exist yet.

- [ ] **Step 3: Write `ripper.py`**

```python
#!/usr/bin/env python3
"""DVD Auto-Ripper — main entry point."""
import logging
from pathlib import Path

from config import load_config
from modules import disc_watcher, namer, notifier, transfer
from modules import ripper as disc_ripper
from modules.namer import NamerError
from modules.ripper import RipError
from modules.transfer import TransferError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def cleanup_temp(temp_dir: Path) -> None:
    """Delete all .mkv files from temp_dir."""
    for f in temp_dir.glob("*.mkv"):
        f.unlink()
        log.info(f"Deleted temp file: {f}")


def eject_disc(volume_path: Path) -> None:
    """Eject the disc using drutil."""
    import subprocess
    subprocess.run(["drutil", "eject"], check=False)
    log.info("Disc ejected")


def main() -> None:
    config = load_config()
    log.info("DVD Auto-Ripper started. Waiting for disc...")

    while True:
        volume_name, volume_path = disc_watcher.wait_for_disc()
        log.info(f"Disc detected: {volume_name} at {volume_path}")

        try:
            titles = disc_ripper.rip(volume_path, config.temp_dir)
            log.info(f"Ripped {len(titles)} title(s)")

            named = namer.identify(volume_name, titles, config.anthropic_api_key)
            log.info(f"Named {len(named)} title(s)")

            transfer.send_all(named, config)
            log.info("Transfer complete")

            notifier.trigger_jellyfin_scan(config)
            notifier.send_discord(
                [t["jellyfin_filename"] for t in named], success=True, config=config
            )

        except RipError as e:
            log.error(f"Rip failed: {e}")
            notifier.send_discord([], success=False, error=str(e), config=config)

        except (TransferError, NamerError) as e:
            log.error(f"Pipeline failed: {e}")
            notifier.send_discord([], success=False, error=str(e), config=config)

        finally:
            cleanup_temp(config.temp_dir)
            eject_disc(volume_path)
            log.info("Ready for next disc.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_ripper_main.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run the full test suite**

```bash
pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add ripper.py tests/test_ripper_main.py
git commit -m "feat: orchestrator main loop wires pipeline end-to-end"
```

---

## Task 9: launchd Daemon Installer

**Files:**
- Create: `com.dillon.ripper.plist`
- Create: `install.sh`

- [ ] **Step 1: Write `com.dillon.ripper.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.dillon.ripper</string>

    <key>ProgramArguments</key>
    <array>
        <string>__PYTHON__</string>
        <string>__RIPPER_PATH__</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>__WORKING_DIR__</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>__LOG_PATH__</string>

    <key>StandardErrorPath</key>
    <string>__LOG_PATH__</string>
</dict>
</plist>
```

- [ ] **Step 2: Write `install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
LOG_PATH="$HOME/Library/Logs/ai-ripper.log"
PLIST_DEST="$HOME/Library/LaunchAgents/com.dillon.ripper.plist"

echo "Installing ai-ripper launchd agent..."
echo "  Script dir : $SCRIPT_DIR"
echo "  Python     : $PYTHON"
echo "  Log file   : $LOG_PATH"
echo "  Plist dest : $PLIST_DEST"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HOME/Library/Logs"

sed \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__RIPPER_PATH__|$SCRIPT_DIR/ripper.py|g" \
    -e "s|__WORKING_DIR__|$SCRIPT_DIR|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    "$SCRIPT_DIR/com.dillon.ripper.plist" > "$PLIST_DEST"

# Unload if already registered, then load fresh
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo ""
echo "✅ ai-ripper installed as launchd agent (com.dillon.ripper)"
echo "   Starts automatically on login. Running now."
echo ""
echo "Useful commands:"
echo "  View logs  : tail -f $LOG_PATH"
echo "  Stop       : launchctl unload $PLIST_DEST"
echo "  Start      : launchctl load $PLIST_DEST"
echo "  Uninstall  : launchctl unload $PLIST_DEST && rm $PLIST_DEST"
```

- [ ] **Step 3: Make `install.sh` executable**

```bash
chmod +x install.sh
```

- [ ] **Step 4: Verify the plist substitution works**

```bash
sed \
    -e "s|__PYTHON__|$(which python3)|g" \
    -e "s|__RIPPER_PATH__|$(pwd)/ripper.py|g" \
    -e "s|__WORKING_DIR__|$(pwd)|g" \
    -e "s|__LOG_PATH__|$HOME/Library/Logs/ai-ripper.log|g" \
    com.dillon.ripper.plist
```

Expected: a valid plist printed to stdout with no `__PLACEHOLDER__` strings remaining.

- [ ] **Step 5: Run the final full test suite**

```bash
pytest -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add com.dillon.ripper.plist install.sh
git commit -m "feat: launchd plist and install script for daemon registration"
```

---

## First-Run Setup Checklist

After implementation is complete, the user needs:

1. Copy `.env.example` to `.env` and fill in all values
2. Generate a Jellyfin API key: Jellyfin Dashboard → Administration → API Keys → + Add
3. Create a Discord webhook: Server Settings → Integrations → Webhooks → New Webhook
4. Ensure `makemkvcon` is in PATH: `which makemkvcon`
5. Create the temp dir: `mkdir -p /tmp/ai-ripper`
6. Test run: `python ripper.py` — insert a disc and watch the logs
7. Install as daemon (optional): `./install.sh`
