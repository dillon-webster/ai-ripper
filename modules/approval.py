"""Phase 3 — human approval gate over Discord.

Content-based identification (modules/identify.py) is usually right but not 100%
(see docs/episode-identification-plan.md), so before a proposed mapping is written
to the library a human confirms it. This replaces the `--dry-run` stopgap with a
one-tap Approve / Fix decision reviewable from a phone.

The one-way webhook in modules/notifier.py can't receive button taps, so this uses
a Discord **bot** (gateway connection). `discord.py` is imported lazily inside
request_approval so the rest of the pipeline (and the test suite) runs without it
installed — only an actual approval call needs the dependency.

`request_approval` is synchronous from the caller's view: it spins up its own
asyncio loop, posts the proposal, blocks for the tap (or a long timeout), tears the
client down, and returns a Decision. It NEVER raises — anything unexpected (bot not
configured, gateway/login failure, timeout) degrades to Decision(approved=False) so
the caller holds the files for manual handling rather than transferring blind.
"""
import asyncio
import contextlib
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECS = 1800
_EMBED_FIELD_MAX = 1024  # Discord hard limit on an embed field value
_MAX_EPISODE_EMBEDS = 9  # Discord allows 10 embeds/msg; reserve 1 for the header/dropped
_THUMB_TIMESTAMP_FRAC = 0.40  # grab a frame ~40% in — past recaps/montage, into the plot
_THUMB_FALLBACK_SECS = 300  # if a title has no known duration
_THUMB_TIMEOUT_SECS = 60
_EPISODE_TAG_RE = re.compile(r"S\d+E\d+(?:-E\d+)?", re.IGNORECASE)


@dataclass
class Decision:
    approved: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Pure formatting (no discord dependency — unit-tested directly)
# ---------------------------------------------------------------------------

def _fmt_duration(secs: Optional[int]) -> str:
    if not secs:
        return "?"
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


def _src_name(t: Dict) -> str:
    if t.get("path"):
        return Path(t["path"]).name
    return f"title #{t.get('title_index')}"


def format_mapping(named: List[Dict]) -> List[str]:
    """One line per proposed episode: source → target [— name] [method, conf]."""
    lines = []
    for t in sorted(named, key=lambda x: (x.get("episode") is None, x.get("episode") or 0)):
        conf, method = t.get("confidence"), t.get("method")
        tag = f"  [{method}, {conf:.2f}]" if method and conf is not None else ""
        name = t.get("episode_name")
        namepart = f" — {name}" if name else ""
        lines.append(f"{_src_name(t)} → {t['jellyfin_filename']}{namepart}{tag}")
    return lines


def format_dropped(dropped: List[Dict]) -> List[str]:
    """One line per dropped (not-transferred) title: source (duration) — why."""
    return [
        f"{_src_name(d)} ({_fmt_duration(d.get('duration_secs'))}) — {d.get('drop_reason', 'dropped')}"
        for d in dropped
    ]


def format_proposal(named: List[Dict], dropped: List[Dict]) -> str:
    """Plain-text proposal, used as the message headline / fallback content."""
    parts = ["**Proposed episodes:**"] + [f"• {ln}" for ln in format_mapping(named)]
    if dropped:
        parts += ["", "**Dropped (not transferred):**"] + [f"• {ln}" for ln in format_dropped(dropped)]
    return "\n".join(parts)


def _clip(text: str) -> str:
    if len(text) <= _EMBED_FIELD_MAX:
        return text
    return text[: _EMBED_FIELD_MAX - 1] + "…"


def _build_embed(named: List[Dict], dropped: List[Dict], discord):
    """Compact single-embed fallback (used when no thumbnails could be extracted)."""
    embed = discord.Embed(
        title="🎬 Rip ready for approval",
        description="Review the episode mapping, then tap **Approve** or **Fix**.",
    )
    embed.add_field(
        name=f"Episodes ({len(named)})",
        value=_clip("\n".join(format_mapping(named)) or "(none)"),
        inline=False,
    )
    if dropped:
        embed.add_field(
            name=f"Dropped ({len(dropped)})",
            value=_clip("\n".join(format_dropped(dropped))),
            inline=False,
        )
    return embed


# ---------------------------------------------------------------------------
# Thumbnails: one representative frame per proposed episode
# ---------------------------------------------------------------------------

def _extract_thumbnail(title: Dict, out_path: Path) -> bool:
    """Grab one downscaled JPEG ~40% into the episode with ffmpeg. Best-effort:
    returns False (and the caller shows that episode without an image) if the title
    has no file, ffmpeg is missing, or the grab fails. Never raises."""
    mkv = title.get("path")
    if not mkv:
        return False
    dur = title.get("duration_secs")
    ts = int(dur * _THUMB_TIMESTAMP_FRAC) if dur else _THUMB_FALLBACK_SECS
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ts), "-i", str(mkv),
             "-frames:v", "1", "-q:v", "5", "-vf", "scale=480:-1", str(out_path)],
            capture_output=True, text=True, timeout=_THUMB_TIMEOUT_SECS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"thumbnail ffmpeg failed for {getattr(mkv, 'name', mkv)}: {e}")
        return False
    if result.returncode != 0 or not out_path.exists():
        log.warning(f"thumbnail grab returned no frame for {getattr(mkv, 'name', mkv)}")
        return False
    return True


def _extract_thumbnails(named: List[Dict], out_dir: Path) -> Dict:
    """Extract a thumbnail per shown episode. Returns {title_index: path}."""
    thumbs = {}
    ordered = _order_for_display(named)[:_MAX_EPISODE_EMBEDS]
    for i, t in enumerate(ordered):
        out = out_dir / f"ep_{i}.jpg"
        if _extract_thumbnail(t, out):
            thumbs[t["title_index"]] = out
    return thumbs


def _order_for_display(named: List[Dict]) -> List[Dict]:
    return sorted(named, key=lambda x: (x.get("episode") is None, x.get("episode") or 0))


def _episode_title(t: Dict) -> str:
    """'S02E01 — The Dundies' from a named title (falls back to the filename)."""
    m = _EPISODE_TAG_RE.search(t.get("jellyfin_filename", ""))
    tag = m.group(0).upper() if m else t.get("jellyfin_filename", "?")
    name = t.get("episode_name")
    return f"{tag} — {name}" if name else tag


def _build_gallery(named: List[Dict], dropped: List[Dict], thumbs: Dict, discord):
    """Header embed + one image-embed per episode. Returns (embeds, files).

    Falls back to the compact single embed if no thumbnails were extracted, so the
    approval still works when ffmpeg is unavailable."""
    if not thumbs:
        return [_build_embed(named, dropped, discord)], []

    ordered = _order_for_display(named)
    shown, overflow = ordered[:_MAX_EPISODE_EMBEDS], ordered[_MAX_EPISODE_EMBEDS:]

    header = discord.Embed(
        title="🎬 Rip ready for approval",
        description=f"{len(named)} episode(s) — check the frames below, then tap "
                    "**Approve** or **Fix**.",
    )
    if overflow:
        header.add_field(
            name=f"+{len(overflow)} more episode(s)",
            value=_clip("\n".join(format_mapping(overflow))), inline=False)
    if dropped:
        header.add_field(
            name=f"Dropped — not transferred ({len(dropped)})",
            value=_clip("\n".join(format_dropped(dropped))), inline=False)

    embeds, files = [header], []
    for i, t in enumerate(shown):
        conf, method = t.get("confidence"), t.get("method")
        tag = f"{method} · {conf:.2f}" if method and conf is not None else ""
        src = _src_name(t)
        embed = discord.Embed(title=_episode_title(t),
                              description=f"{src}  ·  {tag}" if tag else src)
        thumb = thumbs.get(t["title_index"])
        if thumb:
            fn = f"ep_{i}.jpg"
            embed.set_image(url=f"attachment://{fn}")
            files.append(discord.File(str(thumb), filename=fn))
        embeds.append(embed)
    return embeds, files


# ---------------------------------------------------------------------------
# Discord bot driver (blocking wait)
# ---------------------------------------------------------------------------

def request_approval(named: List[Dict], dropped: List[Dict], config,
                     timeout: int = None) -> Decision:
    """Post the proposed mapping to Discord and block until Approve/Fix (or timeout).

    Returns Decision(approved=True) only on an explicit Approve tap. Every other
    outcome — bot not configured, gateway failure, Fix tap, timeout — returns
    approved=False so the caller holds the files. Never raises.
    """
    timeout = timeout or getattr(config, "approval_timeout_secs", DEFAULT_TIMEOUT_SECS)
    token = getattr(config, "discord_bot_token", "")
    channel_id = getattr(config, "discord_channel_id", "")
    if not token or not channel_id:
        log.warning("Discord bot token/channel not configured — cannot request approval; "
                    "holding files for manual handling.")
        return Decision(False, "approval unavailable (bot not configured)")

    # Thumbnails are grabbed synchronously here (ffmpeg subprocess) BEFORE the async
    # driver, so the temp files stay alive for the whole send. The dir is torn down on
    # return. Any failure just means fewer/no images — the gallery degrades gracefully.
    try:
        with tempfile.TemporaryDirectory(prefix="ai-ripper-thumbs-") as td:
            thumbs = _extract_thumbnails(named, Path(td))
            return asyncio.run(
                _drive(named, dropped, thumbs, token, int(channel_id), timeout))
    except Exception as e:  # noqa: BLE001 — approval must never crash the rip
        log.warning(f"Approval flow failed ({e}); holding files for manual handling.")
        return Decision(False, f"approval error: {e}")


async def _drive(named, dropped, thumbs, token: str, channel_id: int, timeout: int) -> Decision:
    import discord  # lazy: only an actual approval needs discord.py installed

    intents = discord.Intents.none()
    intents.guilds = True  # needed to resolve/send to the target channel
    client = discord.Client(intents=intents)

    outcome: Dict[str, Optional[Decision]] = {"decision": None}
    finished = asyncio.Event()

    class ApprovalView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=timeout)

        def _disable(self):
            for child in self.children:
                child.disabled = True

        @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
        async def approve(self, interaction, button):
            outcome["decision"] = Decision(True, f"approved by {interaction.user.display_name}")
            self._disable()
            await interaction.response.edit_message(
                content="✅ Approved — transferring to Jellyfin.", view=self)
            finished.set()

        @discord.ui.button(label="Fix", style=discord.ButtonStyle.secondary, emoji="✏️")
        async def fix(self, interaction, button):
            outcome["decision"] = Decision(False, f"held by {interaction.user.display_name}")
            self._disable()
            await interaction.response.edit_message(
                content="✏️ Held — files kept for manual handling.", view=self)
            finished.set()

        async def on_timeout(self):
            outcome["decision"] = Decision(False, "approval timed out")
            finished.set()

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            # A header embed + one image-embed per episode (falls back to a single
            # compact embed if no thumbnails were extracted). The short content line is
            # what shows in the phone push preview.
            embeds, files = _build_gallery(named, dropped, thumbs, discord)
            await channel.send(
                content="🎬 New rip ready for approval — review below.",
                embeds=embeds,
                files=files,
                view=ApprovalView(),
            )
            log.info("Approval request posted to Discord — waiting for Approve/Fix.")
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failed to post approval message ({e})")
            outcome["decision"] = Decision(False, f"post failed: {e}")
            finished.set()

    start_task = asyncio.create_task(client.start(token))
    try:
        # +60s guards against a gateway that connects but never fires on_timeout.
        await asyncio.wait_for(finished.wait(), timeout=timeout + 60)
    except asyncio.TimeoutError:
        outcome["decision"] = Decision(False, "approval timed out (no gateway response)")
    finally:
        await client.close()
        with contextlib.suppress(Exception):
            await start_task

    return outcome["decision"] or Decision(False, "no decision")
