"""Content-based episode identification — Phase 2 of the episode-ID rework.

Phase 1 (modules/episode_guide.py) gave the namer the real season episode list so
it stops inventing numbers. It does NOT fix scrambled disc order: when every
episode runs ~22 min there's no duration signal, so numbering still follows
makemkv playback order — which is unreliable per disc (reversed on Family Guy,
fully scrambled on The Office S1 disc 1). See docs/episode-identification-plan.md.

This module fixes that by identifying each ripped title from its CONTENT and
matching it against the Phase-1 candidate list, instead of trusting disc order:

  1. Primary signal: the subtitle track's first ~2 min of dialogue (VobSub → OCR),
     LLM-matched against the candidates (constrained multiple-choice, high accuracy).
  2. Fallback signal: ~3 frames → Claude vision, for discs with no usable subs.
  3. Reconcile: map identities onto real episode numbers, dropping Play-All
     duplicates and bonus features (see `reconcile`).

External tools (added to install-linux.sh): ffmpeg/ffprobe, mkvtoolnix
(mkvextract), and vobsub2srt (OCRs the image-based VobSub subs to text via
Tesseract). Upstream vobsub2srt is abandoned and won't build on Tesseract 5, so
install-linux.sh builds a patched copy (Tesseract-5 API + C++17). Missing tools
degrade to the frame fallback, and a title that can't be identified at all is
surfaced (episode=None), never silently dropped — the human approval step
(Phase 3) is the safety net.
"""
import base64
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"

# VobSub OCR backend: vobsub2srt. It reads the .idx/.sub pair mkvextract produces
# and emits <base>.srt via Tesseract. Upstream is abandoned and won't build on
# Tesseract 5 as-is; install-linux.sh builds a patched copy (Tesseract-5 API + C++17).
# Chosen over vobsubocr, whose Rust leptonica bindings won't build on leptonica 1.86.
OCR_CMD = "vobsub2srt"

# Subtitle tracks come in two flavors and need different extraction:
#   TEXT (subrip/ass/mov_text/…): dialogue is already text — ffmpeg transcodes the
#     track straight to SRT, no OCR. Most animated releases (e.g. The Legend of Korra)
#     ship these; extracting them as if they were VobSub silently failed and forced the
#     far weaker frame fallback on every title.
#   IMAGE (DVD VobSub, Blu-ray PGS): bitmaps mkvextract dumps as an .idx/.sub pair that
#     vobsub2srt then OCRs. Most live-action DVDs ship these.
# codec_name comes from ffprobe. Unknown codecs default to the OCR path (the old
# behavior), so nothing that worked before regresses.
_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}

# Subtitle dialogue is sampled from several points spread ACROSS the episode, not
# just the opening. Episodes in a serialized arc (S10 Friends' adoption→birth-mother→
# finale run) open with near-identical dialogue, so the first ~2 min can't tell them
# apart — verified: it mismatched two such episodes at 0.9+ confidence. Mid- and
# late-episode dialogue is far more distinctive, so we sample windows at these
# fractions of the runtime (mirrors the deep frame timestamps below), skipping the
# opening recap/title montage.
SUBTITLE_SAMPLE_FRACTIONS = (0.08, 0.30, 0.55, 0.80)
SUBTITLE_WINDOW_SECS = 45          # width of each sampled window
SUBTITLE_HEAD_SKIP_SECS = 40       # never sample before this — skips recap/montage
SUBTITLE_MAX_CHARS = 3500          # keep the OCR'd dialogue prompt bounded
FRAME_TIMESTAMPS = (8, 300, 500)  # teaser, then two deep-plot beats past the montage

# A ripped title running this many times longer than its matched episode's real
# runtime is a 'Play All'/omnibus (it merely OPENS with that episode), not the
# episode itself → dropped in reconcile. 1.5× clears normal length variance while
# flagging 2-episode (~1.9×) and half-disc (~4×) compilations. Comparing against the
# MATCHED episode's runtime (not a fixed single-episode length) keeps genuinely
# double-length episodes, whose Jellyfin runtime is itself ~2×.
OMNIBUS_RUNTIME_FACTOR = 1.5

# The mirror of OMNIBUS_RUNTIME_FACTOR for the too-SHORT direction: a title matched to an
# episode but running below this fraction of that episode's real runtime is almost
# certainly a bonus (gag reel, deleted-scenes reel) whose dialogue merely resembles the
# episode — not the episode itself. It catches a lone bonus that content-matched a number
# with no real episode competing (the duplicate tie-break only helps when the real episode
# IS present). Deliberately conservative — real DVD rips run ~0.95× their listed runtime,
# so 0.6 leaves a wide safety band and can't drop a real episode. Only applied against a
# true guide runtime (never the median fallback), so it can't misfire off ripped bonuses.
SHORT_RUNTIME_FACTOR = 0.6

# A DVD disc holds a CONTIGUOUS block of episodes, so a kept title whose episode sits
# alone — nothing matched at ±1 — is suspect. When that lone title ALSO matched less
# confidently than a real episode should, it's almost always a bonus reel (deleted
# scenes, a featurette) whose OCR'd dialogue merely resembles a real episode: content
# matching can't tell footage cut FROM an episode apart from the episode itself, and the
# whole-season candidate list lets it land on a number that isn't even on this disc.
# Below this confidence, such an isolated match is dropped (surfaced for approval, never
# deleted) rather than numbered as a phantom episode. See reconcile.
ISOLATED_MATCH_MIN_CONFIDENCE = 0.90

# Before dropping an isolated match, try to REASSIGN it: an on-disc title the model
# matched to an off-disc episode of the SAME length (S3 has two ~42-min episodes, E10
# 'Benihana' and E23 'The Job') should be moved to the adjacent on-disc episode whose
# runtime fits, not discarded. A title within ±this fraction of an episode's runtime
# "fits" it. The finale (42.5 min) fits E23 (43 min), not the also-adjacent E18 (22 min).
RUNTIME_REPAIR_TOL = 0.15

_SUBPROCESS_TIMEOUT = 300


class IdentifyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Season-qualified episode keys
# ---------------------------------------------------------------------------

# A disc isn't always one season. A volume box set (Family Guy shipped as Volumes
# 1-12) can hold the tail of one season and the head of the next, so the candidate
# pool spans two seasons — and there the bare episode number is no longer an
# identity: S04E10 and S05E10 are different episodes with the same number. Everything
# downstream of matching (dedup, adjacency, slot assignment) therefore keys on this
# composite instead. The encoding is order-preserving — sorting keys sorts
# chronologically — because no season has 1000 episodes.
#
# `season is None` (a guide with no season attached: the legacy single-season path
# and most tests) keys on the plain episode number, so that path is untouched.
_SEASON_KEY_STRIDE = 1000


def episode_key(season: Optional[int], index: int) -> int:
    """The identity of one episode slot: season-qualified when a season is known."""
    if season is None:
        return index
    return season * _SEASON_KEY_STRIDE + index


def split_key(key: int) -> tuple:
    """Inverse of `episode_key`: (season, index), with season None if unqualified."""
    if key >= _SEASON_KEY_STRIDE:
        return divmod(key, _SEASON_KEY_STRIDE)
    return None, key


def _title_key(title: Dict) -> int:
    return episode_key(title.get("season"), title["episode"])


def _ep_label(season: Optional[int], index: int) -> str:
    """How an episode reads in a log line or a drop reason: E05, or S04E05 when the
    run spans seasons and the number alone would be ambiguous."""
    return (f"S{season:02d}" if season is not None else "") + f"E{index:02d}"


def _seasons_of(candidates: List[Dict]) -> List[int]:
    return sorted({c["season"] for c in candidates if c.get("season") is not None})


# ---------------------------------------------------------------------------
# Candidate rendering + LLM matching (pure / mockable)
# ---------------------------------------------------------------------------

# Cap each episode overview so a full season stays a reasonable prompt. Summaries
# are ~200–650 chars; the opening ~350 carry the distinctive plot points.
_OVERVIEW_MAX_CHARS = 350


def _candidate_lines(candidates: List[Dict]) -> str:
    """Render the Phase-1 episode list as the constrained choice set for the model.

    Each line carries the plot overview when available — matching OCR'd dialogue
    against real summaries (not just cryptic "The One with..." titles) is what lets
    the model tell serialized-arc / same-length episodes apart.

    When the pool spans two seasons (a volume disc), every line is season-qualified —
    E10 alone would name two different episodes.
    """
    multi = len(_seasons_of(candidates)) > 1
    lines = []
    for c in candidates:
        season = f"S{c['season']:02d}" if multi else ""
        rng = season + f"E{c['index']:02d}" + (f"-E{c['index_end']:02d}" if c.get("index_end") else "")
        nm = f' "{c["name"]}"' if c.get("name") else ""
        dur = f" (~{round(c['runtime_secs'] / 60)} min)" if c.get("runtime_secs") else ""
        lines.append(f"  {rng}{nm}{dur}")
        overview = (c.get("overview") or "").strip()
        if overview:
            lines.append(f"      {overview[:_OVERVIEW_MAX_CHARS]}")
    return "\n".join(lines)


_MATCH_RULES = (
    "Return ONLY a JSON object, no other text:\n"
    '{"episode": <IndexNumber int, or null>, "index_end": <int or null>, '
    '"confidence": <0.0-1.0>, "reasoning": "<one short sentence>"}\n'
    "Rules:\n"
    "- \"episode\" MUST be an IndexNumber from the list above, or null. Never invent a number.\n"
    "- Set \"episode\": null if the content matches NO listed episode (a bonus feature, "
    "menu, or 'Play All' compilation of several episodes) — do not force a guess.\n"
    "- Set \"index_end\" only if you matched a listed episode that shows a spanning range "
    "(e.g. E12-E13); otherwise null.\n"
    "- Judge primarily by plot/dialogue. The ripped title's runtime is given above: when "
    "exactly one candidate's runtime is close to it and the others are not, that is strong "
    "corroborating evidence — prefer it unless the dialogue clearly points elsewhere. A "
    "title far longer than EVERY candidate is a 'Play All' compilation → null.\n"
)

# Added only when the candidate pool spans seasons (a volume disc). The single-season
# prompt — the proven one — is left exactly as it was.
_MULTI_SEASON_RULES = (
    "Return ONLY a JSON object, no other text:\n"
    '{"season": <season int, or null>, "episode": <IndexNumber int, or null>, '
    '"index_end": <int or null>, "confidence": <0.0-1.0>, "reasoning": "<one short sentence>"}\n'
    "Rules:\n"
    "- This disc comes from a VOLUME box set, not a season set: it holds the last "
    "episodes of one season and the first of the next. Either season is a valid answer, "
    "so judge purely by the content — do not assume the disc is all one season.\n"
    "- \"season\" and \"episode\" MUST together name a listed candidate (the season is "
    "part of each line, e.g. S04E22). Never invent a number, and never pair an episode "
    "number with the other season.\n"
    "- Set both to null if the content matches NO listed episode (a bonus feature, menu, "
    "or 'Play All' compilation of several episodes) — do not force a guess.\n"
    "- Set \"index_end\" only if you matched a listed episode that shows a spanning range "
    "(e.g. E12-E13); otherwise null.\n"
    "- Judge primarily by plot/dialogue. The ripped title's runtime is given above: when "
    "exactly one candidate's runtime is close to it and the others are not, that is strong "
    "corroborating evidence — prefer it unless the dialogue clearly points elsewhere. A "
    "title far longer than EVERY candidate is a 'Play All' compilation → null.\n"
)


def _match_rules(candidates: List[Dict]) -> str:
    return _MULTI_SEASON_RULES if len(_seasons_of(candidates)) > 1 else _MATCH_RULES


def _candidate_header(candidates: List[Dict]) -> str:
    seasons = _seasons_of(candidates)
    if len(seasons) > 1:
        listed = " and ".join(str(s) for s in seasons)
        return (f"Candidate episodes for seasons {listed} — this disc spans the boundary "
                "between them (the ONLY valid answers):")
    return "Candidate episodes for this season (the ONLY valid answers):"


def _runtime_line(duration_secs) -> str:
    if not duration_secs:
        return ""
    return f"This ripped title's runtime is ~{round(duration_secs / 60)} min.\n\n"


def _build_subtitle_prompt(dialogue: str, candidates: List[Dict], duration_secs=None) -> str:
    return (
        "You are identifying which TV episode a ripped disc title contains, to number it "
        "for a Jellyfin library. Match the dialogue below to exactly one episode.\n\n"
        f"{_candidate_header(candidates)}\n"
        f"{_candidate_lines(candidates)}\n\n"
        f"{_runtime_line(duration_secs)}"
        "Subtitle dialogue sampled across the title (OCR'd from the disc — may contain "
        f"OCR errors):\n\"\"\"\n{dialogue.strip()}\n\"\"\"\n\n"
        f"{_match_rules(candidates)}"
    )


def _build_frame_prompt(candidates: List[Dict], duration_secs=None) -> str:
    return (
        "You are identifying which TV episode a ripped disc title contains, to number it "
        "for a Jellyfin library. The images are frames sampled from the title (a teaser "
        "shot and two later scenes; the shared opening-title montage is skipped). Match "
        "them to exactly one episode.\n\n"
        f"{_candidate_header(candidates)}\n"
        f"{_candidate_lines(candidates)}\n\n"
        f"{_runtime_line(duration_secs)}"
        f"{_match_rules(candidates)}"
    )


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model reply that may wrap it in prose or fences.

    The prompt asks for JSON only, but the model sometimes reasons first
    ('Looking at the dialogue... {"episode": 23}'). Requiring the WHOLE reply to
    parse threw those away — a correct 0.9-confidence subtitle match on The Office
    S3 finale was discarded this way, forcing a wrong frame-fallback. Prefer a
    ```-fenced block; otherwise take the span from the first '{' to the last '}'.
    """
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if m:
        return m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def _parse_match(raw: str, candidates: List[Dict], method: str) -> Dict:
    """Parse the model's JSON and clamp it to the candidate set.

    An out-of-list episode number is treated as 'no confident match' (episode=None)
    rather than trusted — the whole point is to never number a title as an episode
    that doesn't exist in the season. When the pool spans seasons the SEASON is
    clamped the same way: the pair must name a listed candidate, so a model that
    answers S05E10 for a season-4-only number lands as unmatched, not misfiled.
    """
    try:
        data = json.loads(_extract_json(raw))
    except (json.JSONDecodeError, TypeError) as e:
        raise IdentifyError(f"Malformed match JSON: {e}") from e

    episode = data.get("episode")
    seasons = {c.get("season") for c in candidates}
    # One season in the pool → the model was never asked for one (the single-season
    # prompt has no season field), so the sole season is implied.
    season = data.get("season") if len(seasons) > 1 else next(iter(seasons), None)
    if season is not None:
        try:
            season = int(season)
        except (TypeError, ValueError):
            season = None

    valid = {(c.get("season"), c["index"]) for c in candidates}
    if episode is not None and (season, episode) not in valid:
        log.warning(f"Model returned {_ep_label(season, episode)} — not in the candidate "
                    "list; treating as unmatched")
        episode = None

    index_end = data.get("index_end") if episode is not None else None
    return {
        "season": season if episode is not None else None,
        "episode": episode,
        "index_end": index_end,
        "confidence": float(data.get("confidence") or 0.0),
        "reasoning": data.get("reasoning", ""),
        "method": method,
    }


def _match_subtitles(dialogue: str, candidates: List[Dict], api_key: str,
                     duration_secs=None) -> Dict:
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user",
                   "content": _build_subtitle_prompt(dialogue, candidates, duration_secs)}],
    )
    return _parse_match(resp.content[0].text.strip(), candidates, method="subtitles")


def _match_frames(frame_paths: List[Path], candidates: List[Dict], api_key: str,
                  duration_secs=None) -> Dict:
    client = anthropic.Anthropic(api_key=api_key)
    content = []
    for fp in frame_paths:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(fp.read_bytes()).decode(),
            },
        })
    content.append({"type": "text", "text": _build_frame_prompt(candidates, duration_secs)})
    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": content}],
    )
    return _parse_match(resp.content[0].text.strip(), candidates, method="frames")


# ---------------------------------------------------------------------------
# Media extraction (subprocess — thin wrappers over ffmpeg/mkvtoolnix/OCR)
# ---------------------------------------------------------------------------

def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)


def _find_subtitle_track(mkv_path: Path) -> Optional[tuple]:
    """Return (stream_index, codec_name) of the first subtitle track, or None. Uses
    ffprobe. codec_name selects the extraction path (text transcode vs. VobSub OCR)."""
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index,codec_name", "-of", "json", str(mkv_path),
    ])
    if result.returncode != 0:
        log.warning(f"ffprobe failed on {mkv_path.name}: {result.stderr.strip()}")
        return None
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return None
    if not streams:
        return None
    return streams[0]["index"], streams[0].get("codec_name", "")


def _parse_srt_cues(srt_text: str) -> List[tuple]:
    """Parse SRT text into ordered [(start_secs, text)] cues, dropping indices/blanks."""
    cues: List[tuple] = []
    start = None
    words: List[str] = []
    for line in srt_text.splitlines():
        ts = re.match(r"(\d\d):(\d\d):(\d\d)[,.]\d+ -->", line)
        if ts:
            if start is not None and words:
                cues.append((start, " ".join(words)))
            start = int(ts.group(1)) * 3600 + int(ts.group(2)) * 60 + int(ts.group(3))
            words = []
            continue
        s = line.strip()
        if not s or s.isdigit():
            continue
        if start is not None:
            words.append(s)
    if start is not None and words:
        cues.append((start, " ".join(words)))
    return cues


def _srt_dialogue(srt_text: str) -> str:
    """Sample dialogue spread across the episode, joined with spaces.

    Rather than the opening window (which is near-identical across serialized-arc
    episodes), take a few short windows at SUBTITLE_SAMPLE_FRACTIONS of the runtime,
    skipping the opening recap/montage. Short tracks with no room to spread are used
    whole. Output is capped at SUBTITLE_MAX_CHARS.
    """
    cues = _parse_srt_cues(srt_text)
    if not cues:
        return ""
    span = max(start for start, _ in cues)
    # Too short to spread meaningfully → use all the dialogue.
    if span <= SUBTITLE_HEAD_SKIP_SECS + SUBTITLE_WINDOW_SECS:
        return " ".join(text for _, text in cues)[:SUBTITLE_MAX_CHARS]

    windows = []
    for frac in SUBTITLE_SAMPLE_FRACTIONS:
        anchor = max(SUBTITLE_HEAD_SKIP_SECS, int(frac * span))
        windows.append((anchor, anchor + SUBTITLE_WINDOW_SECS))
    out = [text for start, text in cues
           if any(w0 <= start < w1 for w0, w1 in windows)]
    return " ".join(out)[:SUBTITLE_MAX_CHARS]


def _extract_text_srt(mkv_path: Path, track: int) -> Optional[str]:
    """Transcode a TEXT subtitle track (subrip/ass/mov_text/…) to SRT with ffmpeg and
    return the SRT text. No OCR — the dialogue is already text. ffmpeg's srt muxer
    normalizes every text format to the SRT `_parse_srt_cues` expects."""
    with tempfile.TemporaryDirectory() as td:
        srt = Path(td) / "subs.srt"
        result = _run(["ffmpeg", "-y", "-i", str(mkv_path),
                       "-map", f"0:{track}", str(srt)])
        if result.returncode != 0 or not srt.exists():
            log.warning(f"ffmpeg subtitle extract failed on {mkv_path.name}: "
                        f"{result.stderr.strip()[:200]}")
            return None
        return srt.read_text(errors="replace")


def _ocr_vobsub(mkv_path: Path, track: int) -> Optional[str]:
    """Extract an IMAGE subtitle track (VobSub) and OCR it to SRT text. mkvextract
    pulls the .idx/.sub pair; vobsub2srt runs Tesseract to produce <base>.srt.
    Returns None if mkvextract/OCR fails or the toolchain is missing."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "subs"
        idx = f"{base}.idx"  # mkvextract writes the .idx + companion .sub pair
        srt = Path(f"{base}.srt")
        try:
            ex = _run(["mkvextract", "tracks", str(mkv_path), f"{track}:{idx}"])
            if ex.returncode != 0:
                log.warning(f"mkvextract failed on {mkv_path.name}: {ex.stderr.strip()}")
                return None
            # vobsub2srt reads <base>.idx/.sub and writes <base>.srt via Tesseract.
            ocr = _run([OCR_CMD, "--tesseract-lang", "eng", str(base)])
            if ocr.returncode != 0:
                log.warning(f"{OCR_CMD} failed on {mkv_path.name}: {ocr.stderr.strip()}")
                return None
            if not srt.exists():
                return None
            return srt.read_text(errors="replace")
        except FileNotFoundError as e:
            log.warning(f"OCR toolchain missing ({e}); falling back to frames")
            return None


def extract_subtitle_text(mkv_path: Path) -> Optional[str]:
    """Extract the main subtitle track to SRT and sample dialogue across the episode.

    Dispatches on codec: TEXT tracks (subrip/ass/…) are transcoded directly with
    ffmpeg (no OCR); IMAGE tracks (VobSub) go through mkvextract + vobsub2srt OCR.
    `_srt_dialogue` then samples spread-out windows from the resulting SRT. Returns
    None (caller falls back to frames) if there's no subtitle track, a tool is
    missing/fails, or extraction yields nothing usable.
    """
    found = _find_subtitle_track(mkv_path)
    if found is None:
        return None
    track, codec = found
    if codec in _TEXT_SUB_CODECS:
        srt_text = _extract_text_srt(mkv_path, track)
    else:
        # VobSub, PGS, or an unknown codec → OCR path (preserves prior behavior).
        srt_text = _ocr_vobsub(mkv_path, track)
    if not srt_text:
        return None
    return _srt_dialogue(srt_text) or None


def extract_frames(mkv_path: Path, out_dir: Path,
                   timestamps=FRAME_TIMESTAMPS) -> List[Path]:
    """Grab one JPEG per timestamp (seconds) with ffmpeg. Skips timestamps that fail
    (e.g. past the runtime). Returns the frames actually written."""
    frames = []
    for i, t in enumerate(timestamps):
        out = out_dir / f"frame_{i}.jpg"
        result = _run([
            "ffmpeg", "-y", "-ss", str(t), "-i", str(mkv_path),
            "-frames:v", "1", "-q:v", "2", str(out),
        ])
        if result.returncode == 0 and out.exists():
            frames.append(out)
        else:
            log.warning(f"ffmpeg frame at {t}s failed on {mkv_path.name}")
    return frames


# ---------------------------------------------------------------------------
# Per-title identification + reconcile
# ---------------------------------------------------------------------------

def identify_title(title: Dict, candidates: List[Dict], config) -> Dict:
    """Identify one ripped title. Tries subtitles first, frames as fallback.

    `title` is a rip dict {path, duration_secs, title_index}. Returns it merged with
    {episode, index_end, confidence, reasoning, method}; episode is None when the
    content matches no candidate (bonus/compilation) or identification wasn't possible.
    """
    mkv = title["path"]
    duration_secs = title.get("duration_secs")
    dialogue = extract_subtitle_text(mkv)
    if dialogue:
        # A malformed model reply is a failure of THIS title's subtitle match, not of the
        # whole rip — degrade to frames rather than letting IdentifyError crash main().
        try:
            result = _match_subtitles(dialogue, candidates, config.anthropic_api_key,
                                      duration_secs)
            if result["episode"] is not None:
                return {**title, **result}
            log.info(f"{mkv.name}: subtitles gave no match; trying frames")
        except IdentifyError as e:
            log.warning(f"{mkv.name}: subtitle match failed ({e}); trying frames")

    with tempfile.TemporaryDirectory() as td:
        frames = extract_frames(mkv, Path(td))
        if frames:
            try:
                result = _match_frames(frames, candidates, config.anthropic_api_key,
                                       duration_secs)
                return {**title, **result}
            except IdentifyError as e:
                log.warning(f"{mkv.name}: frame match failed ({e})")

    log.warning(f"{mkv.name}: could not identify (no subtitles, no frames)")
    return {**title, "season": None, "episode": None, "index_end": None,
            "confidence": 0.0, "reasoning": "no usable content signal", "method": "none"}


def build_filename(show: str, season: int, episode: int, index_end: int = None) -> str:
    """Jellyfin TV filename, e.g. 'The.Office.S01E01.mkv' or 'Friends.S02E12-E13.mkv'."""
    name = re.sub(r"\s+", ".", show.strip())
    tag = f"S{season:02d}E{episode:02d}" + (f"-E{index_end:02d}" if index_end else "")
    return f"{name}.{tag}.mkv"


def build_named_title(title: Dict, show: str, season: int) -> Dict:
    """Merge a reconciled title with the transfer-ready naming fields the rest of the
    pipeline (transfer.send_all) expects — the same shape namer.identify produces.

    The title's OWN season wins when it has one: on a volume disc that spans a
    boundary, each episode is filed under the season it actually matched, not under
    whichever season the run was launched with. `season` is the fallback for titles
    carrying none (the single-season path)."""
    ep_season = title.get("season")
    if ep_season is None:
        ep_season = season
    return {
        **title,
        "jellyfin_filename": build_filename(show, ep_season, title["episode"],
                                            title.get("index_end")),
        "media_type": "tv",
        "destination": "tvshows",
        "is_extra": False,
    }


def _covered_keys(t: Dict) -> set:
    """Episode keys a title spans: {key}, or the range for a double-episode slot. A
    spanning range never crosses a season, so plain key arithmetic holds."""
    lo = _title_key(t)
    end = t.get("index_end")
    hi = episode_key(t.get("season"), end) if end else lo
    return set(range(lo, hi + 1))


def _key_sequence(episode_runtimes: Dict[int, int], guide_keys=None) -> tuple:
    """The ordered universe of episode keys, plus key→position.

    Adjacency is defined over this SEQUENCE, not over key arithmetic: on a volume disc
    the episode after S04E25 is S05E01, whose key is nowhere near it. With one season
    the sequence is the season's episode numbers, so ±1 in position is ±1 in number and
    nothing about the single-season behavior changes."""
    seq = sorted(guide_keys) if guide_keys else sorted(episode_runtimes)
    return seq, {k: i for i, k in enumerate(seq)}


def _adjacent_open_slots(claimed: set, episode_runtimes: Dict[int, int],
                         seq: List[int], pos: Dict[int, int]) -> set:
    """Unclaimed episode keys bordering the disc's block (one step beyond either end, or
    an interior gap) that the guide knows a runtime for — the only slots an off-disc
    match may be reassigned into."""
    if not claimed or not episode_runtimes:
        return set()
    places = [pos[k] for k in claimed if k in pos]
    if not places:
        # No guide sequence to walk (legacy callers pass runtimes only) — neighbors
        # are the numerically adjacent keys, as before.
        lo, hi = min(claimed), max(claimed)
        return {e for e in range(lo - 1, hi + 2) if e not in claimed and e in episode_runtimes}
    lo, hi = min(places), max(places)
    window = {seq[i] for i in range(max(0, lo - 1), min(len(seq), hi + 2))}
    return {k for k in window - claimed if k in episode_runtimes}


def _neighborhood(t: Dict, seq: List[int], pos: Dict[int, int]) -> set:
    """The keys a title touches plus the slots either side of them, in guide order."""
    out = set()
    for k in _covered_keys(t):
        out.add(k)
        i = pos.get(k)
        if i is None:
            out.update({k - 1, k + 1})
            continue
        if i > 0:
            out.add(seq[i - 1])
        if i + 1 < len(seq):
            out.add(seq[i + 1])
    return out


def _resolve_isolated_matches(kept: List[Dict], dropped: List[Dict],
                              episode_runtimes: Dict[int, int],
                              seq: List[int] = None, pos: Dict[int, int] = None) -> List[Dict]:
    """Handle kept titles that sit alone (no episode at ±1) AND matched below
    ISOLATED_MATCH_MIN_CONFIDENCE — the fingerprint of an off-disc match: either a bonus
    reel force-matched to an episode not on this disc, OR a real on-disc episode the
    model matched to a same-length episode elsewhere in the season (S3's two ~42-min
    episodes E10/E23). For the latter we REASSIGN by runtime to an adjacent on-disc
    episode; titles with no fit are dropped. On a volume disc "adjacent" follows the
    guide ACROSS the season boundary — the slot after S04E25 is S05E01 — so a disc that
    straddles one isn't read as two isolated fragments.

    Assignment is one-slot-one-title: several isolated ~42-min titles (a real finale AND
    a same-length bonus 'Play All') can all fit the lone open E23, so the CLOSEST runtime
    wins the slot and the rest stay dropped — otherwise both would be numbered E23.

    Isolated-but-confident matches are kept (a legit lone episode), and real episodes
    stay high-confidence so a gap from a failed OCR never strands them. Needs a contiguous
    block to be isolated FROM, so a handful of titles (< 3) is trusted as-is. Dropped
    titles carry a reason for the approval step."""
    if len(kept) < 3:
        return kept
    if seq is None or pos is None:
        seq, pos = _key_sequence(episode_runtimes)

    anchored, isolated = [], []
    for t in kept:
        neighborhood = _neighborhood(t, seq, pos)
        others = {n for o in kept if o is not t for n in _covered_keys(o)}
        if (neighborhood & others) or t.get("confidence", 1.0) >= ISOLATED_MATCH_MIN_CONFIDENCE:
            anchored.append(t)
        else:
            isolated.append(t)
    if not isolated:
        return kept

    claimed = {n for o in anchored for n in _covered_keys(o)}
    open_slots = _adjacent_open_slots(claimed, episode_runtimes, seq, pos)

    # Every (title, slot) pair within runtime tolerance, best fit first. Greedily claim
    # each slot for its closest title (ties broken by higher confidence); a title or slot
    # is used at most once.
    proposals = []
    for t in isolated:
        dur = t.get("duration_secs")
        if not dur:
            continue
        for e in open_slots:
            rt = episode_runtimes[e]
            if abs(dur - rt) <= RUNTIME_REPAIR_TOL * rt:
                proposals.append((abs(dur - rt), -t.get("confidence", 0.0), id(t), t, e))
    proposals.sort(key=lambda p: p[:3])
    used_titles, used_slots, reassign = set(), set(), {}
    for _, _, tid, t, e in proposals:
        if tid in used_titles or e in used_slots:
            continue
        used_titles.add(tid); used_slots.add(e); reassign[tid] = e

    survivors = list(anchored)
    for t in isolated:
        was = _ep_label(t.get("season"), t["episode"])
        slot = reassign.get(id(t))
        if slot is not None:
            # The slot carries its own season: a reassignment can legitimately cross the
            # boundary on a volume disc (an isolated title that really is S05E01).
            slot_season, slot_ep = split_key(slot)
            if slot_season is None:
                slot_season = t.get("season")
            now = _ep_label(slot_season, slot_ep)
            log.info(f"Content-ID: reassigning title #{t.get('title_index')} "
                     f"{was}->{now} (off-disc match; runtime fits the "
                     "adjacent on-disc episode)")
            survivors.append({
                **t, "season": slot_season, "episode": slot_ep, "index_end": None,
                "method": f"{t.get('method', '')}+runtime-repair".lstrip("+"),
                "reasoning": f"content matched {was} (off-disc, same length); "
                             f"reassigned to adjacent on-disc {now} by runtime",
            })
        else:
            dropped.append({**t, "drop_reason":
                            f"isolated low-confidence match ({was}, "
                            f"conf {t.get('confidence', 1.0):.2f}) — not adjacent to the "
                            "disc's episode block; likely a bonus reel"})
    return survivors


def reconcile(identified: List[Dict], episode_runtimes: Dict[int, int] = None,
              guide_keys: List[int] = None) -> Dict:
    """Map identified titles onto real episode slots, dropping duplicates and bonuses.

    Slots are addressed by `episode_key` (season-qualified when the titles carry a
    season), so a volume disc spanning S04→S05 can't collide E10 with E10.
    `episode_runtimes` maps that key → real runtime (secs), from the guide; used to
    spot 'Play All'/omnibus titles by length. Optional: without it a fallback
    single-episode length is derived from the matched titles' own durations.
    `guide_keys` is the guide's full ordered key list, which defines adjacency across
    a season boundary; without it adjacency falls back to the runtime keys.

    Returns {"kept": [...], "dropped": [...]}:
    - Unmatched titles (episode is None) → dropped as bonus/compilation.
    - A title running >OMNIBUS_RUNTIME_FACTOR× its matched episode's real runtime is a
      'Play All' omnibus (it just opens with that episode) → dropped, even if nothing
      else claimed that number. This is what catches a compilation that content-matched
      a distinct episode instead of colliding with the real single.
    - When several titles still claim the SAME number, the shortest is the real episode;
      the longer ones are dropped as omnibus copies.
    - Everything else is kept, annotated with its final episode/index_end.

    Nothing is deleted from disk here — callers surface the drops for approval
    (Phase 3) so a bad OCR that orphaned a real episode is caught, not lost.
    """
    episode_runtimes = episode_runtimes or {}
    # Fallback reference length when the guide has no runtime for a matched episode:
    # the median of matched titles' durations (robust to a few long omnibus outliers).
    matched_durs = sorted(t["duration_secs"] for t in identified
                          if t.get("episode") is not None and t.get("duration_secs"))
    median_dur = matched_durs[len(matched_durs) // 2] if matched_durs else None

    kept, dropped = [], []
    by_key: Dict[int, List[Dict]] = {}
    for t in identified:
        episode = t.get("episode")
        if episode is None:
            dropped.append({**t, "drop_reason": "no matching episode (bonus/compilation)"})
            continue
        key = _title_key(t)
        label = _ep_label(t.get("season"), episode)
        ref = episode_runtimes.get(key) or median_dur
        dur = t.get("duration_secs")
        if ref and dur and dur > ref * OMNIBUS_RUNTIME_FACTOR:
            dropped.append({**t, "drop_reason":
                            f"Play-All/omnibus ({int(dur // 60)} min — "
                            f"~{dur / ref:.1f}× episode length)"})
            continue
        # Too SHORT for the episode it matched → a bonus, not the episode. Checked only
        # against the real guide runtime (not median_dur) so it never fires off ripped junk.
        guide_ref = episode_runtimes.get(key)
        if guide_ref and dur and dur < guide_ref * SHORT_RUNTIME_FACTOR:
            dropped.append({**t, "drop_reason":
                            f"too short for {label} ({int(dur // 60)} min — "
                            f"~{dur / guide_ref:.1f}× episode length; likely a bonus/gag reel)"})
            continue
        by_key.setdefault(key, []).append(t)

    for key, group in sorted(by_key.items()):
        if len(group) == 1:
            kept.append(group[0])
            continue
        # Several titles claim the same number. Keep the one whose runtime is CLOSEST to
        # the episode's real length; the rest are non-episodes that merely share its
        # dialogue — a longer 'Play All' that opens with it, OR a SHORTER bonus (gag reel,
        # deleted scenes) cut from it. The old rule kept the shortest, assuming the extra
        # was always a longer compilation — so a short gag reel beat the real episode and
        # the real one was dropped (The Office S2 E20). Needs the guide runtime; with none
        # (legacy path) fall back to shortest-wins.
        ref = episode_runtimes.get(key)
        if ref:
            group.sort(key=lambda t: abs(t.get("duration_secs", 0) - ref))
        else:
            group.sort(key=lambda t: t.get("duration_secs", 0))
        kept.append(group[0])
        real_dur = group[0].get("duration_secs", 0)
        label = _ep_label(group[0].get("season"), group[0]["episode"])
        for dup in group[1:]:
            longer = dup.get("duration_secs", 0) > real_dur
            kind = "longer — 'Play All'" if longer else "shorter — bonus/gag reel"
            dropped.append({**dup, "drop_reason": f"duplicate of {label} ({kind})"})

    seq, pos = _key_sequence(episode_runtimes, guide_keys)
    kept = _resolve_isolated_matches(kept, dropped, episode_runtimes, seq, pos)
    kept.sort(key=_title_key)
    return {"kept": kept, "dropped": dropped}
