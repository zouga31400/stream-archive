# Stream recorder

Records an audio stream during a time slot and uploads the MP3 to a private
Google Drive folder. Runs on GitHub Actions, started by an external scheduler.

Nothing specific is stored in this repository: the stream address, the slot and
all credentials are GitHub secrets, and the public log shows no times or names.
Detailed reports (with times) go only to email and ntfy.
With `stream_copy = yes` (`settings.txt`), an MP3 stream is saved as it is and
an AAC stream is saved as is in an `.m4a` file (no quality loss); any other
format is re-encoded to MP3 at `bitrate_kbps`. Recordings older than
`retention_days` (`settings.txt`) are permanently deleted before each recording.

## Secrets (Settings > Secrets and variables > Actions)

| Secret | Content |
|---|---|
| `STREAM_URL` | stream address |
| `RECORD_START`, `RECORD_END` | slot, e.g. `18:00` and `23:30` (an end earlier than the start = next day) |
| `RECORD_DAYS` | optional, `1` = Monday ... `7` = Sunday, e.g. `1,3,5` (default: every day) |
| `RECORD_TZ` | optional, time zone `Region/City` (default: UTC) |
| `DRIVE_FOLDER` | optional, Drive folder name (default: `Recordings`) |
| `PODCAST_TITLE` | optional, podcast feed title (default: `Recordings`) |
| `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN` | Google access, see below |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `REPORT_TO` | optional email report |
| `NTFY_TOPIC` | optional ntfy notifications |

## Starting the recording

The workflow has no schedule of its own. An external scheduler (for example
cron-job.org) calls, about 25 minutes before the slot starts:

```
POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/record.yml/dispatches
Authorization: Bearer <fine-grained token, this repository only, Actions: read and write>
Accept: application/vnd.github+json
Body: {"ref": "main", "inputs": {"minutes": "0"}}
```

By hand: Actions > "Recording" > "Run workflow" (`0` = normal recording,
another number = test of that many minutes).

## Google Drive access (once)

1. At https://console.cloud.google.com, create a project and enable "Google Drive API".
2. Google Auth Platform: user type "External"; fill in **Branding** (no logo);
   in **Audience**, click **"Publish app"** (otherwise the token expires after 7 days).
3. Create an OAuth client "Web application" with the redirect URI
   `https://developers.google.com/oauthplayground`.
4. At https://developers.google.com/oauthplayground: gear icon > "Use your own
   OAuth credentials", scope `https://www.googleapis.com/auth/drive.file`,
   "Authorize APIs", then "Exchange authorization code for tokens"; copy the
   **Refresh token** (usually starts with `1//`).

The `drive.file` scope only gives access to the files created by this script.

## Email report (optional)

Gmail account with 2-step verification; create an app password at
https://myaccount.google.com/apppasswords and set `GMAIL_ADDRESS` and
`GMAIL_APP_PASSWORD` (and optionally `REPORT_TO`).

## Push notifications (optional)

Install the ntfy app, subscribe to a long, hard-to-guess topic and set it in
`NTFY_TOPIC`. `push_each_step` in `settings.txt` chooses one notification per
step or a single summary.

## Private podcast feed (optional)

With `podcast_feed = yes` in `settings.txt`, each recording is shared with
anyone who has its link and listed in `feed.xml`, rebuilt in the Drive folder
after every upload (deleted recordings leave the feed too). The feed address is
sent **by email only**: add it to a podcast app (e.g. Overcast > "Add URL") and
never publish it, since anyone with it can listen. `PODCAST_TITLE` sets the
feed title.

## Troubleshooting

- `... secrets missing`: a secret is missing or misnamed.
- `Google sign-in refused (HTTP 400 invalid_grant)`: invalid refresh token; redo step 4.
- `Google sign-in refused (HTTP 401 invalid_client)`: wrong client ID or secret.
- `Nothing could be recorded (stream unreachable)`: check `STREAM_URL`.
