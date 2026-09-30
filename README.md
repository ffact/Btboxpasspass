# TeraBox Link Bypass — standalone CLI 🚀

Single-file command-line tool (`terabox.py`) that resolves TeraBox share links
into direct download links and can download the files while measuring speed.
No environment variables — everything is passed via flags or a local config
file (`./terabox.json`).

Install deps: `pip install aiohttp`

## Commands

```bash
python terabox.py --help                       # usage overview

# one-time setup (asks for the cookie source, saves ./terabox.json)
python terabox.py configure
python terabox.py configure --cookies cookies.txt   # non-interactive

# bypass link(s) straight from the shell
python terabox.py bypass https://1024terabox.com/s/1AbC
cat links.txt | python terabox.py bypass -          # stdin / file paths work too
python terabox.py bypass --quiet <url>              # only the direct dlinks
python terabox.py bypass --json  <url>              # machine-readable output
python terabox.py bypass --cookies cookies.txt <url>  # ad-hoc cookie override

# download the shared file(s) and report throughput
# (SUCCESS if >= 1 MB/s, FAILURE + exit code 1 otherwise)
python terabox.py download --cookies cookies.txt -o ./out <url>

# validate a cookie file (txt or json); exit 0 = compliant
python terabox.py check-cookies cookies.txt
```

## Cookie sources (auto-detected format)

The `--cookies` flag or the `cookies` entry in `terabox.json` accepts any of:

* **Netscape/curl `.txt`** files (tab- or space-separated, e.g. exported with
  "Get cookies.txt" extensions or `curl --cookie-jar`)
* **JSON browser exports** (Firefox storage-inspector nested maps,
  EditThisCookie arrays, lists of cookie objects). A top-level `userAgent`
  field is applied as the request User-Agent.
* Inline dict text (`{"ndus": "...", "browserid": "..."}`)
* Raw header strings (`name=value; name2=value2`)

Non-TeraBox cookies (Google `_ga`, `__Secure-*`, etc.) are filtered out
automatically. The important cookies are `ndus` and `browserid` —
`check-cookies` verifies their presence and expiry.

## Config file

`./terabox.json` (override path with `-c/--config`):

```json
{ "cookies": "cookies.txt" }
```

Flag values always win over the config file; the config file is optional.

## Exit codes

| Command | 0 | 1 |
|---|---|---|
| `bypass` | all URLs resolved | at least one failed |
| `download` | every file downloaded at ≥ 1 MB/s | failure or slow (< 1 MB/s) |
| `check-cookies` | compliant (ndus+browserid, unexpired) | missing/expired/unparseable |

## Disclaimer

Intended for educational and personal use only.
