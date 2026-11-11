"""Records an audio stream during a time slot and uploads the MP3 file to
Google Drive.

GitHub secrets used:
  STREAM_URL            stream address
  RECORD_START          slot start, e.g. 18:00
  RECORD_END            slot end, e.g. 23:30 (earlier than the start = next day)
  RECORD_DAYS           optional: days, 1 = Monday ... 7 = Sunday, e.g. 1,3,5 (default: every day)
  RECORD_TZ             optional: time zone, e.g. Region/City (default: UTC)
  DRIVE_FOLDER          optional: Drive folder name (default: Recordings)
  PODCAST_TITLE         optional: podcast feed title (default: Recordings)
  GDRIVE_CLIENT_ID      Google OAuth client ID
  GDRIVE_CLIENT_SECRET  OAuth client secret
  GDRIVE_REFRESH_TOKEN  refresh token (drive.file scope)
  GMAIL_ADDRESS         optional: Gmail address sending the report
  GMAIL_APP_PASSWORD    optional: Gmail app password
  REPORT_TO             optional: report recipient (defaults to GMAIL_ADDRESS)
  NTFY_TOPIC            optional: ntfy topic for a push notification
Non-secret settings: settings.txt.
"""
import glob
import json
import os
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from email.utils import format_datetime
from xml.sax.saxutils import escape
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo(os.environ.get("RECORD_TZ", "").strip() or "UTC")
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "settings.txt")
WORK_DIR = os.path.join(HERE, "recording")
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
CHUNK = 32 * 256 * 1024  # 8 MB, Google requires a multiple of 256 KB
EARLY_MARGIN = timedelta(minutes=30)  # earlier than this: nothing to do yet
# GitHub stops a job after 6 h (355 min in the workflow): keep a margin
# to assemble and upload the file.
MAX_RUN = timedelta(minutes=330)


LOG_LINES = []
EMAIL_ONLY = []  # private lines sent by email only (never to the public log or ntfy)
FEED_NAME = "feed.xml"
PUSH_EACH_STEP = False  # set from settings.txt (push_each_step)
PUSH_REPEAT_SECONDS = 600  # the same message is pushed at most once per 10 minutes
_last_push = {}


def push(text, title="Recording", priority="default", tags="headphones"):
    """Sends one ntfy notification. Never raises; returns True if sent."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    try:
        r = requests.post(f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                          headers={"Title": title, "Priority": priority, "Tags": tags},
                          timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException:
        return False


def log(*args, private=None, alert=False, quiet=False):
    """Public message in the GitHub log (the repository is public: no times, no
    names); 'private' is the detailed version sent by email and ntfy only."""
    message = " ".join(map(str, args))
    print(message, flush=True)
    if private:
        message = private
    line = f"{datetime.now(TZ):%H:%M:%S} {message}"
    LOG_LINES.append(line)
    if PUSH_EACH_STEP:
        now = time.time()
        if now - _last_push.get(message, 0) >= PUSH_REPEAT_SECONDS:
            _last_push[message] = now
            if alert:
                push(line, "Recording FAILED", "high", "warning")
            elif quiet:
                push(line, priority="low")
            else:
                push(line)


def send_push(ok):
    """End-of-run summary through ntfy (only when steps are not pushed live)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic or PUSH_EACH_STEP:
        return
    try:
        r = requests.post(f"https://ntfy.sh/{topic}",
                          data="\n".join(LOG_LINES).encode("utf-8"),
                          headers={"Title": f"Recording {'OK' if ok else 'FAILED'}",
                                   "Priority": "default" if ok else "high",
                                   "Tags": "headphones" if ok else "warning"},
                          timeout=30)
        r.raise_for_status()
        print("Push notification sent", flush=True)
    except requests.RequestException as e:
        print(f"Push notification failed ({type(e).__name__})", flush=True)


def send_report(ok):
    """Emails the day's log lines. Does nothing if the Gmail secrets are not set."""
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = os.environ.get("REPORT_TO", "").strip() or sender
    if not (sender and password):
        return
    msg = EmailMessage()
    msg["Subject"] = (f"Recording {'OK' if ok else 'FAILED'} - "
                      f"{datetime.now(TZ):%Y-%m-%d}")
    msg["From"] = sender
    msg["To"] = to
    msg.set_content("\n".join(LOG_LINES + [""] + EMAIL_ONLY) + "\n")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(sender, password)
            smtp.send_message(msg)
        print("Report emailed", flush=True)
    except (smtplib.SMTPException, OSError) as e:
        print(f"Report email failed ({type(e).__name__})", flush=True)


def load_config():
    config = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    config[key.strip().lower()] = value.strip()
    except FileNotFoundError:
        pass
    return config


def parse_hour(text, default):
    """'18:00', '18h', '18h30' -> (hour, minute)."""
    text = (text or default).lower().replace("h", ":").strip(":")
    hour, _, minute = text.partition(":")
    return int(hour), int(minute or 0)


def parse_days(text):
    """'6-7' or '1,3,5' (1 = Monday ... 7 = Sunday) -> set of days; empty = every day."""
    days = set()
    for part in (text or "1-7").replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            days.update(range(int(a), int(b) + 1))
        elif part:
            days.add(int(part))
    return days


def compute_window():
    """Returns the recording (start, end) in the configured time zone, or exits."""
    now = datetime.now(TZ)
    test = os.environ.get("TEST_MINUTES", "").strip()
    if test and test != "0":  # "0" = normal recording
        minutes = int(test) if test.isdigit() and int(test) > 0 else 5
        log(f"Test mode: {minutes} minutes starting now")
        return now, now + timedelta(minutes=minutes)

    start_text = os.environ.get("RECORD_START", "").strip()
    end_text = os.environ.get("RECORD_END", "").strip()
    if not (start_text and end_text):
        raise SystemExit("RECORD_START / RECORD_END secrets missing")
    h1, m1 = parse_hour(start_text, "")
    h2, m2 = parse_hour(end_text, "")
    start = now.replace(hour=h1, minute=m1, second=0, microsecond=0)
    end = now.replace(hour=h2, minute=m2, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    if now < start - EARLY_MARGIN and now < end - timedelta(days=1):
        start, end = start - timedelta(days=1), end - timedelta(days=1)  # still in yesterday's slot

    if start.isoweekday() not in parse_days(os.environ.get("RECORD_DAYS", "").strip()):
        log("Not a recording day, nothing to do.")
        sys.exit(0)
    if now < start - EARLY_MARGIN:
        log("Too early, nothing to do.")
        sys.exit(0)
    if now >= end - timedelta(minutes=5):
        log("Slot over or already recorded, nothing to do.")
        sys.exit(0)
    return start, end


def probe_stream(stream_url):
    """Codec and bit rate (kb/s) of the stream's audio, or (None, None)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-rw_timeout", "20000000", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,bit_rate:format=bit_rate", "-of", "json",
             stream_url], capture_output=True, text=True, timeout=60).stdout
        data = json.loads(out or "{}")
    except (subprocess.SubprocessError, ValueError):
        return None, None
    streams = data.get("streams") or [{}]
    codec = streams[0].get("codec_name")
    rate = streams[0].get("bit_rate") or (data.get("format") or {}).get("bit_rate")
    return codec, (int(rate) // 1000 if str(rate or "").isdigit() else None)


def choose_format(stream_url, copy, bitrate):
    """How to record: copy MP3/AAC streams as they are, otherwise re-encode to MP3."""
    codec, rate = probe_stream(stream_url) if copy else (None, None)
    detail = f"{codec or 'unknown'}{f' {rate} kb/s' if rate else ''}"
    if codec == "mp3":
        fmt = {"seg": ".mp3", "out": ".mp3", "mime": "audio/mpeg", "args": ["-c:a", "copy"]}
        log("Stream copied as is", private=f"Stream format {detail}: copied as is")
    elif codec == "aac":
        # Raw AAC segments survive a cut; they are put into an .m4a file at the end.
        fmt = {"seg": ".aac", "out": ".m4a", "mime": "audio/mp4",
               "args": ["-c:a", "copy", "-f", "adts"]}
        log("Stream copied as is", private=f"Stream format {detail}: copied as is")
    else:
        fmt = {"seg": ".mp3", "out": ".mp3", "mime": "audio/mpeg",
               "args": ["-ac", "2", "-c:a", "libmp3lame", "-b:a", f"{bitrate}k"]}
        log("Stream re-encoded to MP3",
            private=f"Stream format {detail}: re-encoded to MP3 {bitrate} kb/s")
    return fmt


def record(stream_url, start, end, fmt):
    """Records until 'end'; restarts ffmpeg if the stream drops. Returns the segments."""
    os.makedirs(WORK_DIR, exist_ok=True)
    wait = (start - datetime.now(TZ)).total_seconds()
    if wait > 0:
        time.sleep(wait)
    log("Recording", private=f"Recording until {end:%H:%M}")

    segments, failures = [], 0
    while True:
        remaining = (end - datetime.now(TZ)).total_seconds()
        if remaining < 5:
            break
        path = os.path.join(WORK_DIR, f"segment_{len(segments):03d}{fmt['seg']}")
        if os.path.exists(path):
            os.remove(path)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
                 "-reconnect", "1", "-reconnect_streamed", "1",
                 "-reconnect_on_network_error", "1", "-reconnect_at_eof", "1",
                 "-reconnect_delay_max", "30",
                 "-rw_timeout", "30000000",
                 "-i", stream_url, "-t", str(int(remaining)),
                 "-map", "0:a:0", *fmt["args"], path],
                timeout=remaining + 180,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,  # never print the stream address
            )
        except subprocess.TimeoutExpired:
            log("ffmpeg stuck, restarting")
        if os.path.exists(path) and os.path.getsize(path) > 50_000:
            segments.append(path)
            failures = 0
        else:
            failures += 1
            if os.path.exists(path):
                os.remove(path)
        if (end - datetime.now(TZ)).total_seconds() >= 5:
            delay = 2 if failures < 5 else 10 if failures < 30 else 60
            log("Stream interrupted, retrying", private=f"Stream interrupted, retrying in {delay} s",
                quiet=True)
            time.sleep(delay)
    log(f"Recording finished ({len(segments)} segment(s))")
    return segments


def download_url(file_id):
    """Direct download link of a Drive file shared with anyone (works for large files)."""
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


def audio_duration(path):
    """Duration as H:MM:SS for the podcast feed, or '' if unknown."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True,
                             timeout=60).stdout.strip()
        seconds = int(float(out))
    except (subprocess.SubprocessError, ValueError):
        return ""
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def assemble(segments, output):
    m4a = output.endswith(".m4a")
    if len(segments) == 1 and not m4a:
        os.replace(segments[0], output)
        return
    listing = os.path.join(WORK_DIR, "list.txt")
    with open(listing, "w", encoding="utf-8") as f:
        for path in segments:
            f.write(f"file '{os.path.basename(path)}'\n")
    extra = ["-bsf:a", "aac_adtstoasc", "-movflags", "+faststart"] if m4a else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-f", "concat",
                    "-safe", "0", "-i", listing, "-c", "copy", *extra, output], check=True)


class Drive:
    def __init__(self):
        self.client_id = os.environ.get("GDRIVE_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "").strip()
        self.refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()
        self.token = None

    def configured(self):
        return all((self.client_id, self.client_secret, self.refresh_token))

    def login(self):
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"}, timeout=30)
        if r.status_code != 200:
            error = r.json().get("error", "") if r.headers.get("content-type", "").startswith(
                "application/json") else ""
            raise RuntimeError(f"Google sign-in refused (HTTP {r.status_code} {error})")
        self.token = r.json()["access_token"]

    def headers(self, **extra):
        return {"Authorization": f"Bearer {self.token}", **extra}

    def find_folder(self, name):
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = (f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
             "and trashed = false")
        r = requests.get(DRIVE_FILES, params={"q": q, "fields": "files(id)"},
                         headers=self.headers(), timeout=30)
        r.raise_for_status()
        files = r.json().get("files", [])
        return files[0]["id"] if files else None

    def folder(self, name, old_names=()):
        """Finds or creates folder 'name' at the Drive root; renames an old folder if found."""
        folder_id = self.find_folder(name)
        if folder_id:
            return folder_id
        for old in old_names:
            folder_id = self.find_folder(old)
            if folder_id:
                r = requests.patch(f"{DRIVE_FILES}/{folder_id}", json={"name": name},
                                   headers=self.headers(), timeout=30)
                r.raise_for_status()
                log("Drive folder renamed")
                return folder_id
        r = requests.post(DRIVE_FILES, json={
            "name": name, "mimeType": "application/vnd.google-apps.folder"},
            headers=self.headers(), timeout=30)
        r.raise_for_status()
        log("Drive folder created")
        return r.json()["id"]

    def purge(self, folder_id, days):
        """Permanently deletes the folder's MP3 files created more than 'days' days ago.

        Permanently (not to the bin): a file in the bin still takes up space.
        The drive.file scope limits access to the files created by this script.
        """
        limit = (datetime.now(TZ) - timedelta(days=days)).astimezone(ZoneInfo("UTC"))
        q = (f"'{folder_id}' in parents and (mimeType = 'audio/mpeg' or mimeType = 'audio/mp4') and trashed = false "
             f"and createdTime < '{limit:%Y-%m-%dT%H:%M:%S}'")
        old, page = [], None
        while True:
            params = {"q": q, "fields": "nextPageToken, files(id, size)", "pageSize": 100}
            if page:
                params["pageToken"] = page
            r = requests.get(DRIVE_FILES, params=params, headers=self.headers(), timeout=30)
            r.raise_for_status()
            data = r.json()
            old.extend(data.get("files", []))
            page = data.get("nextPageToken")
            if not page:
                break
        freed = 0
        for f in old:
            r = requests.delete(f"{DRIVE_FILES}/{f['id']}", headers=self.headers(), timeout=30)
            if r.status_code in (200, 204, 404):
                freed += int(f.get("size") or 0)
        if old:
            log(f"{len(old)} recording(s) older than {days} days deleted, "
                f"{freed / 1_000_000:.0f} MB freed")

    def share(self, file_id):
        """Makes the file readable by anyone with the link (needed by podcast apps)."""
        r = requests.post(f"{DRIVE_FILES}/{file_id}/permissions",
                          json={"role": "reader", "type": "anyone"},
                          headers=self.headers(), timeout=30)
        r.raise_for_status()
        r = requests.patch(f"{DRIVE_FILES}/{file_id}", json={"appProperties": {"shared": "1"}},
                           headers=self.headers(), timeout=30)
        r.raise_for_status()

    def list_audio(self, folder_id):
        q = f"'{folder_id}' in parents and (mimeType = 'audio/mpeg' or mimeType = 'audio/mp4') and trashed = false"
        files, page = [], None
        while True:
            params = {"q": q, "pageSize": 100, "orderBy": "createdTime desc",
                      "fields": "nextPageToken, files(id, name, size, mimeType, createdTime, appProperties)"}
            if page:
                params["pageToken"] = page
            r = requests.get(DRIVE_FILES, params=params, headers=self.headers(), timeout=30)
            r.raise_for_status()
            data = r.json()
            files.extend(data.get("files", []))
            page = data.get("nextPageToken")
            if not page:
                return files

    def update_feed(self, folder_id, title):
        """Rebuilds the podcast feed (feed.xml in the folder) and returns its URL."""
        items = []
        for f in self.list_audio(folder_id):
            props = f.get("appProperties") or {}
            if props.get("shared") != "1":
                self.share(f["id"])
            created = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00"))
            duration = props.get("duration", "")
            items.append(
                "<item>"
                f"<title>{escape(f['name'].rsplit('.', 1)[0])}</title>"
                f"<guid isPermaLink=\"false\">{escape(f['id'])}</guid>"
                f"<pubDate>{format_datetime(created)}</pubDate>"
                f"<enclosure url=\"{escape(download_url(f['id']))}\" length=\"{f.get('size', 0)}\" "
                f"type=\"{escape(f.get('mimeType', 'audio/mpeg'))}\"/>"
                + (f"<itunes:duration>{escape(duration)}</itunes:duration>" if duration else "")
                + "</item>")
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
               f"<channel><title>{escape(title)}</title><description>{escape(title)}</description>"
               "<link>https://drive.google.com/</link><itunes:block>Yes</itunes:block>"
               f"{''.join(items)}</channel></rss>\n").encode("utf-8")

        q = f"'{folder_id}' in parents and name = '{FEED_NAME}' and trashed = false"
        r = requests.get(DRIVE_FILES, params={"q": q, "fields": "files(id)"},
                         headers=self.headers(), timeout=30)
        r.raise_for_status()
        found = r.json().get("files", [])
        if found:
            feed_id = found[0]["id"]
            r = requests.patch(f"{DRIVE_UPLOAD}/{feed_id}", params={"uploadType": "media"},
                               data=xml, headers=self.headers(**{"Content-Type": "application/rss+xml"}),
                               timeout=60)
            r.raise_for_status()
        else:
            meta = {"name": FEED_NAME, "parents": [folder_id], "mimeType": "application/rss+xml"}
            r = requests.post(DRIVE_UPLOAD, params={"uploadType": "multipart"},
                              files={"metadata": (None, json.dumps(meta), "application/json"),
                                     "file": (FEED_NAME, xml, "application/rss+xml")},
                              headers=self.headers(), timeout=60)
            r.raise_for_status()
            feed_id = r.json()["id"]
            self.share(feed_id)
        return download_url(feed_id), len(items)

    def upload(self, path, name, folder_id, properties=None, mime="audio/mpeg"):
        size = os.path.getsize(path)
        r = requests.post(DRIVE_UPLOAD, params={"uploadType": "resumable"},
                          json={"name": name, "parents": [folder_id],
                                "appProperties": properties or {}},
                          headers=self.headers(**{"X-Upload-Content-Type": mime,
                                                  "X-Upload-Content-Length": str(size)}),
                          timeout=30)
        r.raise_for_status()
        session = r.headers["Location"]
        sent, errors = 0, 0
        with open(path, "rb") as f:
            while sent < size:
                f.seek(sent)
                data = f.read(CHUNK)
                last = sent + len(data) - 1
                try:
                    r = requests.put(session, data=data, timeout=120, headers={
                        "Content-Range": f"bytes {sent}-{last}/{size}"})
                except requests.RequestException:
                    r = None
                if r is not None and r.status_code in (200, 201):
                    return r.json().get("id")
                if r is not None and r.status_code == 308:
                    rng = r.headers.get("Range")
                    sent = int(rng.split("-")[1]) + 1 if rng else 0
                    errors = 0
                    continue
                if r is not None and r.status_code == 401:
                    self.login()
                errors += 1
                if errors > 8:
                    raise RuntimeError("Upload to Google Drive failed after several attempts")
                time.sleep(2 ** min(errors, 6))
                # Ask Google how far the upload got before resuming.
                try:
                    r = requests.put(session, timeout=30,
                                     headers={"Content-Range": f"bytes */{size}"})
                    if r.status_code in (200, 201):
                        return r.json().get("id")
                    if r.status_code == 308:
                        rng = r.headers.get("Range")
                        sent = int(rng.split("-")[1]) + 1 if rng else 0
                except requests.RequestException:
                    pass
        raise RuntimeError("Upload to Google Drive incomplete")


def main():
    global PUSH_EACH_STEP
    config = load_config()
    PUSH_EACH_STEP = config.get("push_each_step", "no").lower() in ("yes", "true", "1", "on")
    log("Job started")
    stream_url = os.environ.get("STREAM_URL", "").strip()
    if not stream_url:
        raise SystemExit("STREAM_URL secret missing or empty")
    drive = Drive()
    if not drive.configured():
        raise SystemExit("GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET / GDRIVE_REFRESH_TOKEN secrets missing")

    launched = datetime.now(TZ)
    start, end = compute_window()
    if end > launched + MAX_RUN:
        end = launched + MAX_RUN
        log("Slot too long for GitHub (6 h maximum), stopping early",
            private=f"Slot too long for GitHub (6 h maximum), stopping early at {end:%H:%M}")
    # Check Drive access before spending hours recording.
    drive.login()
    folder_name = os.environ.get("DRIVE_FOLDER", "").strip() or "Recordings"
    folder_id = drive.folder(folder_name)
    log("Google Drive OK")
    keep = config.get("retention_days", "14")
    if keep.isdigit() and int(keep) > 0:
        try:
            drive.purge(folder_id, int(keep))
        except requests.RequestException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            log(f"Could not clean up old recordings ({type(e).__name__}"
                + (f" HTTP {code}" if code else "") + "), continuing")

    bitrate = config.get("bitrate_kbps", "128")
    bitrate = bitrate if bitrate.isdigit() and 32 <= int(bitrate) <= 320 else "128"
    for old in glob.glob(os.path.join(WORK_DIR, "*")):
        os.remove(old)
    begin = max(start, datetime.now(TZ))
    wait = (begin - datetime.now(TZ)).total_seconds()
    if wait > 120:  # probe the stream shortly before the start, not during the wait
        log("Waiting for start",
            private=f"Waiting for start ({start:%H:%M}), {int(wait // 60)} min")
        time.sleep(wait - 60)
    copy = config.get("stream_copy", "yes").lower() in ("yes", "true", "1", "on")
    fmt = choose_format(stream_url, copy, bitrate)
    segments = record(stream_url, begin, end, fmt)
    if not segments:
        raise SystemExit("Nothing could be recorded (stream unreachable)")

    name = f"Recording {start:%Y-%m-%d} {start:%H%M}-{end:%H%M}{fmt['out']}"
    output = os.path.join(WORK_DIR, name)
    assemble(segments, output)
    size_mb = os.path.getsize(output) / 1_000_000
    log(f"File ready ({size_mb:.0f} MB)", private=f"File ready: {name} ({size_mb:.0f} MB)")

    duration = audio_duration(output)
    drive.login()  # the access token expires after one hour
    file_id = drive.upload(output, name, folder_id, {"duration": duration} if duration else None,
                           fmt["mime"])
    log("Uploaded to Google Drive" if file_id else "Upload finished")

    if config.get("podcast_feed", "no").lower() in ("yes", "true", "1", "on"):
        try:
            title = os.environ.get("PODCAST_TITLE", "").strip() or "Recordings"
            feed_url, count = drive.update_feed(folder_id, title)
            log("Podcast feed updated", private=f"Podcast feed updated ({count} episode(s))")
            EMAIL_ONLY.append(f"Podcast feed (keep it private): {feed_url}")
        except requests.RequestException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            log(f"Podcast feed update failed ({type(e).__name__}"
                + (f" HTTP {code}" if code else "") + ")")


if __name__ == "__main__":
    ok = False
    try:
        main()
        ok = True
    except SystemExit as e:
        if e.code in (None, 0):
            sys.exit(0)  # nothing to do today: no email
        log("Failed:", e.code, alert=True)
    except (RuntimeError, requests.RequestException, subprocess.CalledProcessError) as e:
        # Short message: the repository is public, no address must appear.
        code = getattr(getattr(e, "response", None), "status_code", None)
        detail = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        log("Failed:", detail + (f" (HTTP {code})" if code else ""), alert=True)
    except Exception as e:
        log("Failed:", type(e).__name__, alert=True)
    send_report(ok)
    send_push(ok)
    sys.exit(0 if ok else 1)
