"""Chapters in the MP3 file (ID3v2.3 CTOC + CHAP frames, read by podcast apps):
one chapter per track played (Notion, see extras.notion_tracks), titled
"Title — Artist", with the album cover (APIC, at most 600 x 600 JPEG), plus a first
chapter at 0 s for the start of the show. Short excerpts get a chapter without a
cover. A cover that cannot be downloaded only leaves its chapter without a picture.
Nothing about the tracks goes to the public log: only counts.
"""
import io

import requests

import extras

COVER_SIZE = 600
FIRST = {"fr": "Début de l'émission", "en": "Start of the show"}
TOC_TITLE = {"fr": "Chapitres", "en": "Chapters"}


def language():
    return "fr" if extras.labels() is extras.LABELS["fr"] else "en"


def cover_jpeg(url):
    """The cover as a JPEG of at most COVER_SIZE x COVER_SIZE, or None."""
    from PIL import Image  # only needed here

    try:
        r = requests.get(url, headers={"User-Agent": extras.USER_AGENT}, timeout=30)
        r.raise_for_status()
        image = Image.open(io.BytesIO(r.content))
        image = image.convert("RGB")
        image.thumbnail((COVER_SIZE, COVER_SIZE))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()
    except Exception:  # bad link, not a picture...: chapter without a cover
        return None


def plan(tracks, start, duration_ms):
    """[(start_ms, end_ms, title, cover_url)] from the tracks, in order, inside the file."""
    marks = []
    for t in tracks:
        ms = int((t["start"] - start).total_seconds() * 1000)
        if ms < 0 or ms >= duration_ms:
            continue
        title = t["title"] + (f" — {t['artist']}" if t["artist"] else "")
        cover = "" if t.get("status") == "Excerpt" else t.get("cover", "")
        if marks and ms <= marks[-1][0]:  # same start as the previous one: keep it apart
            ms = marks[-1][0] + 1
        marks.append((ms, title, cover))
    if not marks:
        return []
    if marks[0][0] > 0:
        marks.insert(0, (0, FIRST[language()], ""))
    return [(ms, marks[i + 1][0] if i + 1 < len(marks) else duration_ms, title, cover)
            for i, (ms, title, cover) in enumerate(marks)]


def write(path, tracks, start):
    """Writes the chapters into the MP3 file; returns (chapters, covers) written."""
    from mutagen.id3 import APIC, CHAP, CTOC, ID3, TIT2, CTOCFlags, ID3NoHeaderError
    from mutagen.mp3 import MP3

    duration_ms = int(MP3(path).info.length * 1000)
    chapters = plan(tracks, start, duration_ms)
    if not chapters:
        return 0, 0
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for key in ("CTOC", "CHAP"):  # chapters of an earlier run
        tags.delall(key)
    ids, covers = [], 0
    for n, (begin, end, title, cover) in enumerate(chapters, 1):
        element = f"chp{n:03d}"
        frames = [TIT2(encoding=1, text=[title])]
        data = cover_jpeg(cover) if cover else None
        if data:
            frames.append(APIC(encoding=1, mime="image/jpeg", type=3, desc="", data=data))
            covers += 1
        tags.add(CHAP(element_id=element, start_time=begin, end_time=end,
                      start_offset=0xFFFFFFFF, end_offset=0xFFFFFFFF, sub_frames=frames))
        ids.append(element)
    tags.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                  child_element_ids=ids, sub_frames=[TIT2(encoding=1, text=[TOC_TITLE[language()]])]))
    tags.save(path, v2_version=3)
    return len(chapters), covers
