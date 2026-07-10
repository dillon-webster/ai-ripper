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

_SUBPROCESS_TIMEOUT = 300


class IdentifyError(Exception):
    pass


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
    """
    lines = []
    for c in candidates:
        rng = f"E{c['index']:02d}" + (f"-E{c['index_end']:02d}" if c.get("index_end") else "")
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
    "- Judge by plot/dialogue, not runtime alone — runtime only breaks ties.\n"
)


def _build_subtitle_prompt(dialogue: str, candidates: List[Dict]) -> str:
    return (
        "You are identifying which TV episode a ripped disc title contains, to number it "
        "for a Jellyfin library. Match the opening dialogue below to exactly one episode.\n\n"
        "Candidate episodes for this season (the ONLY valid answers):\n"
        f"{_candidate_lines(candidates)}\n\n"
        "Opening subtitle dialogue (first ~2 minutes, OCR'd from the disc — may contain "
        f"OCR errors):\n\"\"\"\n{dialogue.strip()}\n\"\"\"\n\n"
        f"{_MATCH_RULES}"
    )


def _build_frame_prompt(candidates: List[Dict]) -> str:
    return (
        "You are identifying which TV episode a ripped disc title contains, to number it "
        "for a Jellyfin library. The images are frames sampled from the title (a teaser "
        "shot and two later scenes; the shared opening-title montage is skipped). Match "
        "them to exactly one episode.\n\n"
        "Candidate episodes for this season (the ONLY valid answers):\n"
        f"{_candidate_lines(candidates)}\n\n"
        f"{_MATCH_RULES}"
    )


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    return m.group(1) if m else text


def _parse_match(raw: str, candidates: List[Dict], method: str) -> Dict:
    """Parse the model's JSON and clamp it to the candidate set.

    An out-of-list episode number is treated as 'no confident match' (episode=None)
    rather than trusted — the whole point is to never number a title as an episode
    that doesn't exist in the season.
    """
    try:
        data = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, TypeError) as e:
        raise IdentifyError(f"Malformed match JSON: {e}") from e

    episode = data.get("episode")
    valid = {c["index"] for c in candidates}
    if episode is not None and episode not in valid:
        log.warning(f"Model returned episode {episode} not in candidate list; treating as unmatched")
        episode = None

    index_end = data.get("index_end") if episode is not None else None
    return {
        "episode": episode,
        "index_end": index_end,
        "confidence": float(data.get("confidence") or 0.0),
        "reasoning": data.get("reasoning", ""),
        "method": method,
    }


def _match_subtitles(dialogue: str, candidates: List[Dict], api_key: str) -> Dict:
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": _build_subtitle_prompt(dialogue, candidates)}],
    )
    return _parse_match(resp.content[0].text.strip(), candidates, method="subtitles")


def _match_frames(frame_paths: List[Path], candidates: List[Dict], api_key: str) -> Dict:
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
    content.append({"type": "text", "text": _build_frame_prompt(candidates)})
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


def _find_subtitle_track(mkv_path: Path) -> Optional[int]:
    """Return the stream index of the first subtitle track, or None. Uses ffprobe."""
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index", "-of", "json", str(mkv_path),
    ])
    if result.returncode != 0:
        log.warning(f"ffprobe failed on {mkv_path.name}: {result.stderr.strip()}")
        return None
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        return None
    return streams[0]["index"] if streams else None


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


def extract_subtitle_text(mkv_path: Path) -> Optional[str]:
    """Extract + OCR the main subtitle track, then sample dialogue across the episode.

    DVD subs are image-based VobSub: mkvextract pulls the .idx/.sub pair, vobsub2srt
    runs Tesseract to produce an .srt for the whole track, from which `_srt_dialogue`
    samples spread-out windows. Returns None (caller falls back to frames) if there's
    no subtitle track, a tool is missing, or OCR yields nothing usable.
    """
    track = _find_subtitle_track(mkv_path)
    if track is None:
        return None
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
            dialogue = _srt_dialogue(srt.read_text(errors="replace"))
        except FileNotFoundError as e:
            log.warning(f"OCR toolchain missing ({e}); falling back to frames")
            return None
    return dialogue or None


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
    dialogue = extract_subtitle_text(mkv)
    if dialogue:
        # A malformed model reply is a failure of THIS title's subtitle match, not of the
        # whole rip — degrade to frames rather than letting IdentifyError crash main().
        try:
            result = _match_subtitles(dialogue, candidates, config.anthropic_api_key)
            if result["episode"] is not None:
                return {**title, **result}
            log.info(f"{mkv.name}: subtitles gave no match; trying frames")
        except IdentifyError as e:
            log.warning(f"{mkv.name}: subtitle match failed ({e}); trying frames")

    with tempfile.TemporaryDirectory() as td:
        frames = extract_frames(mkv, Path(td))
        if frames:
            try:
                result = _match_frames(frames, candidates, config.anthropic_api_key)
                return {**title, **result}
            except IdentifyError as e:
                log.warning(f"{mkv.name}: frame match failed ({e})")

    log.warning(f"{mkv.name}: could not identify (no subtitles, no frames)")
    return {**title, "episode": None, "index_end": None,
            "confidence": 0.0, "reasoning": "no usable content signal", "method": "none"}


def build_filename(show: str, season: int, episode: int, index_end: int = None) -> str:
    """Jellyfin TV filename, e.g. 'The.Office.S01E01.mkv' or 'Friends.S02E12-E13.mkv'."""
    name = re.sub(r"\s+", ".", show.strip())
    tag = f"S{season:02d}E{episode:02d}" + (f"-E{index_end:02d}" if index_end else "")
    return f"{name}.{tag}.mkv"


def build_named_title(title: Dict, show: str, season: int) -> Dict:
    """Merge a reconciled title with the transfer-ready naming fields the rest of the
    pipeline (transfer.send_all) expects — the same shape namer.identify produces."""
    return {
        **title,
        "jellyfin_filename": build_filename(show, season, title["episode"], title.get("index_end")),
        "media_type": "tv",
        "destination": "tvshows",
        "is_extra": False,
    }


def reconcile(identified: List[Dict]) -> Dict:
    """Map identified titles onto real episode numbers, dropping duplicates and bonuses.

    Returns {"kept": [...], "dropped": [...]}:
    - Unmatched titles (episode is None) → dropped as bonus/compilation.
    - When several titles claim the SAME episode number, the one whose duration is
      closest to a single episode is the real one; the longer ones are 'Play All'
      omnibus titles → dropped.
    - Everything else is kept, annotated with its final episode/index_end.

    Nothing is deleted from disk here — callers surface the drops for approval
    (Phase 3) so a bad OCR that orphaned a real episode is caught, not lost.
    """
    kept, dropped = [], []
    by_episode: Dict[int, List[Dict]] = {}
    for t in identified:
        if t.get("episode") is None:
            dropped.append({**t, "drop_reason": "no matching episode (bonus/compilation)"})
        else:
            by_episode.setdefault(t["episode"], []).append(t)

    for episode, group in sorted(by_episode.items()):
        if len(group) == 1:
            kept.append(group[0])
            continue
        # Duplicate claims: the shortest title is the real single episode; the rest
        # are Play-All/omnibus copies that also open with this episode.
        group.sort(key=lambda t: t.get("duration_secs", 0))
        kept.append(group[0])
        for dup in group[1:]:
            dropped.append({**dup, "drop_reason": f"duplicate of E{episode:02d} (longer — 'Play All')"})

    kept.sort(key=lambda t: t["episode"])
    return {"kept": kept, "dropped": dropped}
