#!/usr/bin/env python3
"""TeraBox Link Bypass — standalone single-file CLI tool. No env vars needed.

All configuration is passed as command-line flags and/or stored in a local
config file (default: ./terabox.json, created by `configure`).

Install deps:   pip install aiohttp

CLI usage:
    # one-time setup (asks for the cookie source, saves ./terabox.json)
    python terabox.py configure

    # bypass links straight from the shell
    python terabox.py bypass https://terabox.com/s/1AbC https://dubox.com/s/1XyZ
    python terabox.py bypass --cookies cookies.txt <url>... # ad-hoc cookie file
    echo <url> | python terabox.py bypass -                 # read URLs from stdin
    python terabox.py bypass --json <url>                   # machine-readable
    python terabox.py bypass --quiet <url>                  # just the direct links

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
import logging
import os
import re
import sys
import time

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("terabox")

CONFIG_PATH_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terabox.json")

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


# ------------------------------------------------------------------------ config


def parse_list(value):
    """Parse '[-123, 456]' / '["a","b"]' / 'a,b' into a list of strings."""
    value = (value or "").strip()
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        try:
            data = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            data = [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(data, (list, tuple, set)):
        return [str(x) for x in data]
    return [str(data)]


def load_config(path):
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    logger.info("Saved config to %s", path)


def ask(prompt, default=None, secret=False):
    try:
        import getpass
        val = getpass.getpass(prompt + ": ") if secret else input(prompt + ": ")
    except EOFError:
        val = ""
    val = val.strip()
    return val or (default or "")


def cmd_configure(args):
    """Interactively create/update the config file. Flag values win over prompts."""
    path = args.config
    cfg = load_config(path)
    print(f"Configuring {path} (Enter keeps current/default shown in [...]).")

    def pick(name, prompt, flag=None, default="", secret=False):
        cur = getattr(args, name, None)
        if cur:                      # explicit flag -> no prompt
            cfg[name] = cur
            print(f"  {prompt}: set from flag")
            return
        old = cfg.get(name, default)
        val = ask(f"{prompt}" + (f" [{old[:12] + '...' if secret and old else old}]" if old else ""),
                  default=old, secret=secret)
        if val:
            cfg[name] = val

    pick("cookies", "Cookie file path or JSON/dict text (blank = no cookies)")

    # Normalise cookies once so bad files fail loudly now, not at runtime.
    if cfg.get("cookies"):
        cookies, ua = load_cookies(cfg["cookies"])
        if not cookies:
            print("  ! Could not parse the cookie source; keeping it anyway.")
        else:
            names = ", ".join(sorted(only_terabox(cookies))) or "(all filtered out)"
            print(f"  Cookies OK ({len(cookies)} parsed; using: {names})"
                  + (" + userAgent" if ua else ""))
    save_config(path, cfg)
    print("Done. Run:  python terabox.py bypass <url>")
    return 0


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
    # Shared folder: list its contents instead
    if str(files[0].get("isdir")) == "1":
        dir_params = {**base, "root": "1", "period": "all", "site_referer": final_url,
                      "dir": files[0].get("path", ""), "order": "name", "by": "name"}
        data = await _share_list(session, list_url, dir_params)
        files = data.get("list")
    return files


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

    cfg = load_config(args.config)
    cookies_source = args.cookies or cfg.get("cookies", "")

    results = asyncio.run(collect_results(urls, cookies_source, args.quiet, args.json))
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


async def download_file(session, dlink, referer, dest):
    """Stream one dlink to `dest`; returns (bytes_written, seconds)."""
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
    """Download the shared file(s) and report throughput; fail under 1 MB/s."""
    urls = [u for u in read_urls(args.urls) if TERABOX_HOSTS.search(u)]
    if not urls:
        sys.exit("No TeraBox URLs given.")
    cfg = load_config(args.config)
    cookies_source = args.cookies or cfg.get("cookies", "")

    async def run():
        overall_ok = True
        outdir = args.out or "."
        os.makedirs(outdir, exist_ok=True)
        cookies, headers = build_session_args(cookies_source)
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
                for f in files:
                    name = f.get("server_filename") or "file"
                    dlink = f.get("dlink")
                    if str(f.get("isdir")) == "1" or not dlink:
                        print(f"! Skipping {name} (folder or no direct link)")
                        continue
                    dest = os.path.join(outdir, name)
                    print(f"⬇ Downloading {name} ({human_size(f.get('size'))}) -> {dest}")
                    try:
                        got, secs = await download_file(session, dlink, referer, dest)
                    except Exception as e:
                        print(f"✗ {name}: download failed: {e}")
                        overall_ok = False
                        continue
                    rate = got / secs / (1024 * 1024)
                    verdict = "SUCCESS" if rate >= 1.0 else "FAILURE (< 1 MB/s)"
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
        description="TeraBox link bypass — standalone CLI tool, configured via flags or a config file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("CLI usage:")[1],
    )
    parser.add_argument("-c", "--config", default=CONFIG_PATH_DEFAULT,
                        help=f"config file path (default: {CONFIG_PATH_DEFAULT})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bypass", aliases=["dl"], help="bypass TeraBox link(s) in the shell")
    p.add_argument("urls", nargs="+",
                   help="TeraBox share URLs, paths to files containing links, or '-' for stdin")
    p.add_argument("--cookies", help="cookie file/json override for this run")
    p.add_argument("--json", action="store_true", help="print results as a JSON array")
    p.add_argument("--quiet", action="store_true", help="print only direct download links")
    p.set_defaults(func=cmd_bypass)

    p = sub.add_parser("download", help="actually download the shared file(s) and measure speed")
    p.add_argument("urls", nargs="+",
                   help="TeraBox share URLs, paths to files containing links, or '-' for stdin")
    p.add_argument("--cookies", help="cookie file/json override for this run")
    p.add_argument("-o", "--out", default=".", help="output directory (default: .)")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("configure", aliases=["config"], help="interactively save settings to the config file")
    p.add_argument("--cookies")
    p.set_defaults(func=cmd_configure)

    p = sub.add_parser("check-cookies", help="validate a Netscape .txt or JSON cookie file")
    p.add_argument("file"); p.set_defaults(func=check_cookies_cmd)

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    rc = args.func(args)
    sys.exit(rc or 0)
