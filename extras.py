"""Episode extras, added to the Drive description (and so to the podcast feed):

- picture and keywords of the day's show, read on a public web page (secret SHOW_PAGE_URL)
  listing the recent shows, one section per show under a heading giving its date;
- the tracks played during the recording, read in a Notion database written by another
  tool (secrets NOTION_TOKEN and NOTION_HISTORY_DB; properties Title, Artist,
  "Played at", Link);
- positions in the file (H:MM:SS, understood by podcast apps) instead of clock times.

The description is made of labelled sections (keywords, summaries, tracks): each tool
updates its own sections and keeps the others. Nothing read here goes to the public log.
"""
import os
import re
from datetime import datetime, time as dtime, timedelta
from html.parser import HTMLParser
from xml.sax.saxutils import escape

import requests

import record
from record import CURRENT_NAME, TZ, push

NOTION_API = "https://api.notion.com/v1"
EXTRAS_DAYS = 3        # pictures and keywords are looked for during this many days
TRACKS_DAYS = 2        # the track list is refreshed during this many days
TRACKS_DELAY = timedelta(minutes=10)  # after the end of the recording
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

# Description sections, in display order. Headers are read in both languages.
SECTIONS = ("keywords", "claude", "local", "summary", "tracks")
LABELS = {
    "fr": {"keywords": "Mots-clés", "claude": "Claude", "local": "Mistral",
           "summary": "Résumé", "tracks": "Titres diffusés"},
    "en": {"keywords": "Keywords", "claude": "Claude", "local": "Mistral",
           "summary": "Summary", "tracks": "Tracks played"},
}
HEADER = re.compile(r"^(Mots-clés|Keywords|Claude|Mistral|Résumé|Summary|Titres diffusés|"
                    r"Tracks played) ?:$")
HEADER_KEYS = {label: key for labels in LABELS.values() for key, label in labels.items()}

MONTHS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
          "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
          "novembre": 11, "décembre": 12, "decembre": 12, "january": 1, "february": 2,
          "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12}
DATE = re.compile(r"\b(\d{1,2})(?:er|st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b",
                  re.IGNORECASE)
CLOCK = re.compile(r"[\[(](\d{1,2}):(\d{2})[\])]")


def labels():
    lang = os.environ.get("TRANSCRIPT_LANGUAGE", "").strip().lower()
    return LABELS["fr" if lang == "fr" else "en"]


def parse(description):
    """Description -> {section: text}. Text before any header is an older summary."""
    sections, key, lines = {}, "summary", []
    for line in (description or "").split("\n"):
        m = HEADER.match(line.strip())
        if m:
            if "\n".join(lines).strip():
                sections[key] = "\n".join(lines).strip()
            key, lines = HEADER_KEYS[m.group(1)], []
        else:
            lines.append(line)
    if "\n".join(lines).strip():
        sections[key] = "\n".join(lines).strip()
    return sections


def compose(sections):
    names = labels()
    sep = " :" if names is LABELS["fr"] else ":"
    return "\n\n".join(f"{names[k]}{sep}\n{sections[k].strip()}"
                       for k in SECTIONS if sections.get(k, "").strip())


def has_summary(description):
    return any(k in parse(description) for k in ("claude", "local", "summary"))


def clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def name_times(name):
    """(start, end) from a name like '<prefix> 25/12/26 18:00-23:30.mp3', or (None, None)."""
    m = CURRENT_NAME.match(name or "")
    if not m:
        return None, None
    _, d, mo, yy, h1, m1, h2, m2, _ = m.groups()
    start = datetime(2000 + int(yy), int(mo), int(d), int(h1), int(m1), tzinfo=TZ)
    end = datetime.combine(start.date(), dtime(int(h2), int(m2)), TZ)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def file_start(f):
    """Real start of the recording (stored at upload), else the start in its name."""
    stored = (f.get("appProperties") or {}).get("start", "")
    try:
        return datetime.fromisoformat(stored).astimezone(TZ)
    except ValueError:
        return name_times(f.get("name"))[0]


def positions(text, start):
    """'[23:30]' or '(23:30)' (clock time) -> '2:30:00 (23h30)': podcast apps read
    H:MM:SS as a position in the file, but read '23:30' as 23 minutes 30 seconds."""
    if not text or not start:
        return text

    def replace(m):
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return m.group(0)
        when = datetime.combine(start.date(), dtime(h, mi), TZ)
        if when < start - timedelta(hours=2):  # after midnight
            when += timedelta(days=1)
        offset = (when - start).total_seconds()
        if offset < -300:
            return f"({h:02d}h{mi:02d})"
        return f"{clock(max(offset, 0))} ({h:02d}h{mi:02d})"

    return CLOCK.sub(replace, text)


# --- Show page ------------------------------------------------------------------------

class _Page(HTMLParser):
    """Turns a page into a list of ('heading' | 'text' | 'img', value) in order."""
    BLOCKS = {"p", "div", "li", "section", "article", "figure", "figcaption", "td", "br",
              "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "span"}
    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.text, self.heading, self.skip = [], [], None, 0

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self.text)).strip()
        self.text = []
        if text:
            self.items.append(("heading" if self.heading else "text", text))

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag in self.BLOCKS and tag != "span":
            self.flush()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.heading = tag
        if tag == "img":
            a = dict(attrs)
            src = next((a[k] for k in ("data-lazy-src", "data-src", "src")
                        if a.get(k) and not a[k].startswith("data:")), "")
            if src:
                self.items.append(("img", src.strip()))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(self.skip - 1, 0)
            return
        if tag in self.BLOCKS and tag != "span":
            self.flush()
        if tag == self.heading:
            self.heading = None

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)


def show_sections(html, base_url=""):
    """{date: {"image": url, "images": [...], "keywords": text}} for every dated
    heading of the page. Pictures repeated in several sections (banners) are ignored."""
    parser = _Page()
    parser.feed(html)
    parser.flush()
    sections, current = {}, None
    for kind, value in parser.items:
        if kind == "heading":
            m = DATE.search(value)
            if m:
                day = datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).date()
                current = sections.setdefault(day, {"images": [], "texts": []})
            else:
                current = None
        elif current is not None:
            if kind == "img":
                url = requests.compat.urljoin(base_url, value)
                if url not in current["images"]:
                    current["images"].append(url)
            else:
                current["texts"].append(value)
    count = {}
    for s in sections.values():
        for url in s["images"]:
            count[url] = count.get(url, 0) + 1
    result = {}
    for day, s in sections.items():
        images = [u for u in s["images"] if count[u] == 1 or len(sections) == 1]
        # Keywords: the longest text made of many comma-separated words.
        lists = [t for t in s["texts"] if t.count(",") >= 8]
        keywords = max(lists, key=len).strip().rstrip(",").strip() if lists else ""
        result[day] = {"image": images[0] if images else "", "images": images,
                       "keywords": keywords}
    return result


def fetch_show_page():
    url = os.environ.get("SHOW_PAGE_URL", "").strip()
    if not url:
        return None
    r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "fr,en"},
                     timeout=60)
    r.raise_for_status()
    return show_sections(r.text, url)


# --- Tracks (Notion) ------------------------------------------------------------------

def _plain(prop):
    items = (prop or {}).get("title") or (prop or {}).get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in items).strip()


def notion_tracks(start, end):
    """[(played_at, artist, title, link)] played between start and end, or None if the
    Notion secrets are not set."""
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db = os.environ.get("NOTION_HISTORY_DB", "").strip()
    m = re.search(r"[0-9a-fA-F]{32}", db.replace("-", ""))
    if not (token and m):
        return None
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}
    body = {"filter": {"and": [
                {"property": "Played at", "date": {"on_or_after": start.isoformat()}},
                {"property": "Played at", "date": {"before": end.isoformat()}}]},
            "sorts": [{"property": "Played at", "direction": "ascending"}], "page_size": 100}
    tracks = []
    while True:
        r = requests.post(f"{NOTION_API}/databases/{m.group(0)}/query", json=body,
                          headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        for page in data.get("results", []):
            p = page.get("properties") or {}
            when = ((p.get("Played at") or {}).get("date") or {}).get("start")
            title = _plain(p.get("Title"))
            if not (when and title):
                continue
            try:
                played = datetime.fromisoformat(when).astimezone(TZ)
            except ValueError:
                continue
            tracks.append((played, _plain(p.get("Artist")), title,
                           (p.get("Link") or {}).get("url") or ""))
        if not data.get("has_more"):
            return tracks
        body["start_cursor"] = data.get("next_cursor")


def track_lines(tracks, start):
    lines = []
    for played, artist, title, link in tracks:
        offset = max((played - start).total_seconds(), 0)
        line = f"{clock(offset)} ({played:%Hh%M}) " + (f"{artist} – {title}" if artist else title)
        lines.append(line + (f" · {link}" if link else ""))
    return "\n".join(lines)


# --- Update of the recent episodes ----------------------------------------------------

def save_picture(drive, folder_id, url, label):
    """Copies the picture to the Drive folder (the site keeps it 15 days only) and
    shares it; returns its Drive id."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    r.raise_for_status()
    mime = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        raise RuntimeError("Not a picture")
    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".jpg")
    file_id = drive.upload_bytes(r.content, label + ext, folder_id, mime)
    drive.share(file_id)
    return file_id


def refresh(drive, folder_id, files=None, log=print, notify_tracks=False):
    """Adds the picture, keywords and track list to the recent episodes that miss them.
    Returns True if a description changed (the podcast feed must then be rebuilt)."""
    files = files if files is not None else drive.list_audio(folder_id)
    now = datetime.now(TZ)
    page, page_error, changed = None, None, False
    for f in files:
        start = file_start(f)
        _, end = name_times(f["name"])
        if not start or not end or now - end > timedelta(days=EXTRAS_DAYS):
            continue
        props = dict(f.get("appProperties") or {})
        sections = parse(f.get("description", ""))
        label = f["name"].rsplit(".", 1)[0]
        update, new_props, pushes = False, {}, []

        if os.environ.get("SHOW_PAGE_URL", "").strip() and props.get("site") != "1":
            if page is None and page_error is None:
                try:
                    page = fetch_show_page() or {}
                except (requests.RequestException, ValueError) as e:
                    code = getattr(getattr(e, "response", None), "status_code", None)
                    page_error = f"{type(e).__name__}" + (f" HTTP {code}" if code else "")
                    log(f"Show page unreachable ({page_error})")
            show = (page or {}).get(name_times(f["name"])[0].date())
            if show:
                if show["image"] and not props.get("image"):
                    try:
                        new_props["image"] = save_picture(drive, folder_id, show["image"], label)
                        update = True
                    except (requests.RequestException, RuntimeError) as e:
                        log(f"Picture not saved ({type(e).__name__})")
                if show["keywords"]:
                    sections["keywords"] = show["keywords"]
                    new_props["site"] = "1"
                    update = True
                    pushes.append((show["keywords"], f"Episode: {label}",
                                   show["image"] or ""))
                found = [w for w, ok in (("picture", "image" in new_props),
                                         ("keywords", bool(show["keywords"]))) if ok]
                log("Show page: " + (" and ".join(found) + " found" if found else "nothing new yet"))
            elif page is not None:
                log("Show page: this show is not online yet")

        if now >= end + TRACKS_DELAY and now - end <= timedelta(days=TRACKS_DAYS):
            try:
                tracks = notion_tracks(start, end)
            except requests.RequestException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                log(f"Track list unavailable ({type(e).__name__}"
                    + (f" HTTP {code}" if code else "") + ")")
                tracks = None
            if tracks:
                text = track_lines(tracks, start)
                if text != sections.get("tracks"):
                    sections["tracks"] = text
                    update = True
                    log(f"Track list: {len(tracks)} track(s)")
                    if notify_tracks and props.get("tracks_pushed") != "1":
                        new_props["tracks_pushed"] = "1"
                        pushes.append((text, f"{labels()['tracks']}: {label}", ""))

        if update:
            description = compose(sections)
            drive.update_file(f["id"], description=description, properties=new_props)
            f["description"] = description
            f.setdefault("appProperties", {}).update(new_props)
            changed = True
        for text, title, attach in pushes:
            parts = push_parts(text)
            for i, part in enumerate(parts, 1):
                suffix = f" ({i}/{len(parts)})" if len(parts) > 1 else ""
                push(part, title=title + suffix, tags="framed_picture" if attach else "notes",
                     attach=attach if i == 1 else "")
    return changed


NTFY_LIMIT = 3800  # ntfy turns messages over 4096 bytes into an attachment: stay below


def push_parts(text, limit=NTFY_LIMIT):
    """Splits a text into parts of at most 'limit' UTF-8 bytes, between lines when possible."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line.encode("utf-8")) > limit:  # a single very long line: cut it
            cut = line.encode("utf-8")[:limit].decode("utf-8", "ignore")
            cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
            if current:
                parts.append(current)
                current = ""
            parts.append(cut)
            line = line[len(cut):].lstrip()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-8")) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def linkify(html_text):
    """Escaped text -> HTML with clickable links (used in the feed's HTML notes)."""
    return re.sub(r"(https?://[^\s<]+)", lambda m: f'<a href="{m.group(1)}">↗</a>', html_text)


def item_image(f):
    image = (f.get("appProperties") or {}).get("image")
    return (f'<itunes:image href="{escape(record.download_url(image))}&amp;f=cover.jpg"/>'
            if image else "")
