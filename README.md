# terabox.py

Single-file TeraBox link bypass + downloader CLI.

## Requirements

- `pip install aiohttp`
- Optional: `aria2c` (`apt install aria2`) for multi-connection downloads.
- **A proxy is mandatory for downloading.** TeraBox throttles residential IPs
  to a few KB/s — running this script on a residential IP is pointless.
  There is no `--proxy` option; route the whole process through one, e.g.:
  `proxychains python terabox.py download <url>`
- No proxy? Don't use this tool — just use JDownloader or similar.

## Usage

```bash
python terabox.py bypass <url>...        # resolve to direct links (--json, --quiet)
python terabox.py download <url>...      # download via aria2c, fail if < 1 MB/s
python terabox.py check-cookies FILE     # validate cookie file (txt/json)
```

Flags: `--cookies FILE|JSON|header` · `-o DIR` · `--connections N` (default 5)
· `--min-split-size SIZE` (default 5M) · `--no-aria2` (single-stream fallback).
URLs also accepted from stdin/file via `-`.

Cookie formats auto-detected: Netscape/curl txt, JSON browser exports, inline
dict, raw `name=value; ...` header. Key cookies: `ndus`, `browserid`.

Exit codes: `0` success, `1` any failure (unresolved link, download error,
speed < 1 MB/s, invalid cookies).

Educational/personal use only.
