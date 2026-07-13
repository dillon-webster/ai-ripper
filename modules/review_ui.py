"""Web-based disc review UI (v1 — filmstrip curation, no transcoding).

Serves a single self-contained page from inside the ripper process so the user can
review EVERY ripped title (kept and dropped by the content-ID pipeline), see a
filmstrip of stills per title, and hand-curate the episode mapping — reassign, add
back a dropped title, or exclude one — before anything is written to Jellyfin. The
pipeline's proposal is only the starting point; the curated list replaces it.

Opt-in via `--review-ui`, which takes the place of the Discord approval gate for
the run (see docs/review-ui-plan.md). The server binds 0.0.0.0 so it's reachable
over the tailnet (Tailscale is the trust boundary — never expose this port to the
internet; no port-forwarding, no reverse proxy, no Tailscale Funnel).

`request_review` mirrors the contract of `approval.request_approval`: synchronous
from the caller, blocks until the user submits (or timeout), and NEVER raises —
any failure (port in use, timeout, bad guide) returns approved=False so the caller
holds the files for manual handling rather than transferring blind.

Live video playback is v2 and intentionally absent: DVD MPEG-2 needs on-the-fly
transcoding no browser can skip, while single-frame ffmpeg grabs (already proven in
approval._extract_thumbnail) are instant and enough to spot a scramble/misID.
"""
import json
import logging
import re
import secrets
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from modules import identify, notifier

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_SECS = 1800
DEFAULT_THUMBS_PER_TITLE = 12
_THUMB_FALLBACK_SECS = 300  # if a title has no known duration
_THUMB_TIMEOUT_SECS = 60
_TAIL_SKIP_FRAC = 0.97  # don't grab the last few % — credits/black frames
# Cap concurrent ffmpeg grabs so a card full of thumbnails can't spike CPU into an
# active rip; extra requests queue on the semaphore.
_MAX_CONCURRENT_GRABS = 2

_EP_TAG_RE = re.compile(r"S(\d+)E(\d+)", re.IGNORECASE)


@dataclass
class ReviewDecision:
    approved: bool
    reason: str = ""
    titles: Optional[List[Dict]] = None  # curated named-titles to transfer (when approved)


# ---------------------------------------------------------------------------
# Pure logic (no server, no ffmpeg — unit-tested directly)
# ---------------------------------------------------------------------------

def thumb_timestamps(duration_secs: Optional[int], count: int) -> List[int]:
    """Evenly-spread grab points across a title. Skips the head (recap/montage —
    same idea as identify.SUBTITLE_HEAD_SKIP_SECS) and the last few % (credits)."""
    count = max(1, int(count))
    if not duration_secs or duration_secs <= 0:
        return [_THUMB_FALLBACK_SECS]
    dur = int(duration_secs)
    lo = min(identify.SUBTITLE_HEAD_SKIP_SECS, dur // 10)
    hi = max(lo, int(dur * _TAIL_SKIP_FRAC) - 1)
    if count == 1 or hi <= lo:
        return [max(0, min(dur - 1, (lo + hi) // 2))]
    step = (hi - lo) / (count - 1)
    out = []
    for i in range(count):
        t = int(lo + step * i)
        if not out or t != out[-1]:  # dedupe on very short titles
            out.append(t)
    return out


def _episode_of(named_title: Dict) -> Optional[int]:
    """The (start) episode number of a named title — from the `episode` key when the
    content-ID pipeline produced it, else parsed from the Jellyfin filename (the
    legacy namer sets only the filename)."""
    if named_title.get("episode") is not None:
        return named_title["episode"]
    m = _EP_TAG_RE.search(named_title.get("jellyfin_filename", ""))
    return int(m.group(2)) if m else None


def build_page_model(all_titles: List[Dict], named: List[Dict], dropped: List[Dict],
                     guide: List[Dict], show: str, season: int,
                     thumbs_per_title: int = DEFAULT_THUMBS_PER_TITLE,
                     nonce: Optional[str] = None) -> Dict:
    """JSON-able model the page renders from: every ripped title (kept AND dropped)
    with its pipeline guess, plus the season's episode slots.

    `nonce` (fresh per call unless given) goes into every thumb URL so no two review
    sessions ever share one — title indices and grab timestamps repeat across discs
    of the same show, and the thumbs are served cacheable, so without it the browser
    would happily show a previous disc's cached frame for the same URL."""
    named_by_idx = {t.get("title_index"): t for t in (named or [])}
    dropped_by_idx = {t.get("title_index"): t for t in (dropped or [])}
    guide_episodes = {e["index"] for e in guide}

    titles = []
    for t in sorted(all_titles, key=lambda x: x.get("title_index") or 0):
        idx = t.get("title_index")
        nt = named_by_idx.get(idx)
        dt = dropped_by_idx.get(idx)
        guess, label, status = None, "no proposal", "unassigned"
        if nt is not None:
            status = "kept"
            guess = _episode_of(nt)
            if guess not in guide_episodes:
                guess = None  # can't preselect an episode the guide doesn't list
            label = f"→ {nt.get('jellyfin_filename', '?')}"
            if nt.get("episode_name"):
                label += f" — {nt['episode_name']}"
            if nt.get("method") and nt.get("confidence") is not None:
                label += f" · {nt['method']} {nt['confidence']:.2f}"
        elif dt is not None:
            status = "dropped"
            label = f"dropped: {dt.get('drop_reason', 'no reason recorded')}"
        titles.append({
            "title_index": idx,
            "name": Path(t["path"]).name if t.get("path") else f"title #{idx}",
            "duration_secs": t.get("duration_secs") or 0,
            "guess": guess,
            "guess_label": label,
            "status": status,
            "thumbs": thumb_timestamps(t.get("duration_secs"), thumbs_per_title),
        })

    return {
        "show": show,
        "season": season,
        "nonce": nonce or secrets.token_hex(8),
        "episodes": [{"index": e["index"], "index_end": e.get("index_end"),
                      "name": e.get("name")} for e in guide],
        "titles": titles,
    }


def build_curated_named(assignments: Dict, all_titles: List[Dict], guide: List[Dict],
                        show: str, season: int) -> List[Dict]:
    """Turn the submitted `{title_index: episode|null}` map into a transfer-ready
    `named` list (the shape transfer.send_all consumes). null/absent = excluded.
    Raises ValueError on bad input — unknown title/episode, or the same episode
    assigned to two titles (which would clobber one file with the other on transfer);
    the HTTP handler turns that into a 400 so the page can show it."""
    if not isinstance(assignments, dict):
        raise ValueError("assignments must be an object of {title_index: episode|null}")
    by_index = {t["title_index"]: t for t in all_titles}
    by_episode = {e["index"]: e for e in guide}

    named, claimed = [], {}
    for tidx_raw, episode in assignments.items():
        if episode is None:
            continue  # excluded / not an episode
        try:
            tidx, episode = int(tidx_raw), int(episode)
        except (TypeError, ValueError):
            raise ValueError(f"bad assignment: {tidx_raw!r} → {episode!r}")
        src = by_index.get(tidx)
        if src is None:
            raise ValueError(f"unknown title index {tidx}")
        entry = by_episode.get(episode)
        if entry is None:
            raise ValueError(f"episode {episode} is not in the season guide")
        if episode in claimed:
            raise ValueError(f"episode {episode} is assigned to two titles "
                             f"(#{claimed[episode]} and #{tidx})")
        claimed[episode] = tidx
        # Carry the guide entry's index_end so a double-episode slot names as E01-E02.
        nt = identify.build_named_title(
            {**src, "episode": episode, "index_end": entry.get("index_end")}, show, season)
        nt["episode_name"] = entry.get("name")
        nt["method"] = "review-ui"
        nt["confidence"] = 1.0
        named.append(nt)
    named.sort(key=lambda t: t["episode"])
    return named


def render_page(model: Dict) -> str:
    """The whole frontend: one self-contained HTML page (inline CSS/JS, no external
    assets — the rip-box serves everything itself)."""
    payload = json.dumps(model).replace("</", "<\\/")  # keep </script> in names inert
    return _PAGE_TEMPLATE.replace("__MODEL_JSON__", payload)


# ---------------------------------------------------------------------------
# Frame grabs (ffmpeg, cached, concurrency-capped)
# ---------------------------------------------------------------------------

def _grab_frame(mkv, t: int, out_path: Path) -> bool:
    """One downscaled JPEG at `t` seconds — same recipe as approval._extract_thumbnail
    but at an explicit timestamp. Best-effort: returns False, never raises."""
    if not mkv:
        return False
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(int(t)), "-i", str(mkv),
             "-frames:v", "1", "-q:v", "5", "-vf", "scale=480:-1", str(out_path)],
            capture_output=True, text=True, timeout=_THUMB_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"review-ui: frame grab failed for {mkv} @{t}s: {e}")
        return False
    if result.returncode != 0 or not out_path.exists():
        log.warning(f"review-ui: no frame for {mkv} @{t}s")
        return False
    return True


class _ThumbCache:
    """Grabbed frames keyed by (title_index, t) in a dedicated subdir of temp_dir —
    NEVER temp_dir itself, so cached JPEGs can't mingle with held rips. A per-key
    lock stops two requests racing ffmpeg onto the same file; a global semaphore
    caps total concurrent grabs."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self._sem = threading.Semaphore(_MAX_CONCURRENT_GRABS)
        self._locks: Dict[str, threading.Lock] = {}
        self._mu = threading.Lock()

    def get(self, title: Dict, t: int) -> Optional[Path]:
        out = self.cache_dir / f"t{title['title_index']}_{t}.jpg"
        with self._mu:
            lock = self._locks.setdefault(out.name, threading.Lock())
        with lock:
            if out.exists():
                return out
            with self._sem:
                if _grab_frame(title.get("path"), t, out):
                    return out
        return None


def _advertised_host(config) -> str:
    """Host to put in the review URL we log and send to Discord. Prefers the box's
    Tailscale IP — that link opens from a phone/laptop anywhere, as long as the
    device is on the tailnet — and falls back to the LAN hostname (home-network
    only) when tailscale isn't installed/up. `review_ui_advertise_host` overrides
    both (e.g. a MagicDNS name). Never raises."""
    override = getattr(config, "review_ui_advertise_host", "")
    if override:
        return override
    try:
        result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return socket.gethostname()


# ---------------------------------------------------------------------------
# HTTP server (stdlib only — no new pip dependency)
# ---------------------------------------------------------------------------

class _ReviewState:
    """Shared between request_review and the handler threads."""

    def __init__(self, all_titles, guide, show, season, page, thumbs, nonce):
        self.all_titles = all_titles
        self.by_index = {t["title_index"]: t for t in all_titles}
        self.guide = guide
        self.show = show
        self.season = season
        self.page = page
        self.thumbs = thumbs
        self.nonce = nonce
        self.outcome: Optional[List[Dict]] = None
        self.done = threading.Event()
        self.mu = threading.Lock()


def _make_handler(state: _ReviewState):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "ai-ripper-review"

        def log_message(self, fmt, *args):  # keep http chatter out of the rip log
            log.debug("review-ui: " + fmt % args)

        def _send(self, code: int, ctype: str, body: bytes, cacheable: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if cacheable:
                # Safe to cache hard: thumb URLs carry the per-session nonce, so a
                # new disc's review never reuses an old URL.
                self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away — nothing to clean up for a static body

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(200, "text/html; charset=utf-8", state.page.encode())
            elif parsed.path.startswith("/thumb/"):
                self._thumb(parsed)
            else:
                self.send_error(404)

        def _thumb(self, parsed):
            # /thumb/<nonce>/<idx> — a wrong nonce means a tab left over from an
            # earlier disc's review session; 404 so its <img>s show as errored
            # instead of silently getting this disc's frames.
            parts = parsed.path.split("/")
            if len(parts) != 4 or parts[2] != state.nonce:
                return self.send_error(404, "stale or unknown review session")
            # Input hygiene: only spawn ffmpeg for a title we actually ripped.
            try:
                idx = int(parts[3])
            except ValueError:
                return self.send_error(400, "bad title index")
            title = state.by_index.get(idx)
            if title is None:
                return self.send_error(404, "unknown title index")
            try:
                t = int(float(parse_qs(parsed.query).get("t", ["0"])[0]))
            except ValueError:
                return self.send_error(400, "bad timestamp")
            dur = title.get("duration_secs") or 0
            t = max(0, min(t, dur - 1) if dur else t)
            jpg = state.thumbs.get(title, t)
            if jpg is None:
                return self.send_error(502, "frame grab failed")
            self._send(200, "image/jpeg", jpg.read_bytes(), cacheable=True)

        def do_POST(self):
            if urlparse(self.path).path != "/submit":
                return self.send_error(404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                named = build_curated_named(payload["assignments"], state.all_titles,
                                            state.guide, state.show, state.season)
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, "application/json",
                                  json.dumps({"error": str(e)}).encode())
            with state.mu:
                if state.outcome is not None or state.done.is_set():
                    return self._send(409, "application/json",
                                      b'{"error": "already submitted"}')
                state.outcome = named
            self._send(200, "application/json", b'{"ok": true}')
            state.done.set()  # after the response is written, so the browser gets its 200

    return _Handler


def request_review(all_titles: List[Dict], named: List[Dict], dropped: List[Dict],
                   guide: List[Dict], show: str, season: int, config,
                   timeout: int = None, _on_start=None) -> ReviewDecision:
    """Launch the local review server, log the URL, and block until the user submits
    a curated mapping (approved, with `titles`) or timeout/failure (approved=False —
    the caller holds the files). Never raises.

    `_on_start(host, port)` is a test hook fired once the server is listening.
    """
    timeout = timeout or getattr(config, "review_ui_timeout_secs", DEFAULT_TIMEOUT_SECS)
    if not guide or not show or season is None:
        log.warning("Review UI needs the episode guide (--show + --season) — holding.")
        return ReviewDecision(False, "review UI needs an episode guide (--show + --season)")

    server = None
    thumb_dir = None
    try:
        port = getattr(config, "review_ui_port", DEFAULT_PORT)
        thumbs_per_title = getattr(config, "review_ui_thumbs_per_title",
                                   DEFAULT_THUMBS_PER_TITLE)
        thumb_dir = Path(config.temp_dir) / "review-thumbs"
        # A killed run (Ctrl-C mid-review, power loss) skips the finally-cleanup, and
        # the cache is keyed only (title_index, t) — wipe first or a leftover frame
        # from the previous disc would be served as this one's.
        shutil.rmtree(thumb_dir, ignore_errors=True)
        thumb_dir.mkdir(parents=True, exist_ok=True)

        model = build_page_model(all_titles, named, dropped, guide, show, season,
                                 thumbs_per_title)
        state = _ReviewState(all_titles, guide, show, season,
                             page=render_page(model), thumbs=_ThumbCache(thumb_dir),
                             nonce=model["nonce"])

        # 0.0.0.0 = the rip-box's own interfaces (tailnet-reachable), NOT public.
        server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
        bound_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="review-ui").start()
        host = _advertised_host(config)
        url = f"http://{host}:{bound_port}/"
        log.info(f"Review UI up: {url} — waiting up to {timeout}s for the "
                 "curated mapping…")
        # Ping Discord with the link — the user usually isn't at a desk when the
        # rip finishes. (Phone push requires desktop Discord to be fully quit;
        # see notifier.send_review_ready.)
        notifier.send_review_ready(url, show, season, config)
        if _on_start:
            _on_start(host, bound_port)

        if not state.done.wait(timeout):
            log.warning("Review UI timed out — holding files.")
            return ReviewDecision(False, f"review timed out after {timeout}s")

        curated = state.outcome or []
        if not curated:
            log.warning("Review submitted with every title excluded — nothing will transfer.")
        return ReviewDecision(True, "reviewed via web UI", titles=curated)
    except Exception as e:  # noqa: BLE001 — review must never crash the rip
        log.warning(f"Review UI failed ({e}); holding files for manual handling.")
        return ReviewDecision(False, f"review UI error: {e}")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thumb_dir is not None:
            shutil.rmtree(thumb_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The page (inline CSS/JS; rendered from the JSON model embedded at serve time)
# ---------------------------------------------------------------------------

_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ai-ripper — disc review</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; font:14px/1.45 system-ui, sans-serif; background:#14161a; color:#e8e8e8; }
header { padding:12px 20px; background:#1d2026; position:sticky; top:0; z-index:5;
         box-shadow:0 1px 4px rgba(0,0,0,.5); }
header h1 { margin:0; font-size:17px; }
header .sub { color:#9aa1ab; font-size:12.5px; margin-top:2px; }
.layout { display:flex; gap:18px; padding:18px 20px 90px; align-items:flex-start; }
#season { width:300px; flex:none; position:sticky; top:70px; background:#1d2026;
          border-radius:10px; padding:12px 14px; max-height:calc(100vh - 170px); overflow-y:auto; }
#season h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
             color:#9aa1ab; margin:0 0 8px; }
.slot { display:flex; gap:8px; padding:4px 6px; border-radius:6px; font-size:13px; }
.slot .ep { font-weight:600; width:62px; flex:none; }
.slot .name { color:#c9ced6; flex:1; min-width:0; overflow:hidden;
              text-overflow:ellipsis; white-space:nowrap; }
.slot.empty { color:#e0a63c; }
.slot.filled { color:#8fd18f; }
.slot.dupe { color:#ff7a76; }
#titles { flex:1; min-width:0; display:flex; flex-direction:column; gap:16px; }
.card { background:#1d2026; border-radius:10px; padding:14px 16px; border:1px solid transparent; }
.card.dupe { border-color:#ff7a76; }
.card.excluded { opacity:.55; }
.card .top { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:baseline; }
.card .fname { font-weight:600; font-size:15px; }
.card .dur { color:#9aa1ab; }
.card .guess { color:#9aa1ab; font-size:12.5px; margin:2px 0 10px; }
.card .guess.dropped { color:#e0a63c; }
.strip { display:flex; gap:6px; overflow-x:auto; padding-bottom:6px; }
.strip img { height:96px; min-width:40px; border-radius:4px; background:#0c0d10;
             cursor:zoom-in; flex:none; }
.strip img.err { min-width:120px; opacity:.25; cursor:default; }
.assign { margin-top:8px; display:flex; align-items:center; gap:10px; }
select { background:#282c34; color:#e8e8e8; border:1px solid #3a3f4a; border-radius:6px;
         padding:6px 8px; font-size:14px; max-width:100%; }
footer { position:fixed; bottom:0; left:0; right:0; background:#1d2026; padding:12px 20px;
         display:flex; align-items:center; gap:16px; box-shadow:0 -1px 6px rgba(0,0,0,.5); z-index:5; }
#msg { flex:1; color:#e0a63c; }
#msg.ok { color:#8fd18f; }
#submit { background:#2f7d4f; color:#fff; border:0; border-radius:8px; padding:10px 26px;
          font-size:15px; font-weight:600; cursor:pointer; }
#submit:disabled { background:#3a3f4a; color:#8a8f98; cursor:not-allowed; }
#lightbox { position:fixed; inset:0; background:rgba(0,0,0,.9); display:none;
            flex-direction:column; align-items:center; justify-content:center; gap:12px; z-index:20; }
#lightbox.open { display:flex; }
#lightbox img { max-width:92vw; max-height:78vh; border-radius:6px; background:#0c0d10; }
#lightbox .bar { display:flex; gap:8px; align-items:center; color:#c9ced6; flex-wrap:wrap;
                 justify-content:center; }
#lightbox button { background:#282c34; color:#e8e8e8; border:1px solid #3a3f4a;
                   border-radius:6px; padding:6px 12px; cursor:pointer; font-size:13px; }
#done { padding:60px 20px; text-align:center; display:none; }
#done h2 { color:#8fd18f; }
@media (max-width: 900px) {
  .layout { flex-direction:column; }
  #season { position:static; width:100%; max-height:240px; }
}
</style>
</head>
<body>
<header><h1 id="hdr"></h1><div class="sub" id="hdr-sub"></div></header>
<div class="layout" id="layout">
  <aside id="season"><h2>Season slots</h2><div id="slots"></div></aside>
  <main id="titles"></main>
</div>
<div id="done"><h2>✓ Review submitted</h2>
  <p>The ripper is transferring your curated mapping. You can close this tab.</p></div>
<footer id="footer"><div id="msg"></div><button id="submit">Transfer these episodes</button></footer>
<div id="lightbox">
  <img id="lb-img" alt="">
  <div class="bar">
    <button data-step="-60">−60s</button>
    <button data-step="-10">−10s</button>
    <span id="lb-time"></span>
    <button data-step="10">+10s</button>
    <button data-step="60">+60s</button>
    <button id="lb-close">Close ✕</button>
  </div>
</div>
<script>
'use strict';
const MODEL = __MODEL_JSON__;
// Session nonce in every thumb URL: title indices/timestamps repeat across discs,
// and thumbs are served cacheable — this keeps the browser from reusing a cached
// frame from a previous disc's review.
const THUMB = '/thumb/' + MODEL.nonce + '/';
const $ = s => document.querySelector(s);
const slotsEl = $('#slots'), titlesEl = $('#titles'), msgEl = $('#msg'), submitBtn = $('#submit');

$('#hdr').textContent = (MODEL.show || 'Disc') + ' — Season ' + MODEL.season + ' review';
$('#hdr-sub').textContent = MODEL.titles.length + ' title(s) ripped · '
  + MODEL.episodes.length + ' episode(s) in the season guide · click a still to enlarge';

const assignments = {};  // title_index -> episode int or null (= exclude)
MODEL.titles.forEach(t => { assignments[t.title_index] = t.guess; });

const pad = n => String(n).padStart(2, '0');
const epTag = e => 'E' + pad(e.index) + (e.index_end ? '-E' + pad(e.index_end) : '');
const fmtDur = s => s ? Math.floor(s / 60) + ':' + pad(s % 60) : '?:??';

// --- title cards -----------------------------------------------------------
const cards = {};
MODEL.titles.forEach(t => {
  const card = document.createElement('div');
  card.className = 'card';

  const top = document.createElement('div');
  top.className = 'top';
  const fname = document.createElement('span');
  fname.className = 'fname';
  fname.textContent = t.name;
  const dur = document.createElement('span');
  dur.className = 'dur';
  dur.textContent = fmtDur(t.duration_secs) + ' · title #' + t.title_index;
  top.append(fname, dur);

  const guess = document.createElement('div');
  guess.className = 'guess' + (t.status === 'dropped' ? ' dropped' : '');
  guess.textContent = t.guess_label;

  const strip = document.createElement('div');
  strip.className = 'strip';
  t.thumbs.forEach(ts => {
    const img = document.createElement('img');
    img.dataset.src = THUMB + t.title_index + '?t=' + ts;
    img.alt = 't=' + ts + 's';
    img.title = fmtDur(ts);
    img.addEventListener('click', () => openLightbox(t, ts));
    img.addEventListener('error', () => img.classList.add('err'));
    strip.appendChild(img);
  });

  const assign = document.createElement('div');
  assign.className = 'assign';
  const lab = document.createElement('span');
  lab.textContent = 'Transfer as:';
  const sel = document.createElement('select');
  sel.appendChild(new Option('— exclude (do not transfer) —', ''));
  MODEL.episodes.forEach(e =>
    sel.appendChild(new Option(epTag(e) + (e.name ? ' — ' + e.name : ''), e.index)));
  sel.value = t.guess == null ? '' : String(t.guess);
  sel.addEventListener('change', () => {
    assignments[t.title_index] = sel.value === '' ? null : parseInt(sel.value, 10);
    refresh();
  });
  assign.append(lab, sel);

  card.append(top, guess, strip, assign);
  titlesEl.appendChild(card);
  cards[t.title_index] = card;
});

// Lazy-load filmstrips only when a card scrolls into view, so page load doesn't
// fire every title's frame grabs at once (the server also caps ffmpeg concurrency).
const io = new IntersectionObserver(entries => {
  entries.forEach(en => {
    if (!en.isIntersecting) return;
    en.target.querySelectorAll('img[data-src]').forEach(img => {
      img.src = img.dataset.src;
      img.removeAttribute('data-src');
    });
    io.unobserve(en.target);
  });
}, { rootMargin: '250px' });
Object.values(cards).forEach(c => io.observe(c));

// --- season panel + duplicate guard -----------------------------------------
function refresh() {
  const byEp = {};
  for (const [ti, ep] of Object.entries(assignments)) {
    if (ep != null) (byEp[ep] = byEp[ep] || []).push(ti);
  }
  slotsEl.innerHTML = '';
  MODEL.episodes.forEach(e => {
    const owners = byEp[e.index] || [];
    const div = document.createElement('div');
    div.className = 'slot ' + (owners.length === 0 ? 'empty' : owners.length > 1 ? 'dupe' : 'filled');
    const ep = document.createElement('span'); ep.className = 'ep'; ep.textContent = epTag(e);
    const name = document.createElement('span'); name.className = 'name'; name.textContent = e.name || '';
    const who = document.createElement('span');
    who.textContent = owners.length === 0 ? 'EMPTY' : owners.map(x => '#' + x).join(', ');
    div.append(ep, name, who);
    slotsEl.appendChild(div);
  });
  const dupes = Object.entries(byEp).filter(([, v]) => v.length > 1);
  MODEL.titles.forEach(t => {
    const c = cards[t.title_index];
    c.classList.toggle('excluded', assignments[t.title_index] == null);
    c.classList.toggle('dupe', dupes.some(([, v]) => v.includes(String(t.title_index))));
  });
  msgEl.classList.remove('ok');
  if (dupes.length) {
    submitBtn.disabled = true;
    msgEl.textContent = 'Episode ' + dupes.map(([k]) => k).join(', ')
      + ' is assigned to more than one title — fix before submitting.';
  } else {
    submitBtn.disabled = false;
    const kept = Object.values(assignments).filter(v => v != null).length;
    if (kept === 0) {
      msgEl.textContent = 'Nothing selected — submitting will transfer no episodes.';
    } else {
      msgEl.classList.add('ok');
      msgEl.textContent = kept + ' episode(s) will transfer.';
    }
  }
}
refresh();

// --- lightbox (on-demand finer frames: step to adjacent timestamps) ----------
let lb = null;  // { title, t }
const lbEl = $('#lightbox'), lbImg = $('#lb-img'), lbTime = $('#lb-time');
function openLightbox(t, ts) { lb = { title: t, t: ts }; lbEl.classList.add('open'); lbShow(); }
function lbShow() {
  lbImg.src = THUMB + lb.title.title_index + '?t=' + lb.t;
  lbTime.textContent = lb.title.name + ' @ ' + fmtDur(lb.t);
}
function lbStep(delta) {
  if (!lb) return;
  const max = Math.max(0, (lb.title.duration_secs || 0) - 1);
  const next = Math.max(0, lb.t + delta);
  lb.t = max ? Math.min(max, next) : next;
  lbShow();
}
document.querySelectorAll('#lightbox button[data-step]').forEach(b =>
  b.addEventListener('click', () => lbStep(parseInt(b.dataset.step, 10))));
$('#lb-close').addEventListener('click', () => lbEl.classList.remove('open'));
lbEl.addEventListener('click', ev => { if (ev.target === lbEl) lbEl.classList.remove('open'); });
document.addEventListener('keydown', ev => {
  if (!lbEl.classList.contains('open')) return;
  if (ev.key === 'Escape') lbEl.classList.remove('open');
  else if (ev.key === 'ArrowLeft') lbStep(-10);
  else if (ev.key === 'ArrowRight') lbStep(10);
});

// --- submit ------------------------------------------------------------------
submitBtn.addEventListener('click', async () => {
  submitBtn.disabled = true;
  submitBtn.textContent = 'Submitting…';
  try {
    const res = await fetch('/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ assignments }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
    $('#layout').style.display = 'none';
    $('#footer').style.display = 'none';
    $('#done').style.display = 'block';
  } catch (e) {
    msgEl.classList.remove('ok');
    msgEl.textContent = 'Submit failed: ' + e.message;
    submitBtn.disabled = false;
    submitBtn.textContent = 'Transfer these episodes';
  }
});
</script>
</body>
</html>
"""
