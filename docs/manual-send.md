# Manually sending a held rip

When a rip is **held** (you tap **Fix** in Discord, or approval times out), the ripped
files are kept in `TEMP_DIR` and nothing is transferred. This is how to send the correct
episodes over by hand — no re-ripping.

## Where the files are

Held files live in `/var/tmp/ai-ripper/` (from `TEMP_DIR` in `.env`), named by disc
label + MakeMKV title index:

```
/var/tmp/ai-ripper/B1_t00.mkv   <- the "_t00" is title index 0
/var/tmp/ai-ripper/B3_t01.mkv
...
```

The source name doesn't matter — what matters is the **destination filename**, which is
what Jellyfin reads (`The.Office.S02E13.mkv`).

## Where they go

Path is built from `.env`: `MEDIA_ROOT` + `tvshows/<Show>/Season <NN>/<file>`.

```
/mnt/media/media/tvshows/The Office/Season 02/The.Office.S02E13.mkv
```

Server: `dillon@100.100.212.32`.

## Send a file (rsync — resumable, recommended)

```bash
rsync -avP /var/tmp/ai-ripper/B1_t00.mkv \
  dillon@100.100.212.32:"/mnt/media/media/tvshows/The Office/Season 02/The.Office.S02E13.mkv"
```

`-P` (= `--partial --progress`) is the key flag: if the transfer dies partway, just run
the same command again and it resumes instead of starting over. `-a` preserves
attributes, `-v` is verbose.

Repeat per episode, changing the source file and the `E<NN>` in the destination. Only
send the real episodes — leave Play-All compilations and bonus reels behind.

> The trailing `\` is just a line-continuation so the command can span two lines. You can
> delete it and write the whole command on one line if you prefer.

## scp (fallback — what the pipeline uses)

Same idea, but **not** resumable — a killed transfer leaves a partial file you must
delete before retrying.

```bash
scp /var/tmp/ai-ripper/B1_t00.mkv \
  dillon@100.100.212.32:"/mnt/media/media/tvshows/The Office/Season 02/The.Office.S02E13.mkv"
```

## First time for a season? Create the folder

rsync/scp won't create the parent directories — make the season folder first (existing
folders are fine to skip):

```bash
ssh dillon@100.100.212.32 'mkdir -p "/mnt/media/media/tvshows/The Office/Season 02"'
```

## After sending

- **See it in Jellyfin now:** trigger a library scan (or wait for the scheduled one).
- **Clean up temp files** once everything's on the server:

  ```bash
  rm -f /var/tmp/ai-ripper/*.mkv
  ```

- **Eject the disc** if it's still in the drive: `eject`.

## Quick reference: The Office S2 disc 3 (the one that went wrong)

The 6 real episodes were E13–E18; a 31-min bonus reel got misidentified as E20 and was
dropped. Mapping used:

| Source file        | Episode |
|--------------------|---------|
| `B1_t00.mkv`       | E13     |
| `B3_t01.mkv`       | E14     |
| `B5_t02.mkv`       | E15     |
| `C1_t04.mkv`       | E16     |
| `C3_t05.mkv`       | E17     |
| `C5_t06.mkv`       | E18     |
