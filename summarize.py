"""Transcribes the latest recording with Whisper and summarizes it, both on the
runner: by default with an open model run by llama.cpp (free, nothing leaves
GitHub and Drive), or with Claude if summary_engine = claude. The summary becomes the
episode description in the podcast feed and is pushed to ntfy. Transcripts and
summaries never appear in the public log.

GitHub secrets used (besides the Drive ones of record.py):
  TRANSCRIPT_LANGUAGE   optional: language code of the recordings, e.g. en (default: detected)
  ANTHROPIC_API_KEY     only with summary_engine = claude
Settings (settings.txt): whisper_model, whisper_beam_size, summary_min_minutes,
transcription_max_minutes, summary_engine, summary_model_repo, summary_model_file,
summary_context_tokens, summary_deadline_minutes.
"""
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

import anthropic
import requests

import record
from record import CURRENT_NAME, TZ, Drive, load_config, name_prefix, push

WORK_DIR = os.path.join(record.HERE, "summary")
STARTED = time.time()
CLAUDE_MODEL = "claude-sonnet-5-5"
LOCAL_REPO = "bartowski/Mistral-Nemo-Instruct-2407-GGUF"
LOCAL_FILE = "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf"
CHUNK_CHARS = 20000  # about 30 minutes of speech per part, fits a 16k-token context
SYSTEM = """You summarize the transcript of one evening of an audio programme \
(talk segments between pieces of music; the transcript only contains the speech).
Write in the same language as the transcript, as plain text without markdown.
Start with two or three sentences giving the gist of the evening. Then list the
topics, guests and notable moments in order, one per line starting with "- " and
the approximate time in brackets taken from the transcript, e.g. "- [18:40] ...".
Stay factual: only report what is said in the transcript, and when a passage is
unclear, say so briefly rather than guessing. Keep the whole summary under 2500
characters."""


def log(message):
    print(message, flush=True)  # public log: never transcript or summary content


def clock(seconds):
    return f"{int(seconds) // 3600}:{int(seconds) % 3600 // 60:02d}:{int(seconds) % 60:02d}"


def minutes(duration):
    """'H:MM:SS' (appProperties) -> minutes, 0 if unknown."""
    try:
        h, m, s = (int(x) for x in duration.split(":"))
        return h * 60 + m + s / 60
    except (AttributeError, ValueError):
        return 0


def recording_start(name):
    """Start time from a name like '<prefix> 25/12/26 18:00-23:30.mp3', or None."""
    m = CURRENT_NAME.match(name)
    if not m:
        return None
    _, d, mo, yy, h1, m1 = m.groups()[:6]
    return datetime(2000 + int(yy), int(mo), int(d), int(h1), int(m1), tzinfo=TZ)


def pick(files, min_minutes, wanted=""):
    """The file to summarize: the one named in 'wanted' (id), else the newest
    recording long enough that has no summary yet."""
    for f in files:
        if wanted and f["id"] == wanted:
            return f
    if wanted:
        return None
    for f in files:  # newest first
        length = minutes((f.get("appProperties") or {}).get("duration"))
        if not length:  # older uploads have no duration: estimate it from the size
            length = int(f.get("size") or 0) / (64_000 / 8) / 60  # at 64 kb/s at least
        if length >= min_minutes and not f.get("description"):
            return f
    return None


def decode(path):
    """Audio as 16 kHz mono floats, decoded by ffmpeg (Whisper's own decoder depends
    on a library whose versions do not always match)."""
    import numpy as np

    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
                          "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def transcribe(path, start, config):
    """Transcript lines '[HH:MM] text'. Stops early if it would exceed the time budget."""
    from faster_whisper import WhisperModel  # heavy import, only when needed

    name = config.get("whisper_model", "small")
    beam = config.get("whisper_beam_size", "1")
    budget = config.get("transcription_max_minutes", "270")
    budget = int(budget) * 60 if budget.isdigit() else 270 * 60
    language = os.environ.get("TRANSCRIPT_LANGUAGE", "").strip() or None
    model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
    began = time.time()
    audio = decode(path)
    segments, _ = model.transcribe(audio, language=language, vad_filter=True,
                                   beam_size=int(beam) if beam.isdigit() else 1,
                                   condition_on_previous_text=False)
    lines, last, complete = [], 0.0, True
    for seg in segments:
        stamp = (start + timedelta(seconds=seg.start)).strftime("%H:%M") if start else clock(seg.start)
        text = seg.text.strip()
        if text:
            lines.append(f"[{stamp}] {text}")
        last = seg.end
        if time.time() - began > budget:
            complete = False
            break
    log(f"Transcription {'finished' if complete else 'stopped at the time limit'}: "
        f"{len(lines)} segment(s), {clock(time.time() - began)} of computing")
    return lines, complete, last


def language_rule():
    code = os.environ.get("TRANSCRIPT_LANGUAGE", "").strip()
    return (f"Write in the language whose ISO 639-1 code is '{code}'." if code
            else "Write in the same language as the transcript.")


def load_local_model(config):
    """Downloads (Hugging Face, GGUF format) and loads the open model with llama.cpp."""
    from huggingface_hub import hf_hub_download  # heavy imports, only when needed
    from llama_cpp import Llama

    path = hf_hub_download(config.get("summary_model_repo", LOCAL_REPO),
                           config.get("summary_model_file", LOCAL_FILE))
    ctx = config.get("summary_context_tokens", "16384")
    return Llama(model_path=path, n_ctx=int(ctx) if ctx.isdigit() else 16384,
                 n_threads=os.cpu_count() or 4, verbose=False)


def ask_local(llm, instructions, text):
    """One request to the local model. The instructions go in the user message:
    some open-model chat templates have no system role."""
    reply = llm.create_chat_completion(
        messages=[{"role": "user", "content": instructions + "\n\n" + text}],
        temperature=0.2, max_tokens=1200)
    return (reply["choices"][0]["message"].get("content") or "").strip()


def split_parts(lines, size=CHUNK_CHARS):
    """Groups transcript lines into parts of about 'size' characters."""
    parts, current, length = [], [], 0
    for line in lines:
        if current and length + len(line) + 1 > size:
            parts.append(current)
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current:
        parts.append(current)
    return parts


def summarize_local(lines, complete, config):
    """Summary by an open model on the runner: notes per part, then a final summary."""
    llm = load_local_model(config)
    log(f"Local model loaded, {clock(time.time() - STARTED)} elapsed")
    deadline = config.get("summary_deadline_minutes", "320")
    deadline = STARTED + (int(deadline) if deadline.isdigit() else 320) * 60

    parts = split_parts(lines)
    note_rule = ("You take notes on one part of the transcript of an audio programme "
                 "(speech only, between pieces of music). List the topics, guests and "
                 "notable moments in order, one short line each starting with \"- \" and "
                 "the time in brackets from the transcript. Only report what is said. "
                 + language_rule() + " Plain text, at most 12 lines.")
    notes, done = [], 0
    for i, part in enumerate(parts, 1):
        if time.time() > deadline:
            break
        notes.append(ask_local(llm, note_rule, f"Part {i}/{len(parts)}:\n\n" + "\n".join(part)))
        done = i
    log(f"Notes taken on {done}/{len(parts)} part(s), {clock(time.time() - STARTED)} elapsed")
    if not notes:
        raise RuntimeError("No time left to summarize")
    partial = not complete or done < len(parts)
    final_rule = (SYSTEM.replace("the transcript of one evening", "notes taken on one evening")
                  .replace("Write in the same language as the transcript.", language_rule()))
    if partial:
        final_rule += "\nThe notes stop before the end of the recording; say so at the end."
    text = ask_local(llm, final_rule, "Notes:\n\n" + "\n".join(notes))
    if not text:
        raise RuntimeError("Empty summary")
    log(f"Summary written by the local model, {clock(time.time() - STARTED)} elapsed")
    return text


def summarize(lines, complete):
    """Summary by Claude (streaming: the transcript is long)."""
    note = "" if complete else ("\n(The transcript stops before the end of the recording; "
                                "say so at the end of the summary.)")
    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # another model takes over if this one declines
        output_config={"effort": "medium"},
        system=SYSTEM.replace("Write in the same language as the transcript.", language_rule()),
        messages=[{"role": "user",
                   "content": "Transcript:\n\n" + "\n".join(lines) + note}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("Summary declined by the model")
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    if not text:
        raise RuntimeError("Empty summary")
    log(f"Summary written ({message.usage.input_tokens} input tokens, "
        f"{message.usage.output_tokens} output tokens)")
    return text


def main():
    config = load_config()
    engine = config.get("summary_engine", "local").lower()
    if engine == "claude" and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise SystemExit("ANTHROPIC_API_KEY secret missing")
    drive = Drive()
    if not drive.configured():
        raise SystemExit("GDRIVE_* secrets missing")
    drive.login()
    folder_id = drive.folder(os.environ.get("DRIVE_FOLDER", "").strip() or "Recordings")
    min_minutes = config.get("summary_min_minutes", "10")
    min_minutes = int(min_minutes) if min_minutes.isdigit() else 10
    target = pick(drive.list_audio(folder_id), min_minutes,
                  os.environ.get("SUMMARY_FILE_ID", "").strip())
    if not target:
        log("No recording to summarize")
        return
    log("Recording found, downloading")
    os.makedirs(WORK_DIR, exist_ok=True)
    path = os.path.join(WORK_DIR, "audio" + os.path.splitext(target["name"])[1])
    drive.download(target["id"], path)

    lines, complete, _ = transcribe(path, recording_start(target["name"]), config)
    if not lines:
        log("No speech found, nothing to summarize")
        return
    if engine == "claude":
        summary = summarize(lines, complete)
    else:
        summary = summarize_local(lines, complete, config)

    drive.login()  # the access token expires after one hour
    drive.set_description(target["id"], summary)
    title = os.environ.get("PODCAST_TITLE", "").strip() or "Recordings"
    drive.update_feed(folder_id, title)
    log("Episode description and podcast feed updated")
    label = target["name"].rsplit(".", 1)[0]
    if push(summary, title=f"Summary: {label}", tags="memo"):
        log("Summary pushed to ntfy")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except anthropic.APIStatusError as e:
        log(f"Failed: Claude API error (HTTP {e.status_code})")
        push(f"Summary failed: Claude API error (HTTP {e.status_code})",
             title="Summary FAILED", priority="high", tags="warning")
        sys.exit(1)
    except Exception as e:  # any other failure: short message, and a notification
        detail = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        log(f"Failed: {detail}")
        push(f"Summary failed: {detail}", title="Summary FAILED", priority="high", tags="warning")
        if isinstance(e, (RuntimeError, requests.RequestException, anthropic.APIError)):
            sys.exit(1)  # no traceback: network errors carry addresses (Drive links)
        raise  # code errors: the traceback only shows file names and lines
