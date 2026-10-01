#!/usr/bin/env python3
r"""TeraBox Link Bypass — command-line edition for a Colab runtime terminal.

Why Colab? TeraBox triggers its anti-bot / "verify" flow on residential IP
addresses. Google Colab VMs have plenty of disk space and a datacenter IP
that TeraBox currently tolerates. This script is NOT a notebook anymore —
run it as a normal Python program from the Colab terminal (Runtime → Run
command), and it performs the whole pipeline in one shot:

  1. install aria2 + cloudflared if missing
  2. resolve every share link into direct dlinks
  3. download ALL resolved files onto the VM with aria2c (x16 connections)
  4. compress every downloaded file into ONE tar.gz archive
  5. serve the archive over a cloudflared quick tunnel and print the FINAL
     aria2c command line whose URL is the proxified archive link
  6. keep the runtime alive (trivial loop) so Colab doesn't suspend while
     you pull the archive from your own machine

USAGE (Colab terminal, or any Linux box):

    python3 terabox_colab.py "https://terabox.com/s/1AbC" \
                             "https://dubox.com/s/1XyZ" \
        --cookies cookies.txt

Cookies may be given as a FILE path or an inline string in ANY supported
format (Netscape .txt export, EditThisCookie/Firefox JSON, or a raw
"ndus=..; browserid=.." header string). Non-TeraBox cookies are dropped.

The last line of output looks like:

    aria2c -x16 -s16 -k1M --continue=true --auto-file-renaming=false \
      --out="terabox_20260101_120000.tar.gz" \
      "https://abc-xyz.trycloudflare.com/content/terabox_out/terabox_20260101_120000.tar.gz"

Paste that into your own machine's terminal — your residential IP never
touches TeraBox at all; it only talks to the (tolerating) Cloudflare edge,
while Colab does the actual TeraBox fetching. The script then idles in an
anti-suspend loop; press Ctrl-C (or stop the cell) when the download is done.

Options:
    --cookies SRC      cookie file path or inline cookie string
    --outdir DIR       download directory (default /content/terabox_out)
    --connections N    aria2 connections per server (default 16)
    --no-archive       skip compression, serve/print each file separately
    --no-loop          exit right after printing the command (no keep-alive)
    bypass URL...      legacy mode: only resolve and print JSON

Sub-dependencies: pip install aiohttp
"""

import argparse
import asyncio
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse

import aiohttp

# --------------------------------------------------------------------- cookies
# (Same auto-detecting loader as the CLI version — kept inline so this file is
#  fully self-contained for a single Colab paste.)

URL_RE = re.compile(r"https?://\S+")
TERABOX_HOSTS = re.compile(
    r"(terabox|teraboxapp|dubox|mirrobox|nephobox|freeterabox|1024tera|4funbox"
    r"|momerybox|tibibox)\.(com|app|fun|link)",
    re.I,
)


def _is_cookie_obj(obj):
    return isinstance(obj, dict) and "name" in obj and "value" in obj


def _walk_cookies(node, out):
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
    import ast
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
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t") if "\t" in line else line.split()
        if len(fields) >= 7 and "." in fields[0]:
            return True
    return False


def load_cookies(source):
    """File path, JSON browser export, Netscape txt, dict literal or header."""
    raw = (source or "").strip()
    if not raw:
        return {}, ""
    if len(raw) < 4096 and os.path.isfile(raw):
        with open(raw, encoding="utf-8", errors="ignore") as fh:
            raw = fh.read().strip()
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
            if not cookies and isinstance(data, dict):
                cookies = {k: str(v) for k, v in data.items()}
            return cookies, ua
    if _looks_netscape(raw):
        return _parse_netscape(raw), ""
    return _parse_pairs(raw), ""


_DROP_PREFIXES = ("_ga", "__bid", "g_state", "csrfToken", "NID", "SID",
                  "__Secure-", "_ytidb", "PREF", "CONSENT")


def only_terabox(cookies):
    return {k: v for k, v in cookies.items() if not k.startswith(_DROP_PREFIXES)}


def build_session_args(cookies_source):
    cookies, ua = load_cookies(cookies_source)
    headers = {"User-Agent": ua} if ua else {}
    return only_terabox(cookies), headers


# ------------------------------------------------------------------- resolver

def human_size(size_bytes):
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
    """Resolve a TeraBox share URL into a flat list of file dicts w/ dlink."""
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
            if any(f.get("dlink") for f in candidate):
                break
    if not files:
        return None

    if any(str(f.get("isdir")) == "1" for f in files):
        out = []
        for top in files:
            if str(top.get("isdir")) == "1":
                fname = top.get("server_filename") or "folder"
                await _walk_share(session, list_url, base, final_url,
                                  top.get("path", "/" + fname),
                                  fname + "/", 0, out)
            elif top.get("dlink"):
                top["_relpath"] = top.get("_relpath") or top.get("server_filename")
                out.append(top)
        return out
    for f in files:
        f.setdefault("_relpath", f.get("server_filename") or "file")
    return files


async def _walk_share(session, list_url, base, referer, path, prefix, depth, out):
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


# ------------------------------------------------------------------ colab bits

def _in_colab():
    try:
        import google.colab  # noqa: F401
        return True
    except ImportError:
        return False


def clear_output():
    if _in_colab():
        from google.colab import output
        output.clear()
    else:
        print("\033[2J\033[H", end="")


def setup(quiet=True):
    """Install aria2 + cloudflared on the Colab VM (idempotent)."""
    def run(cmd):
        return subprocess.run(cmd, capture_output=quiet, shell=isinstance(cmd, str))

    if not shutil.which("aria2c"):
        print("📦 Installing aria2 ...")
        run("apt-get update -qq && apt-get install -y -qq aria2 >/dev/null 2>&1 "
            "|| pip install aria2p >/dev/null 2>&1 || true")
        if not shutil.which("aria2c"):
            # Static fallback: download a prebuilt binary.
            run(["wget", "-q", "https://github.com/qnm/binaries/releases/download/"
                 "latest/aria2-x86_64-linux", "-O", "/usr/local/bin/aria2c"])
            run(["chmod", "+x", "/usr/local/bin/aria2c"])
    if not shutil.which("cloudflared"):
        print("☁️ Installing cloudflared ...")
        run(["wget", "-q",
             "https://github.com/cloudflare/cloudflared/releases/latest/"
             "download/cloudflared-linux-amd64.deb",
             "-O", "/tmp/cloudflared.deb"])
        run(["dpkg", "-i", "/tmp/cloudflared.deb"])
    ok = []
    ok.append("aria2c " + ("✓" if shutil.which("aria2c") else "✗"))
    ok.append("cloudflared " + ("✓" if shutil.which("cloudflared") else "✗"))
    print("✅ Setup done:", ", ".join(ok))


# ------------------------------------------------------------- resolve & list

RESULTS = []  # list of {"url":..., "files":[{name,size,dlink,relpath}]}


def resolve(urls, cookies_source="", quiet=False):
    """Resolve TeraBox share URLs into file entries (stored in RESULTS)."""
    urls = [u for u in (urls if isinstance(urls, list) else [urls])
            if TERABOX_HOSTS.search(u)]
    if not urls:
        raise ValueError("No TeraBox URLs given.")

    async def run():
        out = []
        cookies, headers = build_session_args(cookies_source)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookies=cookies, headers=headers,
                                         cookie_jar=jar) as session:
            for url in urls:
                # remember the post-redirect share page URL — TeraBox dlinks
                # demand that exact Referer when downloaded.
                try:
                    async with session.get(url) as resp:
                        referer = str(resp.url)
                except Exception:
                    referer = url
                files = await fetch_download_links(session, url)
                if not files:
                    print(f"✗ {url}: could not bypass (bad link or expired cookie)")
                    continue
                entries = []
                for f in files:
                    if str(f.get("isdir")) == "1" or not f.get("dlink"):
                        continue
                    rel = f.get("_relpath") or f.get("server_filename") or "file"
                    entries.append({"name": os.path.basename(rel),
                                    "relpath": rel,
                                    "size": int(f.get("size") or 0),
                                    "dlink": f["dlink"]})
                print(f"✓ {url}: {len(entries)} file(s), "
                      f"{human_size(sum(e['size'] for e in entries))} total")
                for e in entries:
                    print(f"    • {e['relpath']}  ({human_size(e['size'])})")
                out.append({"url": url, "referer": referer, "files": entries})
        return out

    RESULTS.clear()
    RESULTS.extend(asyncio.run(run()))
    total = sum(len(r["files"]) for r in RESULTS)
    if not total:
        print("! Nothing resolved — check your cookies (ndus/browserid).")
    return RESULTS


# ------------------------------------------------------------------- download

def download_all(outdir="/content/terabox_out", connections=16, cookies_source=""):
    """Download every resolved file onto the Colab VM using aria2c.

    TeraBox ties each dlink to the session that minted it (cookies + IP), so
    we replay the *same* cookies and Referer through aria2's HTTP headers.
    """
    os.makedirs(outdir, exist_ok=True)
    jobs = []
    for r in RESULTS:
        for f in r["files"]:
            dest = os.path.join(outdir, f["relpath"])
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            jobs.append((f["dlink"].replace("http://", "https://"),
                         dest, f["size"]))
    if not jobs:
        print("! Nothing to download — run resolve(...) first.")
        return False
    referers = [r.get("referer") or r.get("url") or "" for r in RESULTS]

    cookies, hdrs = build_session_args(cookies_source)
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    user_agent = hdrs.get("User-Agent") or \
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"

    # NOTE: aria2's --input-file parser splits option lines on *whitespace*,
    # so paths/filenames containing spaces cannot be expressed there. Build
    # the command with explicit per-file URL + --dir/--out pairs instead
    # (passed as one argv entry each -> no shell quoting issues).
    cmd = ["aria2c",
           f"--max-connection-per-server={connections}", "--split=16",
           "--min-split-size=1M", "--continue=true",
           "--auto-file-renaming=false", "--allow-overwrite=true",
           "--file-allocation=none", "--summary-interval=30",
           "--console-log-level=warn",
           f"--header=User-Agent: {user_agent}"]
    for ref in dict.fromkeys(referers):  # one Referer header per distinct share
        if ref:
            cmd.append(f"--header=Referer: {ref}")
    if cookie_header:
        cmd.append(f"--header=Cookie: {cookie_header}")
    for dlink, dest, _ in jobs:
        cmd += [dlink,
                f"--dir={os.path.dirname(dest)}",
                f"--out={os.path.basename(dest)}"]
    print(f"⬇ Downloading {len(jobs)} file(s) to {outdir} ...")
    rc = subprocess.run(cmd).returncode

    ok = 0
    for _, dest, size in jobs:
        got = os.path.getsize(dest) if os.path.exists(dest) else 0
        good = got > 0 and (not size or got >= size)
        ok += good
        if not good:
            print(f"✗ incomplete: {dest} ({got}/{size or '?'} bytes)")
    print(f"✅ {ok}/{len(jobs)} file(s) complete on Colab."
          + ("  (re-run download_all() to resume failures)" if ok < len(jobs) else ""))
    return rc == 0 and ok == len(jobs)


# ------------------------------------------------------------------- archive

def make_archive(outdir="/content/terabox_out", files=None, name=None):
    """Compress every downloaded file into ONE tar.gz next to outdir.

    Works with any number of files (single file or nested folders). Returns
    the archive path, or None if there is nothing to pack. The archive lives
    OUTSIDE the scanned tree so it never packs itself.
    """
    if files is None:  # everything regular under outdir (multiple downloads)
        files = [p for p in glob.glob(os.path.join(outdir, "**"), recursive=True)
                 if os.path.isfile(p)]
    files = sorted({os.path.abspath(p) for p in files if os.path.isfile(p)})
    if not files:
        print("! Nothing to compress — no downloaded files found.")
        return None
    if name is None:
        name = f"terabox_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
    base_dir = os.path.dirname(os.path.abspath(outdir))  # e.g. /content
    arc = os.path.join(base_dir, name)
    tmp = arc + ".part"
    print(f"🗜 Compressing {len(files)} file(s) into {arc} ...")
    with tarfile.open(tmp, "w:gz") as tf:
        for p in files:
            try:
                arcname = os.path.relpath(p, base_dir)
            except ValueError:  # different drive roots — fall back to basename
                arcname = os.path.basename(p)
            tf.add(p, arcname=arcname)
    os.replace(tmp, arc)
    print(f"✅ Archive ready ({human_size(os.path.getsize(arc))}).")
    return arc


# ------------------------------------------------------- tunnel + aria2 output

TUNNEL_PROC = None
TUNNEL_URL = None


def find_free_port(start=7860, end=7880):
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found")


def start_tunnel(root="/"):
    """Serve `root` over http + cloudflared quick tunnel; returns public URL."""
    global TUNNEL_PROC, TUNNEL_URL
    if TUNNEL_URL:
        return TUNNEL_URL

    subprocess.run(["pkill", "-f", "cloudflared tunnel"], capture_output=True)
    time.sleep(1)
    port = find_free_port()

    # Simple static file server rooted at `/` so any abs path is reachable.
    handler_thread = threading.Thread(
        target=_serve_files, args=(port, root), daemon=True)
    handler_thread.start()

    TUNNEL_PROC = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    for _ in range(30):
        time.sleep(1)
        line = TUNNEL_PROC.stderr.readline().decode("utf-8", errors="ignore")
        m = re.search(r"(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)", line)
        if m:
            TUNNEL_URL = m.group(1)
            break
    if not TUNNEL_URL:
        print("⚠️ Cloudflare tunnel failed to come up. Re-run this cell.")
    return TUNNEL_URL


def _serve_files(port, root):
    import http.server
    import functools

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # silence request spam
            pass

    Handler = functools.partial(Quiet, directory=root)
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler) as httpd:
        httpd.serve_forever()


def quote_url(url):
    """Percent-encode the path so spaces/CJK names survive the shell + HTTP."""
    p = urllib.parse.urlsplit(url)
    safe_path = "/" + urllib.parse.quote(p.path.lstrip("/"), safe="/@:_-~")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, safe_path, p.query, ""))


def stop_tunnel():
    """Kill the cloudflared tunnel (Colab reclaims resources on idle anyway)."""
    global TUNNEL_PROC, TUNNEL_URL
    if TUNNEL_PROC:
        try:
            TUNNEL_PROC.terminate()
        except Exception:
            pass
        TUNNEL_PROC = None
    TUNNEL_URL = None
    print("🛑 Tunnel stopped.")


def serve_and_print(outdir="/content/terabox_out", base="/",
                    connections=16, chunk="1M", dl_dir="./downloaded",
                    archive=None):
    """Start the tunnel and print the aria2 command with proxified links.

    If `archive` (a tar.gz produced by make_archive) is given, the FINAL
    command line downloads that single archive instead of every file —
    one URL for any number of downloaded files.
    """
    url = start_tunnel(base)
    if not url:
        return None
    print(f"✅ Public link: {url}\n")

    if archive:
        if not os.path.exists(archive):
            print("! Archive not found:", archive)
            return None
        prox = url + "/" + urllib.parse.quote(
            os.path.abspath(archive).lstrip("/"), safe="/")
        cmd = (f"aria2c -x{connections} -s{connections} -k{chunk} "
               f"--continue=true --auto-file-renaming=false "
               f'--out="{os.path.basename(archive)}" "{prox}"')
        print("📦 Archive staged on Colab. Run this on YOUR machine:\n")
        print("-" * 70)
        print(cmd)
        print("-" * 70)
        print("\n💡 Extract with: tar -xzf "
              f"{os.path.basename(archive)}")
        print("💡 The tunnel dies when this script/session stops — keep it open.")
        return cmd

    files = []
    for r in RESULTS:
        for f in r["files"]:
            dest = os.path.join(outdir, f["relpath"])
            if os.path.exists(dest):
                files.append((f["relpath"], dest))
    if not files:
        print("! No completed files found in", outdir,
              "— run resolve() + download_all() first.")
        return None

    lines = [f"aria2c -x{connections} -s{connections} -k{chunk} "
             "--continue=true --auto-file-renaming=false"]
    for rel, dest in files:
        # single encode of the path segments -> URL that aria2/shell can use
        prox = url + "/" + urllib.parse.quote(dest.lstrip("/"), safe="/")
        name = os.path.basename(rel)
        sub = os.path.dirname(rel)
        if sub:
            lines.append(f'  --dir="{dl_dir}/{sub}"')
        lines.append(f'  --out="{name}" "{prox}"')
    cmd = " \\\n".join(lines)

    print(f"📁 {len(files)} file(s) staged on Colab. Run this on YOUR machine:\n")
    print("-" * 70)
    print(cmd)
    print("-" * 70)
    print("\n💡 The tunnel dies when this script/session stops — keep it open.")
    return cmd


# ------------------------------------------------------------- keep-alive loop

def keep_alive(interval=60):
    """Trivial loop that keeps the Colab runtime from suspending.

    Colab reclaims idle VMs; a foreground process that periodically produces
    output counts as activity and buys us time while the user pulls the
    archive through the tunnel. Ctrl-C ends the session on purpose.
    """
    print(f"\n⏳ Keeping runtime alive — press Ctrl-C when your download "
          f"is done (heartbeat every {interval}s)...")
    try:
        n = 0
        while True:
            n += 1
            print(f"[keep-alive] heartbeat #{n} — "
                  f"{time.strftime('%H:%M:%S')} — tunnel still up."
                  + (f" {TUNNEL_URL}" if TUNNEL_URL else ""))
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n👋 Keep-alive stopped by user. Shutting down.")
        stop_tunnel()


# ------------------------------------------------------------------------ CLI

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # Legacy smoke-test mode: `python terabox_colab.py bypass <url>` prints
    # the resolved JSON without downloading anything.
    if argv and argv[0] == "bypass":
        urls = [u for u in argv[1:] if not u.startswith("-")]
        cookies = ""
        if "--cookies" in argv:
            cookies = argv[argv.index("--cookies") + 1]
        resolve(urls, cookies)
        print(json.dumps(RESULTS, indent=2, ensure_ascii=False))
        return 0 if RESULTS else 1

    ap = argparse.ArgumentParser(
        prog="terabox_colab.py",
        description="TeraBox bypass pipeline for a Colab runtime terminal: "
                    "resolve → download → compress → tunnel → aria2 command.",
        epilog="Example: python3 terabox_colab.py https://terabox.com/s/1AbC "
               "--cookies cookies.txt")
    ap.add_argument("urls", nargs="+", help="one or more TeraBox share links")
    ap.add_argument("--cookies", default="",
                    help="cookie file path OR inline cookie string "
                         "(Netscape/JSON/header, auto-detected)")
    ap.add_argument("--outdir", default="/content/terabox_out",
                    help="directory where files are downloaded (default: %(default)s)")
    ap.add_argument("--connections", type=int, default=16,
                    help="aria2 connections per server (default: %(default)s)")
    ap.add_argument("--no-archive", action="store_true",
                    help="skip compression; serve/print each file separately")
    ap.add_argument("--no-loop", action="store_true",
                    help="exit right after printing the final command "
                         "(no anti-suspend keep-alive loop)")
    args = ap.parse_args(argv)

    # 1) tooling
    setup()

    # 2) resolve every share link into direct dlinks
    try:
        resolve(args.urls, args.cookies)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    if not RESULTS:
        print("✗ Nothing resolved — check the links/cookies.")
        return 1

    # 3) download all files onto this machine
    ok = download_all(outdir=args.outdir, connections=args.connections,
                      cookies_source=args.cookies)
    if not ok:
        print("⚠ Some downloads failed — re-run the same command to resume, "
              "or continuing with whatever completed...")

    # 4) compress ALL downloaded files into one archive
    archive = None
    if not args.no_archive:
        archive = make_archive(outdir=args.outdir)

    # 5) tunnel + final aria2 command line (uses the archive when present)
    cmd = serve_and_print(outdir=args.outdir, archive=archive,
                          connections=args.connections)
    if cmd is None:
        return 1

    # 6) trivial loop so the Colab runtime doesn't suspend mid-transfer
    if not args.no_loop:
        keep_alive()
    return 0


if __name__ == "__main__":
    sys.exit(main())
