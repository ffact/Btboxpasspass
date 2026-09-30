# TeraBox Link Bypass — standalone CLI 🚀

Single-file command-line tool (`terabox.py`) that resolves TeraBox share links
into direct download links and can download the files while measuring speed.
No environment variables and no config files — everything is passed via flags.

Install deps: `pip install aiohttp` (plus optionally `apt install aria2` for
multi-connection downloads).

## Commands

```bash
python terabox.py --help                       # usage overview

# bypass link(s) straight from the shell
python terabox.py bypass https://1024terabox.com/s/1AbC
cat links.txt | python terabox.py bypass -          # stdin / file paths work too
python terabox.py bypass --quiet <url>              # only the direct dlinks
python terabox.py bypass --json  <url>              # machine-readable output
python terabox.py bypass --cookies cookies.txt <url>  # ad-hoc cookie override

# download the shared file(s) with aria2c and report throughput
# (SUCCESS if >= 1 MB/s, FAILURE + exit code 1 otherwise)
python terabox.py download --cookies cookies.txt -o ./out <url>
python terabox.py download --connections 8 --min-split-size 1M <url>
python terabox.py download --no-aria2 <url>         # built-in single-stream fallback

# validate a cookie file (txt or json); exit 0 = compliant
python terabox.py check-cookies cookies.txt
```

## Cookie sources (auto-detected format)

The `--cookies` flag accepts any of:

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

## Exit codes

| Command | 0 | 1 |
|---|---|---|
| `bypass` | all URLs resolved | at least one failed |
| `download` | every file downloaded at ≥ 1 MB/s | failure or slow (< 1 MB/s) |
| `check-cookies` | compliant (ndus+browserid, unexpired) | missing/expired/unparseable |

## Disclaimer

Intended for educational and personal use only.
