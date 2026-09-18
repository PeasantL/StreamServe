# StreamServe

A small self-hosted video catalogue and streaming server. It scans a folder of
videos, generates thumbnails with ffmpeg, transcodes WebM to MP4 so browsers and
iOS can play it, and serves everything over HTTP with byte-range support so you
can seek.

> **Security note.** The IP allowlist is a network filter, not authentication.
> Anyone who can reach the port from an allowed address can download, rename and
> delete videos. Set `auth_password` to require a password as well — see
> [Authentication](#authentication). Run it on a LAN or behind a VPN. Do not
> expose it to the internet.

## Requirements

- Python 3.11+ and `ffmpeg` (which also provides `ffprobe`), or Docker.
- On Debian/Ubuntu: `sudo apt-get install ffmpeg`

## Running with Docker

```bash
cp .env.example .env      # then edit PARENT_DIRECTORY, VIDEO_FOLDER, ALLOWED_IPS
docker compose up -d
```

Open <http://localhost:6969>.

`PARENT_DIRECTORY` is the directory that *contains* your video folders; the
buttons across the top of the page switch between its direct subdirectories.
Each page keeps its folder in `?folder=`, so switching in one tab does not move
an open player, download, or edit in another tab to a different library.

## Running directly

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config_example.json config.json    # then edit video_dir and allowed_ips
python main.py
```

## Configuration

Values resolve highest-precedence first: environment variable, then
`config.json`, then the built-in default.

| Key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `video_dir` | `VIDEO_DIR` | `videos` | Folder to open on startup. |
| `thumbnail_dir` | `THUMBNAIL_DIR` | `thumbnails` | Where generated thumbnails are written. |
| `db_file` | `DB_FILE` | `video_db.json` | Catalogue database. |
| `allowed_ips` | `ALLOWED_IPS` | `127.0.0.1,::1` | Addresses or CIDR blocks allowed to connect. |
| `trusted_proxies` | `TRUSTED_PROXIES` | *(empty)* | Proxies whose `X-Forwarded-For` may be believed. |
| `ffmpeg_timeout` | `FFMPEG_TIMEOUT` | `30` | Seconds allowed for a thumbnail or probe. |
| `convert_timeout` | `CONVERT_TIMEOUT` | `3600` | Seconds allowed for one WebM→MP4 transcode. |
| `thumbnail_width` | `THUMBNAIL_WIDTH` | `320` | Maximum thumbnail width in pixels. |
| `page_size` | `PAGE_SIZE` | `100` | Videos per page in the grid; `0` shows them all. |
| `video_encoder` | `VIDEO_ENCODER` | `libx264` | Encoder used when a file must be re-encoded. |
| `hwaccel` | `HWACCEL` | *(empty)* | ffmpeg hardware decoder (`cuda`, `qsv`, `vaapi`, `videotoolbox`). |
| `max_download_bytes` | `MAX_DOWNLOAD_BYTES` | `8589934592` | Ceiling on a single remote download. |
| `download_timeout` | `DOWNLOAD_TIMEOUT` | `30` | Seconds allowed for a remote request. |
| `import_limit` | `IMPORT_LIMIT` | `20` | Most posts one booru tag-search import will fetch. |
| `allow_duplicates` | `ALLOW_DUPLICATES` | `false` | Allow the same video to be added to a folder twice. |
| `auth_password` | `AUTH_PASSWORD` | *(empty)* | Shared password. Empty disables authentication. |
| `session_secret` | `SESSION_SECRET` | *(empty)* | Key signing session cookies; empty generates one per process. |
| `session_ttl` | `SESSION_TTL` | `2592000` | Session lifetime in seconds (30 days). |
| `session_cookie_secure` | `SESSION_COOKIE_SECURE` | `false` | Set true only behind HTTPS. |
| `gelbooru_api_key` | `GELBOORU_API_KEY` | *(empty)* | Optional Gelbooru API key; see Booru post pages. |
| `gelbooru_user_id` | `GELBOORU_USER_ID` | *(empty)* | Optional Gelbooru user id, paired with the key. |
| `host` / `port` | `HOST` / `PORT` | `0.0.0.0` / `6969` | Listen address. |
| — | `PARENT_DIR` | parent of `video_dir` | Directory whose subfolders are offered as switchable libraries. |
| — | `CONFIG_FILE` | `config.json` | Path to the config file itself. |

`config.json` is never written back to. The last selected folder is saved in
the database file and used when a URL omits `folder`.

### About `trusted_proxies`

`X-Forwarded-For` is set by the client unless a proxy you control overwrites it.
Leave `trusted_proxies` empty and the header is ignored entirely. Set it to your
reverse proxy's address and the last hop that proxy appended is used as the
client address.

## Browsing

The grid is paged at `page_size` videos and each tile shows the duration,
resolution and view count over its thumbnail. Opening the player counts one
view. The toolbar sorts by newest or most viewed, and filters two ways:

- **Search** (`?q=`) matches a substring of the title, description or any tag.
- **Tag chips** (`?tag=`, repeatable) narrow *conjunctively* — a second tag
  shows fewer videos, not more. Tags imported from a booru make this useful
  immediately; see [Booru post pages](#booru-post-pages).

Both are query parameters, so a filtered view is a URL you can bookmark or
share. Sorting, paging and opening a video all preserve the active filter.

**Rescan** re-reads the folder shown on the page, for files copied in while the
server was running. If a scan is already running, another folder's scan waits;
a repeat request for the same folder uses its existing task. The tray in the
corner shows every job this server is running — downloads, imports and scans —
including ones started in another tab.

### Playing

Previous and Next step through videos in the order the grid was showing, under
the same filter. Keyboard shortcuts:

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| <kbd>space</kbd> / <kbd>k</kbd> | play / pause | <kbd>m</kbd> | mute |
| <kbd>←</kbd> <kbd>→</kbd> | seek ±5s | <kbd>f</kbd> | fullscreen |
| <kbd>↑</kbd> <kbd>↓</kbd> | volume | <kbd>n</kbd> / <kbd>p</kbd> | next / previous |
| <kbd>esc</kbd> | back to the grid | | |

**Edit** changes the title, description and tags together. Earlier this was a
rename prompt that sent an empty description and tag list along with the new
title, silently destroying both.

### Subtitles

A `.srt` or `.vtt` file sitting next to a video, with the same name, is picked
up automatically. Browsers accept only WebVTT, so SubRip is converted when
served — the header and the timecode separator, nothing else. A `.vtt` wins
over a `.srt` when both are present.

## Formats

The catalogue serves MP4. Anything else in the library folder — `.webm`,
`.mkv`, `.mov`, `.avi`, `.m4v`, `.flv`, `.wmv`, `.ts`, `.mpg`, `.mpeg`,
`.ogv` — is converted on scan and the untouched original is moved into
`original_webm/` beside it.

Conversion decides per stream. A source already holding H.264 video is
**remuxed**, not re-encoded, which takes seconds rather than minutes; an MKV
with H.264 video and Opus audio copies the video and re-encodes only the
audio. Set `video_encoder` and `hwaccel` if the host has a hardware encoder
worth using.

## Remote downloads

`POST /api/download` fetches a video URL. Only `http` and `https` are accepted,
hostnames resolving to private, loopback or link-local addresses are refused
(including across redirects), and the download stops at `max_download_bytes`.

A video already in the folder is not fetched twice. A repeated file URL or
booru post is refused immediately; a file that turns out to be byte-identical
to one already stored is dropped after downloading but before transcoding.
Set `allow_duplicates` to turn this off.

### Booru post pages

The same box also accepts a booru post *page*, and fetches the original file
that post holds — not the resized "sample" the page displays:

| Site | URL to paste |
| --- | --- |
| Danbooru | `https://danbooru.donmai.us/posts/12345` |
| Gelbooru | `https://gelbooru.com/index.php?page=post&s=view&id=12345` |
| rule34.us | `https://rule34.us/index.php?r=posts/view&id=12345` |
| rule34.xxx | `https://rule34.xxx/index.php?page=post&s=view&id=12345` |
| Safebooru | `https://safebooru.org/index.php?page=post&s=view&id=12345` |

Danbooru and the gelbooru-family sites are read through their JSON APIs; where
the API is unavailable the post page itself is parsed for the original link,
which is the only route rule34.us offers. The entry is titled `gelbooru 12345`
after the post it came from, because boorus name their files by md5 hash, and
the post URL is kept as the description.

**Only video posts can be added.** This is a video catalogue: an `.mp4` or
`.webm` post is stored, and anything else is refused with a message naming the
type it found, rather than downloaded and then discarded.

Resolution is held to the same rules as any other download, plus one more: a
file URL must live on the booru's own domain. Post pages carry user-written
comments, and a comment containing a plausible-looking media URL must not be
able to redirect the fetch somewhere else.

A post's own tags come across with it, so an imported video is searchable and
filterable straight away. Posts resolved by page scrape rather than by API
arrive untagged.

Gelbooru requires credentials on its JSON API. Set `gelbooru_api_key` and
`gelbooru_user_id` (or `GELBOORU_API_KEY` / `GELBOORU_USER_ID`) from *Account
→ Options → API Access Credentials* to use it; without them gelbooru falls
back to reading the post page, which usually still works.

### Booru tag searches

The same box also takes a tag-search URL and imports the video posts it
matches, up to `import_limit`:

| Site | URL to paste |
| --- | --- |
| Danbooru | `https://danbooru.donmai.us/posts?tags=blue_hair+video` |
| Gelbooru | `https://gelbooru.com/index.php?page=post&s=list&tags=animated` |
| rule34.xxx | `https://rule34.xxx/index.php?page=post&s=list&tags=webm` |
| Safebooru | `https://safebooru.org/index.php?page=post&s=list&tags=cat` |

Image posts are filtered out at search time, so the job fetches only what it
will keep. Posts are fetched one at a time — a booru will rate-limit a client
that opens a dozen connections at once. One post failing does not abandon the
rest; the job reports how many were added, already present and failed.

rule34.us has no search API, so only its single post pages work.

Gelbooru's search API needs the credentials above; without them it answers an
unauthenticated search with an empty result rather than an error, and the job
says so.

## Authentication

Off by default: leave `auth_password` empty and the IP allowlist is the only
gate, exactly as before. Set it and every page and API call needs a session.

```bash
AUTH_PASSWORD='a long passphrase'
SESSION_SECRET='any long random string'   # else sessions end at each restart
```

This layers *behind* the allowlist rather than replacing it — a blocked address
never reaches the password prompt. It is what makes reaching the server over a
VPN or a tunnel defensible, where every peer shares one source address.

The session is a signed cookie carrying an expiry, `HttpOnly` and
`SameSite=Lax`. Failed attempts are throttled per address. `/healthz` stays
open so the container health check keeps working. Set `session_cookie_secure`
only behind HTTPS — a `Secure` cookie is never sent over plain http, and the
login would silently never take.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
pytest
```

Branding lives in `static/`: `icon-32.png` is the tab favicon and
`icon-180.png` the icon iOS and Android use when the page is saved to a home
screen. `/static/` is served without a session so the login page can show it.

Tests cover the Range parser, the IP allowlist and forwarded-header handling,
path containment, the SSRF guard, booru URL and tag-search resolution, partial
metadata updates, search and tag filtering, pagination, media probing, the
remux decision, duplicate detection, session authentication, the job registry,
player neighbours, subtitle conversion, and concurrent database writes. They do
not require ffmpeg or a network connection.

## Licence

No licence has been chosen yet, which means default copyright applies and others
have no right to use or redistribute this code. Add a `LICENSE` file if you want
to change that.
