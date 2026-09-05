#!/usr/bin/env python3
import http.server
import socketserver
import subprocess
import os
import re
import json
import signal
import shlex
import time
import urllib.request
import urllib.parse
from datetime import datetime

MAX_RETRIES = 50
RETRY_DELAY_SECONDS = 10

DOWNLOAD_DIR = "/var/downloads"
STATE_FILE = os.path.expanduser("~/fetcher_state.json")
PORT = 8092

EXT_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf", "application/zip": ".zip",
    "application/x-7z-compressed": ".7z", "application/x-rar-compressed": ".rar",
    "application/gzip": ".gz", "application/x-tar": ".tar",
    "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "audio/mpeg": ".mp3", "audio/flac": ".flac",
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "text/plain": ".txt", "application/json": ".json",
    "application/octet-stream": ".bin",
}

def probe_url(url):
    """One lightweight request that answers everything we need before starting
    a download: the real filename (Content-Disposition, then URL path after
    following redirects, then a timestamped fallback), and the total size
    (Content-Length) for a real progress percentage. Issues a real GET, not a
    HEAD - many servers (CDNs, cloud storage) only attach Content-Disposition
    when actually serving the body - but closes the connection immediately
    after reading headers, before any body bytes are read, so it doesn't
    download the file twice."""
    content_type = ""
    content_length = None
    final_path = urllib.parse.urlparse(url).path
    name, source = None, None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=8)
        try:
            cl = resp.headers.get("Content-Length")
            if cl and cl.strip().isdigit():
                content_length = int(cl)
            cd = resp.headers.get("Content-Disposition", "") or ""
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
            if m:
                candidate = os.path.basename(urllib.parse.unquote(m.group(1)).strip())
                if candidate:
                    name, source = candidate, "server"
            content_type = (resp.headers.get("Content-Type", "") or "").split(";")[0].strip()
            # resp.url is the final URL after following any redirect chain,
            # which is often where the real filename actually lives even when
            # the link you pasted was a short-lived redirect wrapper.
            final_path = urllib.parse.urlparse(resp.url).path
        finally:
            resp.close()
    except Exception:
        pass

    if name is None:
        base = os.path.basename(urllib.parse.unquote(final_path))
        if base and "." in base and len(base) < 120:
            name, source = base, "url"
        else:
            ext = EXT_BY_CONTENT_TYPE.get(content_type, "")
            name = "download-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ext
            source = "fallback"

    return name, source, content_length

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            # A corrupt/partial read must never be treated as "empty" - the
            # next save_state() would then overwrite real history with just
            # whatever's being added right now, permanently losing the rest.
            backup = STATE_FILE + ".corrupt-" + datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                os.replace(STATE_FILE, backup)
            except OSError:
                pass
            return []
    return []

def save_state(state):
    # Write to a temp file then atomically rename over the real one, so a
    # crash/restart mid-write leaves the previous good file intact instead
    # of a half-written, corrupt one.
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)

def is_running(pid):
    """True if the process is still running. Also reaps it if it just exited -
    without this, a finished download's process becomes a zombie that Linux
    still reports as "existing", which would make the status page show
    "downloading..." forever even after the file is already complete."""
    if not pid:
        return False
    try:
        wpid, _status = os.waitpid(pid, os.WNOHANG)
        return wpid == 0
    except ChildProcessError:
        # Not our direct child - happens whenever this service restarts
        # (a code deploy) while a download is running: KillMode=process
        # deliberately lets the detached wget loop survive the restart, but
        # it's then reparented away from us, so we can no longer waitpid()
        # it. That doesn't mean it's dead - fall back to a plain existence
        # check so a still-running download isn't misreported as finished.
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

# --------------------------------------------------------------- icons
# Icons carry a title and an aria-label rather than standing alone: a glyph
# is quicker to scan once you know it, but ambiguous the first time and
# invisible to a screen reader.
ICON_PAUSE = (
    '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">'
    '<rect x="4" y="3" width="3.2" height="10" rx="1" fill="currentColor"/>'
    '<rect x="8.8" y="3" width="3.2" height="10" rx="1" fill="currentColor"/>'
    '</svg>'
)
ICON_PLAY = (
    '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">'
    '<path d="M5 3.2v9.6a.6.6 0 0 0 .92.5l7.2-4.8a.6.6 0 0 0 0-1L5.92 2.7'
    'A.6.6 0 0 0 5 3.2z" fill="currentColor"/>'
    '</svg>'
)
ICON_TRASH = (
    '<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true" '
    'fill="none" stroke="currentColor" stroke-width="1.3" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M6 3.4V2.9a.9.9 0 0 1 .9-.9h2.2a.9.9 0 0 1 .9.9v.5"/>'
    '<path d="M3.2 4.2h9.6"/>'
    '<path d="M4.7 4.2l.55 8.1a1 1 0 0 0 1 .93h3.5a1 1 0 0 0 1-.93l.55-8.1"/>'
    '</svg>'
)

def icon_action(href, icon, label, danger=False):
    cls = "act act-danger" if danger else "act"
    return (f'<a class="{cls}" href="{href}" title="{label}" '
            f'aria-label="{label}">{icon}</a>')

# ------------------------------------------------------------ live speed
# wget runs detached with its output discarded, so there's no progress stream
# to read - speed is derived by watching the destination file grow between
# page renders. Samples live in memory rather than in the state file so the
# tiny, nearly-full root filesystem isn't rewritten every few seconds; after
# a service restart a download simply shows no rate until the next sample.
SPEED_SAMPLES = {}
MIN_SAMPLE_SECONDS = 1.0
SPEED_SMOOTHING = 0.6  # weight given to the newest reading

def update_speed(dest, size, now=None):
    """Record a size sample for dest and return the smoothed bytes/second,
    or None while there isn't enough history to say."""
    now = time.monotonic() if now is None else now
    prev = SPEED_SAMPLES.get(dest)
    if prev is None or size < prev["size"]:
        # No history yet, or the file shrank because the download was
        # restarted - either way, begin a fresh baseline instead of
        # reporting a nonsensical (or negative) rate.
        SPEED_SAMPLES[dest] = {"t": now, "size": size, "bps": None}
        return None
    elapsed = now - prev["t"]
    if elapsed < MIN_SAMPLE_SECONDS:
        # Too soon to re-measure; keep showing the last figure so a quick
        # reload doesn't blank the column or divide by ~zero.
        return prev["bps"]
    raw = (size - prev["size"]) / elapsed
    bps = raw if prev["bps"] is None else (
        SPEED_SMOOTHING * raw + (1 - SPEED_SMOOTHING) * prev["bps"])
    SPEED_SAMPLES[dest] = {"t": now, "size": size, "bps": bps}
    return bps

def fmt_speed(bps):
    n = float(bps)
    for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
        if n < 1024 or unit == "GB/s":
            return f"{n:.0f} {unit}" if unit == "B/s" else f"{n:.1f} {unit}"
        n /= 1024

# Multi-part extensions must be treated as one unit, otherwise renaming
# "archive.tar.gz" would leave ".gz" and silently mangle the type.
COMPOUND_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")

def split_ext(name):
    """Split a filename into (stem, extension), honouring .tar.gz-style
    compound extensions."""
    lower = name.lower()
    for compound in COMPOUND_EXTENSIONS:
        if lower.endswith(compound) and len(name) > len(compound):
            return name[:-len(compound)], name[-len(compound):]
    return os.path.splitext(name)

def apply_rename(detected_name, requested_name):
    """Rename only the stem; the extension always comes from the detected
    name. If the user appends their own real-looking extension it is
    discarded (so the file type can't change), but dotted names like
    'holiday.2024' are kept intact - only a trailing segment that actually
    looks like a file extension is stripped."""
    if not requested_name:
        return detected_name
    _detected_stem, ext = split_ext(detected_name)
    requested = os.path.basename(requested_name).strip().strip(".")
    if not requested:
        return detected_name
    # Strip a trailing extension from the user's input only if it looks like a
    # genuine one (1-8 chars, letters/digits) - not arbitrary text after a dot.
    stem, user_ext = os.path.splitext(requested)
    if stem and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", user_ext or ""):
        requested = stem
    return requested + ext

def list_all_folders(root_dir):
    """Every existing folder under root_dir, as '/'-joined relative paths -
    populates the destination picker so saving into a folder you already
    made doesn't require typing its path by hand."""
    rels = []
    for dirpath, dirnames, _files in os.walk(root_dir):
        dirnames.sort(key=str.lower)
        rel = os.path.relpath(dirpath, root_dir).replace(os.sep, "/")
        if rel != ".":
            rels.append(rel)
    rels.sort(key=str.lower)
    return rels

def folder_options_html(root_dir):
    opts = ['<option value="">(no folder &mdash; save directly here)</option>']
    for rel in list_all_folders(root_dir):
        opts.append(f'<option value="{esc(rel)}">{esc(rel)}</option>')
    return "".join(opts)

def spawn_wget_loop(dest, url):
    """Launch the resumable-download shell loop for a given destination/URL.
    Shared by /add (first launch) and /resume (relaunch after a pause) so
    the two can't drift into different retry/resume behavior."""
    # -c resumes a partial file instead of restarting; the shell loop
    # retries on connection drops (bounded, so a permanently dead link
    # doesn't retry forever) while -c means each retry (or a manual resume
    # after a pause) picks up where the file already left off.
    wget_cmd = f"wget -c -q -O {shlex.quote(dest)} {shlex.quote(url)}"
    loop = (
        f"n=0; until {wget_cmd}; do "
        f"n=$((n+1)); [ $n -ge {MAX_RETRIES} ] && exit 1; "
        f"sleep {RETRY_DELAY_SECONDS}; done"
    )
    return subprocess.Popen(
        ["/bin/bash", "-c", loop],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid
    )

def safe_subdir(folder):
    """Turn user input into a subfolder path that can never escape
    DOWNLOAD_DIR (no absolute paths, no .. traversal)."""
    parts = []
    for part in str(folder).replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        parts.append(part)
    if not parts:
        return ""
    candidate = os.path.join(DOWNLOAD_DIR, *parts)
    root = os.path.realpath(DOWNLOAD_DIR)
    if os.path.realpath(candidate) != root and not os.path.realpath(candidate).startswith(root + os.sep):
        return ""
    return os.path.join(*parts)

# ------------------------------------------------------------------ theming
# The Desktop shell writes ls_theme / ls_accent cookies. Cookies are scoped by
# host and ignore the port, so one set by the shell on :8095 is sent here on
# :8092 as well - which is how the shell restyles an app it otherwise can't
# reach across the origin boundary.
VALID_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
DEFAULT_ACCENT = "#2E9B95"

def _shade(hex_color, factor):
    """Darken (factor<1) or lighten (factor>1) a #rrggbb colour."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    def clamp(v):
        return max(0, min(255, int(v)))
    return "#%02x%02x%02x" % (clamp(r * factor), clamp(g * factor), clamp(b * factor))

def _rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"

LIGHT_TOKENS = (
    "--panel:rgba(255,255,255,0.72);"
    "--panel-strong:rgba(255,255,255,0.97);"
    "--panel-line:rgba(255,255,255,0.55);"
    "--line:rgba(20,30,30,0.07);"
    "--line-strong:rgba(20,30,30,0.25);"
    "--field:rgba(255,255,255,0.90);"
    "--field-line:rgba(20,30,30,0.18);"
    "--text:#1a1a1a;"
    "--text-muted:#555555;"
    "--text-faint:#6a6a6a;"
    "--glow:rgba(255,255,255,0.9);"
    "--icon-shadow:rgba(0,0,0,0.35);"
    "--thumb-bg:rgba(0,0,0,0.06);"
    "--hover:rgba(20,30,30,0.06);"
    "--shadow-col:rgba(20,30,30,0.10);"
    "--danger:#c0392b;"
    "--danger-line:rgba(192,57,43,0.35);"
    "--danger-soft:rgba(192,57,43,0.08);"
    "--track:#e8e8e8;"
)

DARK_TOKENS = (
    "--panel:rgba(28,34,37,0.72);"
    "--panel-strong:rgba(22,27,30,0.97);"
    "--panel-line:rgba(255,255,255,0.10);"
    "--line:rgba(255,255,255,0.09);"
    "--line-strong:rgba(255,255,255,0.20);"
    "--field:rgba(255,255,255,0.07);"
    "--field-line:rgba(255,255,255,0.16);"
    "--text:#E7EDEC;"
    "--text-muted:#A8B8B6;"
    "--text-faint:#839593;"
    "--glow:rgba(0,0,0,0.85);"
    "--icon-shadow:rgba(0,0,0,0.65);"
    "--thumb-bg:rgba(255,255,255,0.08);"
    "--hover:rgba(255,255,255,0.08);"
    "--shadow-col:rgba(0,0,0,0.45);"
    "--danger:#EE8175;"
    "--danger-line:rgba(238,129,117,0.40);"
    "--danger-soft:rgba(238,129,117,0.15);"
    "--track:rgba(255,255,255,0.14);"
)

def theme_css(theme, accent):
    """Build the :root variable block for a theme + accent. The accent comes
    from a cookie, i.e. it is client-controlled, so it is validated against a
    strict hex pattern before being placed into CSS - anything else falls back
    to the default rather than being interpolated into the stylesheet."""
    if not VALID_HEX.match(accent or ""):
        accent = DEFAULT_ACCENT
    dark = (theme == "dark")
    # On dark surfaces the hover shade should get brighter, not darker.
    accent_dark = _shade(accent, 1.15 if dark else 0.85)
    common = (f"--accent:{accent};--accent-dark:{accent_dark};"
              f"--accent-soft:{_rgba(accent, 0.18)};")
    return ":root{" + common + (DARK_TOKENS if dark else LIGHT_TOKENS) + "}"

def theme_from_cookies(cookie_header):
    theme = parse_cookie(cookie_header, "ls_theme", "light")
    accent = parse_cookie(cookie_header, "ls_accent", DEFAULT_ACCENT)
    return theme_css(theme if theme in ("light", "dark") else "light", accent)

def parse_cookie(cookie_header, name, default=""):
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return urllib.parse.unquote(part[len(name) + 1:])
    return default

PAGE_TEMPLATE = """<!doctype html>
<html><head><title>Fetcher</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<style>
{theme_vars}
/* Form controls don't inherit colour by default, so on a dark field they'd
   render with the platform's black text and be unreadable. */
input,select,textarea{{color:var(--text);}}
option{{background:var(--panel-strong);color:var(--text);}}
/* Transparent page so the desktop wallpaper shows through the window behind
   it. Readability is preserved by putting all actual content on frosted-glass
   panels (translucent white + backdrop blur), which lighten whatever is behind
   them - so dark text stays legible over light AND dark wallpapers alike. */
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:1180px;margin:0 auto;padding:22px 18px 32px;background:transparent;color:var(--text);}}
.page-head{{background:var(--panel);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--panel-line);border-radius:8px;overflow:hidden;padding:12px 16px;margin-bottom:14px;}}
h1{{font-size:19px;margin:0 0 3px;}}
p.sub{{color:var(--text-muted);font-size:12.5px;margin:0;line-height:1.45;}}
/* Form on the left at ~1/3 width, the download list taking the remaining 2/3. */
.layout{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,2fr);gap:14px;align-items:start;}}
@media (max-width:820px){{.layout{{grid-template-columns:1fr;}}}}
form{{background:var(--panel);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--panel-line);padding:16px;border-radius:8px;overflow:hidden;box-shadow:0 2px 10px var(--shadow-col);margin:0;}}
label{{display:block;font-size:12px;font-weight:600;color:var(--text);margin-bottom:4px;margin-top:12px;}}
label:first-child{{margin-top:0;}}
input[type=text],select{{width:100%;padding:9px;border:1px solid var(--field-line);border-radius:5px;box-sizing:border-box;font-size:14px;background:var(--field);color:var(--text);}}
input[type=text]:focus,select:focus{{outline:2px solid var(--accent);outline-offset:1px;border-color:var(--accent);background:#fff;}}
select{{margin-bottom:6px;}}
button{{margin-top:16px;padding:10px 18px;background:var(--accent);color:#fff;border:none;border-radius:5px;font-weight:600;cursor:pointer;font-size:13px;width:100%;}}
button:hover{{background:var(--accent-dark);}}
.ext-note{{font-size:10.5px;color:var(--text-faint);margin-top:5px;line-height:1.4;}}
.folder-path{{font-family:monospace;font-size:10.5px;color:var(--text-faint);margin-top:5px;}}
/* A <table> with border-collapse can't reliably clip its own border-radius
   (a separate, well-known quirk from the backdrop-filter one above) - so the
   rounded frosted-glass look lives on this wrapper instead, and the table
   itself stays a plain, unstyled element inside it. */
.table-wrap{{background:var(--panel);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--panel-line);border-radius:8px;overflow:hidden;box-shadow:0 2px 10px var(--shadow-col);}}
table{{width:100%;border-collapse:collapse;background:transparent;}}
td,th{{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:13px;}}
tr:last-child td{{border-bottom:none;}}
th{{color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:0.03em;}}
.status-running{{color:#c98a1a;font-weight:600;}}
.status-done{{color:var(--accent);font-weight:600;}}
.status-error{{color:var(--danger);font-weight:600;}}
.status-paused{{color:#5CA4C4;font-weight:600;}}
/* Icon buttons for pause / continue / remove. Sized as proper touch targets
   and tinted from currentColor so they follow the theme and accent. */
.act{{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:6px;color:var(--text-muted);text-decoration:none;vertical-align:middle;transition:background 0.12s ease,color 0.12s ease;}}
.act + .act{{margin-left:4px;}}
.act:hover{{background:var(--hover);color:var(--accent);}}
.act-danger:hover{{background:var(--danger-soft);color:var(--danger);}}
.act:focus-visible{{outline:2px solid var(--accent);outline-offset:1px;}}
.tag{{font-size:10.5px;color:var(--text-faint);font-weight:400;}}
.progress-wrap{{display:flex;align-items:center;gap:7px;}}
.progress-track{{width:70px;height:6px;background:var(--track);border-radius:3px;overflow:hidden;flex:none;}}
.progress-fill{{height:100%;background:#c98a1a;border-radius:3px;}}
.progress-pct{{font-family:monospace;font-size:11.5px;color:#c98a1a;}}
/* Speed gets its own labelled column between Status and Started. Left
   aligned like every other column so the headings form one clean edge. */
.col-speed{{text-align:left;white-space:nowrap;width:96px;}}
.speed{{font-family:monospace;font-size:11.5px;color:var(--text-muted);white-space:nowrap;}}
.speed-wait{{opacity:0.6;font-style:italic;font-family:inherit;font-size:11px;}}
.speed-idle{{color:var(--text-faint);}}
/* "Preparing" overlay - covers the page while the server probes the link, so
   a slow response reads as work-in-progress instead of a dead button. */
.prep-overlay{{position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.28);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);}}
.prep-overlay[hidden]{{display:none;}}
.prep-card{{background:var(--panel-strong);border:1px solid var(--panel-line);border-radius:8px;overflow:hidden;padding:22px 26px;box-shadow:0 8px 28px var(--shadow-col);max-width:330px;text-align:center;}}
.prep-spinner{{width:26px;height:26px;margin:0 auto 12px;border-radius:50%;border:3px solid var(--field-line);border-top-color:var(--accent);animation:prep-spin 0.8s linear infinite;}}
@keyframes prep-spin{{to{{transform:rotate(360deg);}}}}
.prep-title{{font-size:14px;font-weight:600;color:var(--text);margin-bottom:5px;}}
.prep-sub{{font-size:12px;color:var(--text-muted);line-height:1.45;}}
@media (prefers-reduced-motion: reduce){{.prep-spinner{{animation:none;}}}}
</style></head>
<body>
<div class="page-head">
<h1>Fetcher</h1>
<p class="sub">Paste a link and go &mdash; the name and file type are detected automatically.</p>
</div>
<div class="layout">
<form method="POST" action="/add" id="add-form">
<label>Source URL</label>
<input type="text" name="url" placeholder="https://..." required>

<label>Folder <span class="tag">(optional)</span></label>
<select name="folder_select">{folder_options}</select>
<input type="text" name="folder_new" placeholder="or type a new folder, e.g. models/gguf">
<div class="folder-path">saves to {download_dir}/&lt;folder&gt;</div>

<label>Rename to <span class="tag">(optional)</span></label>
<input type="text" name="name" placeholder="leave blank to keep detected name">
<div class="ext-note">Name only &mdash; the file type is always kept from the source, so you can't accidentally break it.</div>

<button type="submit" id="add-btn">Download</button>
</form>
<div class="table-wrap">
<table>
<tr><th>Name</th><th>Status</th><th class="col-speed">Speed</th><th>Started</th><th></th></tr>
{rows}
</table>
</div>
</div>

<div class="prep-overlay" id="prep-overlay" hidden>
  <div class="prep-card">
    <div class="prep-spinner"></div>
    <div class="prep-title">Preparing download&hellip;</div>
    <div class="prep-sub">Checking the link and working out the file name. This takes a few seconds &mdash; no need to click again.</div>
  </div>
</div>

<script>
(function(){{
  var form = document.getElementById('add-form');
  var btn = document.getElementById('add-btn');
  var overlay = document.getElementById('prep-overlay');
  if(!form || !btn || !overlay) return;
  var submitted = false;
  form.addEventListener('submit', function(e){{
    // The server has to fetch the URL's headers before it can answer, which
    // takes a few seconds on this hardware. Without a signal the page just
    // looks frozen, so people click again - and used to get two downloads.
    if(submitted){{ e.preventDefault(); return; }}
    submitted = true;
    overlay.hidden = false;
    btn.textContent = 'Preparing\\u2026';
    // Deliberately NOT btn.disabled here: a disabled control is dropped from
    // the submission in some browsers, and the click is already swallowed by
    // the guard above.
    btn.setAttribute('aria-busy', 'true');
    btn.style.opacity = '0.6';
    btn.style.cursor = 'wait';
  }});
  // Coming back via the Back button restores the page from cache with the
  // overlay still up; reset it so the form is usable again.
  window.addEventListener('pageshow', function(){{
    submitted = false;
    overlay.hidden = true;
    btn.textContent = 'Download';
    btn.removeAttribute('aria-busy');
    btn.style.opacity = '';
    btn.style.cursor = '';
  }});

  // Live progress refresh, but only while the form is genuinely idle - never
  // while you're typing into it, mid-submit, or with text already entered.
  if(window.FETCHER_AUTOREFRESH){{
    setInterval(function(){{
      if(submitted) return;
      var active = document.activeElement;
      if(active && form.contains(active)) return;
      var dirty = ['url','name','folder_new'].some(function(n){{
        var el = form.querySelector('[name=' + n + ']');
        return el && el.value.trim() !== '';
      }});
      if(dirty) return;
      location.reload();
    }}, 3000);
  }}
}})();
</script>
</body></html>
"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # Lets the browser extension (a different origin: moz-extension://...)
        # actually read the response. Without this, Firefox's Same Origin
        # Policy blocks it even though the request reaches the server fine.
        self.send_header("Access-Control-Allow-Origin", "*")
        # Without this, Firefox can keep serving a stale cached page even
        # across closing and reopening the tab, making a real deploy look
        # like it never landed.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self.render()
        elif self.path.startswith("/cancel/"):
            idx = self.path.split("/cancel/")[1]
            state = load_state()
            try:
                idx = int(idx)
                item = state[idx]
                pid = item.get("pid")
                if pid and is_running(pid):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                del state[idx]
                save_state(state)
            except (ValueError, IndexError, ProcessLookupError):
                pass
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        elif self.path.startswith("/pause/"):
            idx = self.path.split("/pause/")[1]
            state = load_state()
            try:
                idx = int(idx)
                item = state[idx]
                pid = item.get("pid")
                if pid and is_running(pid):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                # The partial file and its record both stay - only the
                # process stops, so /resume can pick it back up with -c.
                item["pid"] = None
                item["paused"] = True
                save_state(state)
            except (ValueError, IndexError):
                pass
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        elif self.path.startswith("/resume/"):
            idx = self.path.split("/resume/")[1]
            state = load_state()
            try:
                idx = int(idx)
                item = state[idx]
                if item.get("paused") and item.get("url") and item.get("dest"):
                    proc = spawn_wget_loop(item["dest"], item["url"])
                    item["pid"] = proc.pid
                    item["paused"] = False
                    save_state(state)
            except (ValueError, IndexError):
                pass
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/add":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            fields = urllib.parse.parse_qs(body)
            url = fields.get("url", [""])[0].strip()
            name = fields.get("name", [""])[0].strip()
            # A typed new-folder path wins over the picker, so you're never
            # stuck if the folder you want doesn't exist yet.
            folder = (fields.get("folder_new", [""])[0].strip()
                      or fields.get("folder_select", [""])[0].strip())
            if url and (url.startswith("http://") or url.startswith("https://")):
                detected_name, source, total_size = probe_url(url)
                final_name = apply_rename(detected_name, name)
                if name and final_name != detected_name:
                    source = "renamed"
                safe_name = os.path.basename(final_name)
                subdir = safe_subdir(folder)
                dest_dir = os.path.join(DOWNLOAD_DIR, subdir) if subdir else DOWNLOAD_DIR
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                except OSError:
                    dest_dir, subdir = DOWNLOAD_DIR, ""
                dest = os.path.join(dest_dir, safe_name)
                state = load_state()
                # Guard against starting a second download of the same file.
                # probe_url() above needs a few seconds on this hardware, and
                # this server handles one request at a time, so a page that
                # looks frozen invites a second click - which used to queue a
                # second identical POST behind the first. Two wget processes
                # sharing one -c destination would interleave writes into the
                # same file, so this refuses rather than corrupting it.
                # Requests are serialised, so this check-then-append is safe.
                already = any(
                    item.get("dest") == dest and
                    (item.get("paused") or is_running(item.get("pid")))
                    for item in state
                )
                if already:
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                proc = spawn_wget_loop(dest, url)
                state.append({
                    "name": safe_name,
                    "url": url,
                    "pid": proc.pid,
                    "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "dest": dest,
                    "source": source,
                    "total_size": total_size,
                    "folder": subdir,
                    "paused": False
                })
                save_state(state)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def render(self):
        state = load_state()
        rows = ""
        any_running = False
        for i, item in reversed(list(enumerate(state))):
            pid = item.get("pid")
            dest = item.get("dest", "")
            running = is_running(pid)
            current_size = os.path.getsize(dest) if os.path.exists(dest) else 0
            if running:
                any_running = True
                total_size = item.get("total_size")
                bps = update_speed(dest, current_size)
                speed_cell = (f'<span class="speed">{fmt_speed(bps)}</span>'
                              if bps is not None else
                              '<span class="speed speed-wait">measuring&hellip;</span>')
                if total_size:
                    pct = min(100, round(current_size / total_size * 100))
                    status = (f'<span class="status-running">downloading</span>'
                              f'<div class="progress-wrap">'
                              f'<div class="progress-track"><div class="progress-fill" style="width:{pct}%"></div></div>'
                              f'<span class="progress-pct">{pct}%</span></div>')
                else:
                    status = '<span class="status-running">downloading&hellip; (size unknown)</span>'
                actions = (icon_action(f"/pause/{i}", ICON_PAUSE, "Pause")
                           + icon_action(f"/cancel/{i}", ICON_TRASH,
                                         "Stop and remove", danger=True))
            elif item.get("paused"):
                status = '<span class="status-paused">paused</span>'
                speed_cell = '<span class="speed-idle">&mdash;</span>'
                actions = (icon_action(f"/resume/{i}", ICON_PLAY, "Continue")
                           + icon_action(f"/cancel/{i}", ICON_TRASH,
                                         "Remove from list", danger=True))
            elif current_size > 0:
                status = '<span class="status-done">done</span>'
                speed_cell = '<span class="speed-idle">&mdash;</span>'
                actions = icon_action(f"/cancel/{i}", ICON_TRASH,
                                      "Remove from list", danger=True)
            else:
                status = '<span class="status-error">failed</span>'
                speed_cell = '<span class="speed-idle">&mdash;</span>'
                actions = icon_action(f"/cancel/{i}", ICON_TRASH,
                                      "Remove from list", danger=True)
            source = item.get("source", "manual")
            source_tag = {
                "server": '<span class="tag">(from server)</span>',
                "url": '<span class="tag">(from link)</span>',
                "fallback": '<span class="tag">(generic &mdash; server gave no name)</span>',
                "renamed": '<span class="tag">(renamed)</span>',
            }.get(source, "")
            folder = item.get("folder", "")
            folder_tag = (f'<div class="folder-path">in {esc(folder)}/</div>'
                          if folder else "")
            rows += (f"<tr><td>{esc(item['name'])} {source_tag}{folder_tag}</td><td>{status}</td>"
                     f'<td class="col-speed">{speed_cell}</td>'
                     f"<td>{esc(item['started'])}</td><td>{actions}</td></tr>")
        # Forget speed samples for downloads that are no longer listed, so a
        # long-lived process doesn't accumulate entries for removed items.
        live_dests = {item.get("dest") for item in state}
        for gone in set(SPEED_SAMPLES) - live_dests:
            del SPEED_SAMPLES[gone]

        # A blunt <meta refresh> would reload mid-typing and wipe the URL you
        # were pasting. Flag it for the script instead, which reloads only
        # when the form is genuinely idle.
        refresh = '<script>window.FETCHER_AUTOREFRESH=1;</script>' if any_running else ""
        html = PAGE_TEMPLATE.format(
            rows=rows or "<tr><td colspan=5>No downloads yet.</td></tr>",
            refresh=refresh,
            download_dir=esc(DOWNLOAD_DIR),
            folder_options=folder_options_html(DOWNLOAD_DIR),
            theme_vars=theme_from_cookies(self.headers.get("Cookie", ""))
        )
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

class ReusableServer(socketserver.TCPServer):
    allow_reuse_address = True

if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    with ReusableServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()
