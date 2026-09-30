#!/usr/bin/env python3
"""TeraBox Link Bypass — standalone single-file CLI tool. No env vars needed.

Everything is passed as command-line flags; no config files to manage.

Install deps:   pip install aiohttp          (optional: apt install aria2)

CLI usage:
    # bypass links straight from the shell
    python terabox.py bypass https://terabox.com/s/1AbC https://dubox.com/s/1XyZ
    python terabox.py bypass --cookies cookies.txt <url>... # ad-hoc cookie file
    echo <url> | python terabox.py bypass -                 # read URLs from stdin
    python terabox.py bypass --json <url>                   # machine-readable
    python terabox.py bypass --quiet <url>                  # just the direct links

    # download via aria2c (multi-connection; falls back to plain HTTP if absent)
    python terabox.py download --cookies cookies.txt <url>...
    python terabox.py download --connections 8 --min-split-size 1M -o out <url>...

    # helpers
    python terabox.py check-cookies FILE   # validate a Netscape/.txt or .json cookie file

Supported cookie formats (auto-detected): Netscape/curl txt, JSON browser
exports (Firefox / EditThisCookie / array of cookie objects), inline dict text,
or raw "name=value; name2=value2" header strings. A "userAgent" field in JSON
exports is applied as the request User-Agent. Non-TeraBox cookies (e.g. Google)
are ignored automatically.
"""

import argparse
import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time

import aiohttp

URL_RE = re.compile(r"https?://\S+")
TERABOX_HOSTS = re.compile(
    r"(terabox|teraboxapp|dubox|mirrobox|nephobox|freeterabox|1024tera|4funbox"
    r"|momerybox|tibibox)\.(com|app|fun|link)",
    re.I,
)

# --------------------------------------------------------------------- cookies


def _is_cookie_obj(obj):
    return isinstance(obj, dict) and "name" in obj and "value" in obj


def _walk_cookies(node, out):
    """Recursively collect {name: value} pairs from any JSON export shape."""
    if isinstance(node, dict):
        if _is_cookie_obj(node):
            out[node["name"]] = node["value"]
        else:
            for v in node.values():
                _walk_cookies(v, out)
    elif isinstance(node, list):
        for item in node:
            if _is_cookie_obj(item):
                out[item["name"]] = item["value"]
    return out


def _parse_netscape(text):
    """Parse the Netscape/curl cookie format into {name: value}.

    Accepts tab-separated (canonical curl output) as well as sloppy files whose
    fields were separated by spaces instead of tabs.
    """
    cookies = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t") if "\t" in line else line.split()
        if len(fields) >= 7 and "." in fields[0]:
            cookies[fields[5]] = fields[6]
    return cookies


def _parse_pairs(raw):
    """Inline Python-dict literal or 'a=b; c=d' cookie-header string."""
    try:
        data = ast.literal_eval(raw)
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items()}
    except (ValueError, SyntaxError):
        pass
    pairs = {}
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k and v:
            pairs[k] = v
    return pairs


def _looks_netscape(text):
    """True if any non-comment line has >=7 whitespace/tab-separated fields."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t") if "\t" in line else line.split()
        if len(fields) >= 7 and "." in fields[0]:
            return True
    return False


def load_cookies(source):
    """Accept a file path (.txt/.json), inline JSON/dict text, or header string.

    Returns (cookies, user_agent).
    """
    raw = (source or "").strip()
    if not raw:
        return {}, ""
    # 1) A path to a cookie file (content sniffing decides txt vs json).
    if len(raw) < 4096 and os.path.isfile(raw):
        with open(raw, encoding="utf-8", errors="ignore") as fh:
            raw = fh.read().strip()
    # 2) JSON export (Firefox nested map / EditThisCookie array / plain dict).
    if raw[:1] in "{[":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            ua = ""
            if isinstance(data, dict):
                ua = data.get("userAgent") or data.get("user_agent") or ""
            cookies = _walk_cookies(data, {})
            if not cookies and isinstance(data, dict):  # plain {"ndus": "..."} map
                cookies = {k: str(v) for k, v in data.items()}
            return cookies, ua
    # 3) Netscape txt format (tab- or space-separated lines).
    if _looks_netscape(raw):
        return _parse_netscape(raw), ""
    # 4) Inline dict literal / Cookie header string.
    return _parse_pairs(raw), ""


# Cookie names that leak from full-browser exports and are useless/harmful here.
_DROP_PREFIXES = ("_ga", "__bid", "g_state", "csrfToken", "NID", "SID",
                  "__Secure-", "_ytidb", "PREF", "CONSENT")


def only_terabox(cookies):
    return {k: v for k, v in cookies.items() if not k.startswith(_DROP_PREFIXES)}


# -------------------------------------------------------------------- downloader


def human_size(size_bytes):
    """Format a byte count as KB/MB/GB."""
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "unknown"
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "bytes" else f"{int(size)} {unit}"
        size /= 1024


def _between(text, start, end):
    i = text.find(start)
    if i == -1:
        return ""
    i += len(start)
    j = text.find(end, i)
    return text[i:j] if j != -1 else ""


async def _share_list(session, list_url, params):
    try:
        async with session.get(list_url, params=params) as resp:
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, json.JSONDecodeError):
        return {}


async def fetch_download_links(session, url):
    """Resolve a TeraBox share URL into a list of file dicts (with `dlink`)."""
    async with session.get(url) as resp:
        html = await resp.text()
        final_url = str(resp.url)

    js_token = _between(html, "fn%28%22", "%22%29")
    log_id = _between(html, "dp-logid=", "&")
    surl = _between(final_url + "&", "surl=", "&")
    if not js_token or not surl:
        return None

    base = {"app_id": "250528", "web": "1", "channel": "dubox", "clienttype": "0",
            "jsToken": js_token, "shorturl": surl}
    list_url = "https://www.1024tera.com/share/list"
    # Payload order matters: TeraBox answers the compact "periodic" form even
    # when the verbose page/dp-logid form returns {"code":460020,"need verify"},
    # and dlink is only populated when site_referer is included.
    payloads = [
        {**base, "root": "1", "period": "all", "site_referer": final_url},
        {**base, "root": "1", "period": "all"},
        {**base, "dp-logid": log_id, "page": "1", "num": "20", "order": "time",
         "desc": "1", "site_referer": final_url, "root": "1"},
    ]
    files = []
    for params in payloads:
        data = await _share_list(session, list_url, params)
        candidate = data.get("list") or []
        if candidate:
            files = candidate
            # keep looking only if current payload gave no usable download links
            if any(f.get("dlink") for f in candidate):
                break

    if not files:
        return None
    # Shared folder(s): recursively walk the tree and return every file found.
    # NOTE: sub-directory listings need root=0 AND the folder's real path —
    # dir=/ or root=1 just echoes the folder entry (or errors errno=2).
    if any(str(f.get("isdir")) == "1" for f in files):
        out = []
        for top in files:
            if str(top.get("isdir")) == "1":
                fname = top.get("server_filename") or "folder"
                await _walk_share(session, list_url, base, final_url,
                                  top.get("path", "/" + fname),
                                  fname + "/", 0, out)   # prefix keeps folder name
            elif top.get("dlink"):
                top["_relpath"] = top.get("_relpath") or top.get("server_filename")
                out.append(top)
        return out
    for f in files:  # plain-file share: no folder prefix
        f.setdefault("_relpath", f.get("server_filename") or "file")
    return files


async def _walk_share(session, list_url, base, referer, path, prefix, depth, out):
    """Depth-first listing of one shared folder; appends flat file dicts to out."""
    params = {**base, "root": "0", "period": "all", "site_referer": referer,
              "dir": path, "order": "name", "by": "name"}
    data = await _share_list(session, list_url, params)
    for e in data.get("list") or []:
        name = e.get("server_filename") or "file"
        rel = prefix + name
        if str(e.get("isdir")) == "1":
            if depth < 5:
                await _walk_share(session, list_url, base, referer,
                                  e.get("path", path + "/" + name), rel + "/", depth + 1, out)
        elif e.get("dlink"):
            e["_relpath"] = rel
            out.append(e)


def build_session_args(cookies_source):
    """Turn a cookie source into (cookies_dict, headers_dict) for aiohttp."""
    cookies, ua = load_cookies(cookies_source)
    headers = {"User-Agent": ua} if ua else {}
    return only_terabox(cookies), headers


async def bypass_all(urls, cookies_source, quiet=False):
    """Resolve each URL; yield (url, files_or_None, error_or_None)."""
    cookies, headers = build_session_args(cookies_source)
    # unsafe jar: the share page sets helper cookies (TSID/csrfToken/ndut_fmt)
    # on a host that differs from the /share/list API host, and aiohttp's
    # default jar would refuse to send them cross-domain.
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookies=cookies, headers=headers,
                                     cookie_jar=jar) as session:
        for url in urls:
            try:
                files = await fetch_download_links(session, url)
                yield url, files, None if files else "could not bypass (bad link or expired cookie)"
            except Exception as e:
                yield url, None, str(e)


def read_urls(values):
    """Accept URLs as arguments, '-' to read stdin, or paths to text files."""
    urls = []
    for v in values:
        if v == "-":
            urls += URL_RE.findall(sys.stdin.read())
        elif len(v) < 4096 and os.path.isfile(v):
            with open(v, encoding="utf-8", errors="ignore") as fh:
                urls += URL_RE.findall(fh.read())
        else:
            urls.append(v)
    # de-duplicate, keep order
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def cmd_bypass(args):
    all_urls = read_urls(args.urls)
    urls = [u for u in all_urls if TERABOX_HOSTS.search(u)]
    for u in all_urls:
        if u not in urls:
            print(f"! Skipping non-TeraBox URL: {u}", file=sys.stderr)
    if not urls:
        sys.exit("No TeraBox URLs given. Example: python terabox.py bypass https://terabox.com/s/1xxxx")

    results = asyncio.run(collect_results(urls, args.cookies, args.quiet, args.json))
    return 0 if all(results) else 1


async def collect_results(urls, cookies_source, quiet, as_json):
    ok_flags, rows = [], []
    async for url, files, err in bypass_all(urls, cookies_source, quiet):
        ok = files is not None
        ok_flags.append(ok)
        if as_json:
            rows.append({"url": url, "ok": ok,
                         "files": [{"name": f.get("server_filename"), "size": f.get("size"),
                                    "dlink": f.get("dlink")} for f in (files or [])],
                         "error": err})
        elif quiet:
            for f in (files or []):
                print(f.get("dlink", ""))
            if err:
                print(f"! {url}: {err}", file=sys.stderr)
        else:
            if err:
                print(f"\n✗ {url}\n  {err}")
            else:
                print(f"\n✓ {url}")
                for f in files:
                    print(f"  ┎ Title : {f.get('server_filename', 'file')}")
                    print(f"  ┠ Size  : {human_size(f.get('size'))}")
                    print(f"  ┖ Link  : {f.get('dlink', '')}")
    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    return ok_flags


def aria2_download(dlink, referer, cookie_header, outdir, name,
                   connections=5, split_size="5M", user_agent=""):
    """Download one file via aria2c. Returns True on success (file complete)."""
    aria2c = shutil.which("aria2c")
    if not aria2c:
        return None  # signal: aria2 unavailable
    cmd = [
        aria2c, "--continue=true", "--auto-file-renaming=false",
        "--allow-overwrite=true", "--summary-interval=0", "--console-log-level=warn",
        f"--max-connection-per-server={connections}",
        f"--min-split-size={split_size}", "--split=16",
        f"--header=Referer: {referer}",
        f"--header=Cookie: {cookie_header}",
    ]
    if user_agent:
        cmd.append(f"--header=User-Agent: {user_agent}")
    cmd += [f"--dir={outdir}", f"--out={name}", dlink.replace("http://", "https://")]
    proc = subprocess.run(cmd)
    return proc.returncode == 0 and os.path.isfile(os.path.join(outdir, name))


async def download_file(session, dlink, referer, dest):
    """Fallback single-stream HTTP download; returns (bytes_written, seconds)."""
    t0 = time.monotonic()
    got = 0
    url = dlink.replace("http://", "https://")
    tmp = dest + ".part"
    async with session.get(url, headers={"Referer": referer},
                           timeout=aiohttp.ClientTimeout(total=None, sock_read=60)) as resp:
        if resp.status != 200:
            body = (await resp.text())[:200]
            raise RuntimeError(f"HTTP {resp.status}: {body}")
        with open(tmp, "wb") as fh:
            async for chunk in resp.content.iter_chunked(131072):
                fh.write(chunk)
                got += len(chunk)
    os.replace(tmp, dest)
    return got, time.monotonic() - t0


def cmd_download(args):
    """Resolve share URLs, download via aria2c, report throughput per file."""
    urls = [u for u in read_urls(args.urls) if TERABOX_HOSTS.search(u)]
    if not urls:
        sys.exit("No TeraBox URLs given.")
    use_aria2 = not args.no_aria2 and shutil.which("aria2c")
    if not args.no_aria2 and not use_aria2:
        print("! aria2c not found — falling back to plain HTTP download", file=sys.stderr)

    async def run():
        overall_ok = True
        base_out = args.out or "."
        os.makedirs(base_out, exist_ok=True)
        cookies, headers = build_session_args(args.cookies)
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        user_agent = headers.get("User-Agent", "")
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookies=cookies, headers=headers,
                                         cookie_jar=jar) as session:
            for url in urls:
                async with session.get(url) as resp:
                    html = await resp.text()
                    referer = str(resp.url)
                files = await fetch_download_links(session, url)
                if not files:
                    print(f"✗ {url}: could not bypass (bad link or expired cookie)")
                    overall_ok = False
                    continue
                # A shared folder gets its own output subdirectory named after it.
                outdir = base_out
                folders = sorted({(f.get("_relpath") or "").split("/")[0]
                                  for f in files if f.get("_relpath")})
                if len(folders) == 1 and folders[0]:
                    outdir = os.path.join(base_out, folders[0])
                    os.makedirs(outdir, exist_ok=True)
                for f in files:
                    rel = f.get("_relpath") or f.get("server_filename") or "file"
                    name = os.path.basename(rel)
                    dlink = f.get("dlink")
                    if str(f.get("isdir")) == "1" or not dlink:
                        print(f"! Skipping {name} (folder or no direct link)")
                        continue
                    subdir = os.path.dirname(rel)
                    # outdir already carries the top folder name; don't double it
                    if subdir.split("/")[0] == os.path.basename(outdir):
                        subdir = subdir.split("/", 1)[1] if "/" in subdir else ""
                    filedir = os.path.join(outdir, subdir) if subdir else outdir
                    os.makedirs(filedir, exist_ok=True)
                    dest = os.path.join(filedir, name)
                    expected = f.get("size")
                    print(f"⬇ Downloading {name} ({human_size(expected)}) -> {dest}"
                          + (f" [aria2c x{args.connections}]" if use_aria2 else ""))
                    t0 = time.monotonic()
                    try:
                        done = aria2_download(dlink, referer, cookie_header, filedir, name,
                                              args.connections, args.min_split_size,
                                              user_agent) if use_aria2 else None
                        if done is None:  # no aria2 -> aiohttp fallback
                            got, _ = await download_file(session, dlink, referer, dest)
                        elif not done:
                            raise RuntimeError("aria2c failed (see its output above)")
                        else:
                            got = os.path.getsize(dest)
                    except Exception as e:
                        print(f"✗ {name}: download failed: {e}")
                        overall_ok = False
                        continue
                    secs = time.monotonic() - t0
                    rate = got / secs / (1024 * 1024)
                    verdict = "SUCCESS" if rate >= 1.0 else "FAILURE (< 1 MB/s)"
                    if expected and int(got) < int(expected):
                        print(f"✗ {name}: incomplete ({got}/{expected} bytes)")
                        overall_ok = False
                    else:
                        print(f"✓ {name}: {got} bytes in {secs:.1f}s = {rate:.2f} MB/s -> {verdict}")
                        if rate < 1.0:
                            overall_ok = False
        return overall_ok

    return 0 if asyncio.run(run()) else 1


# ----------------------------------------------------------------------- helpers


def check_cookies_cmd(args):
    """Validate a cookie file (txt or json) and report compliance. Exit 0/1."""
    path = args.file
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError as e:
        sys.exit(f"Cannot read {path}: {e}")
    cookies, ua = load_cookies(text)
    if not cookies:
        print(f"✗ {path}: no cookies recognised "
              "(expected Netscape tab-separated txt or JSON export)")
        return 1
    print(f"✓ {path}: parsed {len(cookies)} cookie(s): {', '.join(sorted(cookies))}")
    if ua:
        print(f"✓ userAgent found: {ua[:60]}...")
    terabox = only_terabox(cookies)
    dropped = sorted(set(cookies) - set(terabox))
    if dropped:
        print(f"ℹ Ignoring non-TeraBox cookies: {', '.join(dropped)}")
    missing = [k for k in ("ndus", "browserid") if k not in terabox]
    code = 0
    if missing:
        print(f"✗ Missing important TeraBox cookie(s): {', '.join(missing)} "
              f"— links may fail to bypass")
        code = 1
    else:
        print("✓ ndus + browserid present — compliant for TeraBox bypass")
    # Expiry sanity check (JSON exports / Netscape txt).
    now = time.time()
    exp = [float(e) for e in re.findall(r'"expirationDate"\s*:\s*([0-9.]+)', text)]
    if not exp:
        for line in text.splitlines():
            if line.startswith("#") or "\t" not in line:
                continue
            f = line.split("\t")
            if len(f) >= 7 and f[4].isdigit() and int(f[4]) > 0:
                exp.append(float(f[4]))
    if exp:
        latest = max(exp)
        days = (latest - now) / 86400
        if days < 0:
            print(f"✗ Newest cookie expired {-days:.0f} day(s) ago "
                  f"({time.strftime('%Y-%m-%d', time.localtime(latest))}) — re-export your cookies!")
            code = 1
        else:
            status = "OK" if days > 7 else "EXPIRES SOON"
            print(f"ℹ Latest expiry: {time.strftime('%Y-%m-%d', time.localtime(latest))} "
                  f"({days:.0f} days left) [{status}]")
    return code


# -------------------------------------------------------------------------- main


def build_parser():
    parser = argparse.ArgumentParser(
        prog="terabox.py",
        description="TeraBox link bypass — standalone CLI tool, configured via flags only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("CLI usage:")[1],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bypass", help="bypass TeraBox link(s) in the shell")
    p.add_argument("urls", nargs="+",
                   help="TeraBox share URLs, paths to files containing links, or '-' for stdin")
    p.add_argument("--cookies", help="cookie file (txt/json), inline JSON/dict, or header string")
    p.add_argument("--json", action="store_true", help="print results as a JSON array")
    p.add_argument("--quiet", action="store_true", help="print only direct download links")
    p.set_defaults(func=cmd_bypass)

    p = sub.add_parser("download", help="download the shared file(s) with aria2c and measure speed")
    p.add_argument("urls", nargs="+",
                   help="TeraBox share URLs, paths to files containing links, or '-' for stdin")
    p.add_argument("--cookies", help="cookie file (txt/json), inline JSON/dict, or header string")
    p.add_argument("-o", "--out", default=".", help="output directory (default: .)")
    p.add_argument("--connections", type=int, default=5, metavar="N",
                   help="aria2c max connections per server (default: 5)")
    p.add_argument("--min-split-size", default="5M", metavar="SIZE",
                   help="aria2c min split size (default: 5M)")
    p.add_argument("--no-aria2", action="store_true",
                   help="skip aria2c and use the built-in single-stream downloader")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("check-cookies", help="validate a Netscape .txt or JSON cookie file")
    p.add_argument("file"); p.set_defaults(func=check_cookies_cmd)

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    rc = args.func(args)
    sys.exit(rc or 0)
