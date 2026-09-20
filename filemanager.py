#!/usr/bin/env python3
import http.server
import socketserver
import os
import re
import json
import shutil
import mimetypes
import zipfile
import tarfile
import urllib.parse
import hashlib
import hmac
import time
import errno
import itertools
import socket
import threading
from datetime import datetime
from email.utils import formatdate

CONFIG_FILE = "/etc/legasynas.conf"

def _load_config_file():
    cfg = {}
    try:
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip()
    except OSError:
        pass
    return cfg

_FILE_CONFIG = _load_config_file()

def config(key, default):
    """Env var wins (a systemd unit or manual override), then the shared
    config file, then this hardcoded default - so nothing changes for anyone
    who hasn't touched /etc/legasynas.conf."""
    return os.environ.get(key, _FILE_CONFIG.get(key, default))

ROOT_DIR = config("ROOT_DIR", "/var/downloads")
PORT = int(config("PORT_FILES", "8093"))

# ---------- optional login gate ----------
# A separate file (not the main config) so it can be locked down to 0600 -
# it holds a password hash and the secret used to sign session cookies.
# Auth stays off (everything works exactly as before) until install.sh's
# auth step actually creates this file with a real password.
AUTH_FILE = "/etc/legasynas-auth.conf"

def _load_auth_file():
    cfg = {}
    try:
        with open(AUTH_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cfg[key.strip()] = value.strip()
    except OSError:
        pass
    return cfg

# Read fresh on every call rather than cached at startup - this is what lets
# a password change made through serve.py (the only one of the three with a
# change-password control) take effect here immediately, with no restart
# needed.
def auth_enabled():
    return bool(_load_auth_file().get("AUTH_HASH"))

SESSION_COOKIE = "legasynas_session"
SESSION_LIFETIME = 60 * 60 * 24 * 14  # 14 days

def _sign(expiry, auth):
    # Session "tokens" are self-verifying (expiry + HMAC of that expiry using
    # a secret shared by all three services), not looked up in a store - the
    # three services are independent processes with no shared memory, and a
    # signed value lets each one verify a cookie set by either of the others
    # with no IPC or shared file to keep in sync on every request.
    return hmac.new(auth.get("AUTH_SECRET", "").encode(), str(expiry).encode(), hashlib.sha256).hexdigest()

def make_session_cookie():
    auth = _load_auth_file()
    expiry = int(time.time()) + SESSION_LIFETIME
    return f"{expiry}.{_sign(expiry, auth)}"

def verify_session_cookie(value):
    if not value or "." not in value:
        return False
    expiry_s, _, sig = value.partition(".")
    try:
        expiry = int(expiry_s)
    except ValueError:
        return False
    if expiry < time.time():
        return False
    return hmac.compare_digest(sig, _sign(expiry, _load_auth_file()))

def check_password(password, auth=None):
    auth = auth if auth is not None else _load_auth_file()
    try:
        salt = bytes.fromhex(auth.get("AUTH_SALT", ""))
    except ValueError:
        return False
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000).hex()
    return hmac.compare_digest(computed, auth.get("AUTH_HASH", ""))

def get_cookie(handler, name):
    header = handler.headers.get("Cookie", "")
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return None

def is_authenticated(handler):
    if not auth_enabled():
        return True
    return verify_session_cookie(get_cookie(handler, SESSION_COOKIE))

# Same palette the Desktop shell's own dark/light theme uses (see
# desktop.html's :root[data-theme] blocks) - kept in sync by eye since this
# is a plain string template, not shared CSS.
LOGIN_COLORS = {
    "light": {
        "page_bg": "#dff4f1", "card_bg": "#ffffff", "text": "#1f2b2a",
        "input_bg": "#ffffff", "input_border": "#ccc", "shadow": "rgba(0,0,0,.12)",
    },
    "dark": {
        "page_bg": "#16211f", "card_bg": "#212c2a", "text": "#E7EDEC",
        "input_bg": "#293532", "input_border": "#3a4644", "shadow": "rgba(0,0,0,.45)",
    },
}

LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><title>Sign in</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;background:{page_bg};display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}}
form{{background:{card_bg};padding:32px;border-radius:14px;box-shadow:0 8px 28px {shadow};width:280px}}
h2{{margin:0 0 16px;color:{text}}}
input{{width:100%;box-sizing:border-box;padding:10px;margin:6px 0;border:1px solid {input_border};
border-radius:8px;font-size:14px;background:{input_bg};color:{text}}}
button{{width:100%;padding:10px;margin-top:8px;background:#2E9B95;color:#fff;border:0;
border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}}
button:hover{{background:#278a85}}
.err{{color:#D8564A;font-size:13px;margin-bottom:4px}}
</style>
<form method="POST" action="/login">
<h2>Sign in</h2>
{error}
<input name="username" placeholder="Username" autocomplete="username" autofocus>
<input name="password" type="password" placeholder="Password" autocomplete="current-password">
<button type="submit">Sign in</button>
</form>"""

def send_login_page(handler, failed=False):
    error_html = '<div class="err">Incorrect username or password.</div>' if failed else ""
    # Dark is the standard now, same as the Desktop shell itself - "light"
    # only shows up here if that's what the ls_theme cookie explicitly says
    # (set by the shell, and readable here since cookies aren't port-scoped).
    theme = "light" if get_cookie(handler, "ls_theme") == "light" else "dark"
    body = LOGIN_PAGE.format(error=error_html, **LOGIN_COLORS[theme]).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

def handle_login_post(handler):
    length = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(length).decode("utf-8", "replace")
    fields = urllib.parse.parse_qs(body)
    username = fields.get("username", [""])[0]
    password = fields.get("password", [""])[0]
    auth = _load_auth_file()
    ok = hmac.compare_digest(username, auth.get("AUTH_USER", "")) and check_password(password, auth)
    if not ok:
        time.sleep(1)  # slow down automated guessing
        send_login_page(handler, failed=True)
        return
    cookie = make_session_cookie()
    handler.send_response(303)
    handler.send_header("Location", "/")
    handler.send_header(
        "Set-Cookie",
        f"{SESSION_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_LIFETIME}")
    handler.send_header("Content-Length", "0")
    handler.end_headers()

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

def fmt_bytes(n):
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024

def safe_path(rel):
    """Resolve a client-supplied relative path to a real path that can never
    escape ROOT_DIR, no matter what's in it (.., absolute paths, symlinks)."""
    parts = []
    for part in str(rel or "").replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        parts.append(part)
    candidate = os.path.join(ROOT_DIR, *parts) if parts else ROOT_DIR
    root_real = os.path.realpath(ROOT_DIR)
    cand_real = os.path.realpath(candidate)
    if cand_real != root_real and not cand_real.startswith(root_real + os.sep):
        return ROOT_DIR, ""
    return cand_real, "/".join(parts)

def sort_entries(entries, sort_key, direction):
    """Folders always stay pinned above files (matches every mainstream file
    manager); the chosen column/direction only orders within each group."""
    reverse = (direction == "desc")
    def key_fn(e):
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            st = None
        if sort_key == "size":
            return st.st_size if st else 0
        if sort_key == "modified":
            return st.st_mtime if st else 0
        return e.name.lower()
    dirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
    files = [e for e in entries if not e.is_dir(follow_symlinks=False)]
    dirs.sort(key=key_fn, reverse=reverse)
    files.sort(key=key_fn, reverse=reverse)
    return dirs + files

def is_image(name):
    ctype, _ = mimetypes.guess_type(name)
    return bool(ctype) and ctype.startswith("image/")

def is_previewable(name):
    ctype, _ = mimetypes.guess_type(name)
    if not ctype:
        return False
    return ctype.startswith(("image/", "video/", "audio/")) or ctype in (
        "application/pdf", "text/plain")

# Compound archive extensions handled explicitly so ".tar.gz" isn't split at
# the wrong dot and so extracting names the output folder "photos" not
# "photos.tar".
ARCHIVE_COMPOUND = (".tar.gz", ".tar.bz2", ".tar.xz")

def archive_kind(name):
    lower = name.lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith((".tar.gz", ".tgz")):
        return "tar:gz"
    if lower.endswith((".tar.bz2", ".tbz2")):
        return "tar:bz2"
    if lower.endswith((".tar.xz", ".txz")):
        return "tar:xz"
    if lower.endswith(".tar"):
        return "tar:"
    return None

def archive_stem(name):
    lower = name.lower()
    for c in ARCHIVE_COMPOUND:
        if lower.endswith(c):
            return name[:-len(c)]
    return os.path.splitext(name)[0]

def _check_member_safe(target, dest_real):
    real = os.path.realpath(target)
    if real != dest_real and not real.startswith(dest_real + os.sep):
        raise ValueError("archive entry escapes destination: " + target)

def extract_archive(real_path, dest_dir):
    """Extract a zip/tar archive, rejecting any member whose path would land
    outside dest_dir (the classic 'zip slip' path-traversal attack)."""
    kind = archive_kind(real_path)
    if kind is None:
        return False
    os.makedirs(dest_dir, exist_ok=True)
    dest_real = os.path.realpath(dest_dir)
    if kind == "zip":
        with zipfile.ZipFile(real_path) as zf:
            for member in zf.infolist():
                _check_member_safe(os.path.join(dest_dir, member.filename), dest_real)
            zf.extractall(dest_dir)
    else:
        mode = "r:" + kind.split(":", 1)[1]
        with tarfile.open(real_path, mode) as tf:
            for member in tf.getmembers():
                _check_member_safe(os.path.join(dest_dir, member.name), dest_real)
            tf.extractall(dest_dir)
    return True

def safe_upload_name(filename):
    """Sanitize a client-supplied upload filename that may carry a relative
    path (folder uploads send 'holiday/2026/img.jpg'). Keeps the folder
    structure but drops anything that could escape the destination."""
    parts = []
    for part in str(filename or "").replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        parts.append(part)
    return "/".join(parts)

class _BoundaryReader:
    """Reads a multipart/form-data body from a file-like object a chunk at a
    time - never buffers more than one 64KB chunk, which is what makes it
    safe to stream a multi-GB movie or model checkpoint straight to disk on
    a box with only 256MB of RAM."""
    CHUNK = 65536

    def __init__(self, rfile, remaining):
        self.rfile = rfile
        self.remaining = remaining
        self.buf = b""

    def _fill(self):
        if self.remaining <= 0:
            return False
        data = self.rfile.read(min(self.CHUNK, self.remaining))
        if not data:
            self.remaining = 0
            return False
        self.remaining -= len(data)
        self.buf += data
        return True

    def read_line(self):
        while b"\r\n" not in self.buf:
            if not self._fill():
                line, self.buf = self.buf, b""
                return line
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def read_until(self, delim, sink=None):
        """Consume bytes up to (not including) the next `delim`, feeding
        them to sink() as they arrive so a file part can be written straight
        to disk instead of accumulating in memory. Leaves self.buf
        positioned right after the delimiter. Returns False if the stream
        ended before delim was found."""
        keep = len(delim) + 2
        while True:
            idx = self.buf.find(delim)
            if idx != -1:
                if sink:
                    sink(self.buf[:idx])
                self.buf = self.buf[idx + len(delim):]
                return True
            if len(self.buf) > keep:
                if sink:
                    sink(self.buf[:-keep])
                self.buf = self.buf[-keep:]
            if not self._fill():
                if sink and self.buf:
                    sink(self.buf)
                self.buf = b""
                return False

def stream_multipart(rfile, content_length, content_type, resolve_dest_dir):
    """Stream-parses a multipart/form-data body straight from the socket.
    File parts are written directly to disk in the directory returned by
    resolve_dest_dir(path_value); file content is never held in memory
    beyond one chunk, regardless of how large the upload is.

    Assumes the hidden 'path' field arrives before any 'file' part, which
    holds for our own upload form since the path input is written before
    the file input in the page markup (browsers serialize multipart bodies
    in DOM order). Returns (saved_filenames, total_bytes_written)."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not m:
        rfile.read(content_length)
        return [], 0
    boundary = (m.group(1) or m.group(2)).strip()
    first_delim = ("--" + boundary).encode()
    part_delim = ("\r\n--" + boundary).encode()

    br = _BoundaryReader(rfile, content_length)
    if not br.read_until(first_delim):
        return [], 0
    br.read_line()  # trailing \r\n right after the first boundary marker

    dest_dir = None
    saved = []
    total = 0
    while True:
        headers = []
        while True:
            line = br.read_line()
            if line == b"":
                break
            headers.append(line.decode(errors="replace"))
        header_blob = "\n".join(headers)
        disp_m = re.search(r'name="([^"]*)"(?:;\s*filename="([^"]*)")?', header_blob)
        field_name = disp_m.group(1) if disp_m else ""
        filename = disp_m.group(2) if disp_m else None

        if filename:
            if dest_dir is None:
                dest_dir = resolve_dest_dir("")
            # A folder upload sends each file with its path relative to the
            # chosen folder ("holiday/2026/img.jpg"), so the structure has to
            # be recreated rather than flattened - but every segment is still
            # client-supplied, so it gets the same ..-stripping treatment as
            # any other path before being trusted.
            safe_name = safe_upload_name(filename)
            if safe_name:
                target = os.path.join(dest_dir, safe_name)
                parent = os.path.dirname(target)
                real_root = os.path.realpath(dest_dir)
                if os.path.realpath(parent) == real_root or \
                        os.path.realpath(parent).startswith(real_root + os.sep):
                    os.makedirs(parent, exist_ok=True)
                else:
                    safe_name = None
            f = open(os.path.join(dest_dir, safe_name), "wb") if safe_name else None
            written = 0
            def _sink(chunk, _f=f):
                nonlocal written
                if _f:
                    _f.write(chunk)
                written += len(chunk)
            more = br.read_until(part_delim, sink=_sink)
            if f:
                f.close()
            if safe_name and written > 0:
                saved.append(safe_name)
            total += written
        elif field_name == "path":
            chunks = []
            more = br.read_until(part_delim, sink=chunks.append)
            dest_dir = resolve_dest_dir(b"".join(chunks).decode(errors="replace"))
        else:
            more = br.read_until(part_delim, sink=None)

        if not more:
            break
        while len(br.buf) < 2 and br._fill():
            pass
        if br.buf[:2] == b"--":
            break
        br.read_line()

    return saved, total

# ---------- background jobs ----------
# Copying, moving or zipping a multi-GB item takes minutes on this hardware.
# Done inside the HTTP request it froze this whole (single-threaded) server
# for that long - and every failure was swallowed by a bare "except OSError:
# pass", so a copy that died halfway just left a truncated file and no
# message at all. Each of those now runs as a job on its own thread: it
# reports progress, and it records what actually went wrong.
JOBS = {}
JOBS_LOCK = threading.Lock()
_JOB_IDS = itertools.count(1)
JOB_KEEP_SECONDS = 15 * 60

def _prune_jobs():
    now = time.time()
    with JOBS_LOCK:
        for jid in [i for i, j in JOBS.items()
                    if j["state"] != "running" and now - j["finished"] > JOB_KEEP_SECONDS]:
            del JOBS[jid]

def job_start(label, worker, wait=0.5):
    """Run worker(job) on a daemon thread and return the job.

    Waits up to `wait` seconds for it, so instant work (a same-filesystem
    move is only a rename) is already finished by the time the page reloads,
    while slow work returns at once and is followed through the progress
    strip instead."""
    job = {"id": next(_JOB_IDS), "label": label, "state": "running",
           "done": 0, "total": 0, "error": "", "notes": [],
           "started": time.time(), "finished": 0.0}

    def run():
        try:
            worker(job)
        except Exception as e:  # anything the worker didn't already report
            job["error"] = job["error"] or str(e) or e.__class__.__name__
        # State goes last: the progress strip treats it as the "finished" signal.
        job["finished"] = time.time()
        job["state"] = "error" if job["error"] else "done"

    with JOBS_LOCK:
        JOBS[job["id"]] = job
    t = threading.Thread(target=run, daemon=True)
    t.start()
    if wait:
        t.join(wait)
    _prune_jobs()
    return job

def jobs_snapshot():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["id"])
        return [{**{k: j[k] for k in ("id", "label", "state", "done", "total", "error")},
                 "notes": list(j["notes"])} for j in jobs]

def tree_size(path):
    """Bytes under path (a file or a folder), not following symlinks."""
    if os.path.islink(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
    return total

# FAT32 - how most USB sticks and external drives are sold - cannot hold a file
# of 4 GiB or more. A video or model file blows through that limit partway
# into the copy and dies with a bare "File too large", minutes in; so it is
# checked before anything is copied.
FAT_MAX_FILE = 4 * 1024 ** 3 - 1
_FAT_TYPES = ("vfat", "msdos", "fat")

def fs_type_of(path):
    """Filesystem type (as /proc/mounts names it) of whatever holds `path`."""
    real = os.path.realpath(path)
    best, best_type = "", None
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = (parts[1].replace("\\040", " ").replace("\\011", "\t")
                       .replace("\\012", "\n").replace("\\134", "\\"))
                inside = mnt == "/" or real == mnt or real.startswith(mnt.rstrip("/") + "/")
                if inside and len(mnt) >= len(best):
                    best, best_type = mnt, parts[2]
    except OSError:
        return None
    return best_type

def largest_file(path):
    """Size of the biggest single file at or under path (symlinks skipped)."""
    if os.path.islink(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    biggest = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                try:
                    biggest = max(biggest, os.path.getsize(p))
                except OSError:
                    pass
    return biggest

def _copy_data(fin, fout, job, chunk=8 * 1024 * 1024):
    """Copy all of fin into fout, adding to job["done"] as it goes.

    Uses sendfile - the kernel moves the data itself, so an 800MHz core isn't
    shovelling every byte through Python - and drops to a plain read/write
    loop on a filesystem that refuses it."""
    infd, outfd = fin.fileno(), fout.fileno()
    size = os.fstat(infd).st_size
    offset = 0
    try:
        while offset < size:
            sent = os.sendfile(outfd, infd, offset, min(chunk, size - offset))
            if sent == 0:
                break
            offset += sent
            job["done"] += sent
    except OSError as e:
        if e.errno not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise
        fin.seek(offset)
        fout.seek(offset)
        while True:
            buf = fin.read(1024 * 1024)
            if not buf:
                break
            fout.write(buf)
            job["done"] += len(buf)
        return
    if offset != size:
        raise OSError(errno.EIO, "the source file changed while it was being copied")

def copy_file(src, dst, job):
    """Copy src to dst via dst + ".part", renamed into place only once it is
    complete and flushed to the device. A copy that dies partway therefore
    never leaves a truncated file wearing the real name (what the old code
    did), and "done" means the bytes are really on the disk or stick."""
    part = dst + ".part"
    try:
        with open(src, "rb") as fin, open(part, "wb") as fout:
            _copy_data(fin, fout, job)
            fout.flush()
            os.fsync(fout.fileno())
        try:
            shutil.copystat(src, part)
        except OSError:
            pass  # FAT/exFAT can't hold Unix permissions; the data is what matters
        os.replace(part, dst)
    except BaseException:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise

def copy_tree(src, dst, job):
    """Copy a folder, skipping symlinks (following one could read outside the
    NAS root). Removes the half-made copy if anything fails."""
    real_src, real_dst = os.path.realpath(src), os.path.realpath(dst)
    if real_dst == real_src or real_dst.startswith(real_src + os.sep):
        raise OSError(errno.EINVAL, "can't copy a folder into itself")
    os.makedirs(dst)
    try:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            rel = os.path.relpath(root, src)
            out = dst if rel == "." else os.path.join(dst, rel)
            for d in dirs:
                os.makedirs(os.path.join(out, d), exist_ok=True)
            for name in files:
                sp = os.path.join(root, name)
                if not os.path.islink(sp):
                    copy_file(sp, os.path.join(out, name), job)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise

def do_bulk_move_or_copy(sels, dest_input, op):
    """Start a move/copy job and return it."""
    verb = "Moving" if op == "move" else "Copying"
    if len(sels) == 1:
        label = f"{verb} {os.path.basename(sels[0].rstrip('/'))}"
    else:
        label = f"{verb} {len(sels)} items"

    def work(job):
        dest_real, _dest_rel = safe_path(dest_input)
        os.makedirs(dest_real, exist_ok=True)
        plan, targets = [], set()
        for rel in sels:
            real, relp = safe_path(rel)
            if not relp or not os.path.exists(real):
                continue
            name = os.path.basename(real)
            target = os.path.join(dest_real, name)
            if os.path.exists(target) or target in targets:
                job["notes"].append(f"Skipped {name} - already in the destination")
                continue  # skip conflicts rather than silently clobber
            targets.add(target)
            plan.append((real, target))
        # A move within one filesystem is only a rename; just the work that has
        # to copy bytes needs sizing up and checking against the free space.
        dest_dev = os.stat(dest_real).st_dev
        must_copy = lambda real: op == "copy" or os.stat(real).st_dev != dest_dev
        problems = []
        if fs_type_of(dest_real) in _FAT_TYPES:
            keep = []
            for real, target in plan:
                if must_copy(real) and largest_file(real) > FAT_MAX_FILE:
                    problems.append(
                        f"{os.path.basename(real)} is over 4 GB, the most a FAT32 drive can hold - "
                        f"reformat the drive as exFAT or ext4 to store files this big")
                else:
                    keep.append((real, target))
            plan = keep
        need = sum(tree_size(real) for real, _t in plan if must_copy(real))
        job["total"] = need
        if need:
            free = shutil.disk_usage(dest_real).free
            if need > free:
                job["error"] = "; ".join(
                    [f"Not enough free space at the destination "
                     f"({fmt_bytes(need)} needed, {fmt_bytes(free)} free)."] + problems)
                return
        failed = list(problems)
        for real, target in plan:
            name = os.path.basename(real)
            try:
                if op == "move":
                    try:
                        os.rename(real, target)
                        continue
                    except OSError as e:
                        if e.errno != errno.EXDEV:
                            raise
                if os.path.isdir(real):
                    copy_tree(real, target, job)
                else:
                    copy_file(real, target, job)
                if op == "move":
                    # Only now that the copy is complete is the original removed.
                    if os.path.isdir(real):
                        shutil.rmtree(real)
                    else:
                        os.remove(real)
            except OSError as e:
                failed.append(f"{name} ({e.strerror or e})")
        if failed:
            job["error"] = "Could not finish: " + "; ".join(failed)

    return job_start(label, work)

# Already-compressed formats: deflating them burns CPU on this box for no size
# gain, so they go into the zip as-is.
_STORE_EXTS = {".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".iso",
               ".zip", ".7z", ".rar", ".gz", ".xz", ".bz2", ".zst",
               ".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf",
               ".mp4", ".mkv", ".webm", ".mov", ".avi",
               ".mp3", ".flac", ".aac", ".ogg", ".m4a"}

def do_bulk_compress(cur_real, sels, archive_name):
    """Start a zip job and return it."""
    name = os.path.basename(archive_name) or (
        "archive-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    if not name.lower().endswith(".zip"):
        name += ".zip"
    zip_path = os.path.join(cur_real, name)

    def work(job):
        items = []
        for rel in sels:
            real, relp = safe_path(rel)
            if relp and os.path.exists(real):
                items.append(real)
        job["total"] = sum(tree_size(p) for p in items)
        part = zip_path + ".part"

        def add(zf, fp, arc):
            if os.path.islink(fp):
                return
            info = zipfile.ZipInfo.from_file(fp, arc)
            info.compress_type = (zipfile.ZIP_STORED
                                  if os.path.splitext(fp)[1].lower() in _STORE_EXTS
                                  else zipfile.ZIP_DEFLATED)
            with open(fp, "rb") as src, zf.open(info, "w") as dst:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    dst.write(buf)
                    job["done"] += len(buf)

        try:
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zf:
                for real in items:
                    base = os.path.basename(real)
                    if os.path.isdir(real):
                        for root, dirs, fnames in os.walk(real):
                            dirs[:] = [d for d in dirs
                                       if not os.path.islink(os.path.join(root, d))]
                            for fn in fnames:
                                fp = os.path.join(root, fn)
                                add(zf, fp, os.path.join(base, os.path.relpath(fp, real)))
                    else:
                        add(zf, real, base)
            os.replace(part, zip_path)
        except BaseException:
            try:
                os.unlink(part)
            except OSError:
                pass
            raise

    return job_start(f"Zipping {len(sels)} item{'' if len(sels) == 1 else 's'} into {name}", work)

def list_all_folders(root_dir):
    """Every existing folder under root_dir, as '/'-joined relative paths -
    populates the destination picker so moving/copying into a folder you
    already made doesn't require typing its path by hand."""
    rels = []
    for dirpath, dirnames, _files in os.walk(root_dir):
        dirnames.sort(key=str.lower)
        rel = os.path.relpath(dirpath, root_dir).replace(os.sep, "/")
        if rel != ".":
            rels.append(rel)
    rels.sort(key=str.lower)
    return rels

def folder_options_html(root_dir):
    opts = ['<option value="">(NAS root)</option>']
    for rel in list_all_folders(root_dir):
        opts.append(f'<option value="{esc(rel)}">{esc(rel)}</option>')
    return "".join(opts)

# Small/Medium/Large govern both the inline list-view thumbnail size and the
# tiles-view tile size, so bumping to "Large" actually gives the bigger
# photo previews that prompted this - not just a cosmetic label change.
THUMB_PX = {"small": 30, "medium": 64, "large": 120}

# Both icons share one square 24x24 canvas so every icon renders at exactly
# the same width AND height no matter which glyph it is - fitting the same
# bounding box (the previous approach) still let a wide-and-short folder
# come out visibly shorter than a tall-and-narrow file at the same nominal
# size. Each glyph keeps its own native proportions (no stretching) and is
# just centered within the shared square via a translate offset, the same
# way real icon sets keep a fixed slot size regardless of the glyph's shape.
ICON_CANVAS = 24

def folder_svg(size=19, css_class="icon"):
    cls = f' class="{css_class}"' if css_class else ""
    # Native art is 24x20 - centered with 2px of padding top and bottom.
    return (
        f'<svg{cls} width="{size}" height="{size}" viewBox="0 0 {ICON_CANVAS} {ICON_CANVAS}" '
        'xmlns="http://www.w3.org/2000/svg">'
        '<g transform="translate(0,2)">'
        '<path d="M2 4a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 '
        '2 0 0 1-2-2V4z" fill="#FBC02D"/>'
        '<path d="M2 4a2 2 0 0 1 2-2h5l2 2H4a2 2 0 0 0-2 2V4z" fill="#F2A600"/>'
        '</g></svg>'
    )

# A small flash-drive silhouette (metal connector + plastic body + accent
# stripe) so a mounted USB stick reads visually distinct from a plain folder,
# rather than showing as just another yellow folder next to real ones.
def usb_svg(size=19, css_class="icon"):
    cls = f' class="{css_class}"' if css_class else ""
    # Native art is 22x10 - centered the same way as folder/file above.
    return (
        f'<svg{cls} width="{size}" height="{size}" viewBox="0 0 {ICON_CANVAS} {ICON_CANVAS}" '
        'xmlns="http://www.w3.org/2000/svg">'
        '<g transform="translate(1,7)">'
        '<rect x="0" y="3" width="6" height="4" fill="#9AA0A6"/>'
        '<rect x="6" y="0" width="16" height="10" rx="2" fill="#CFD3D6" '
        'stroke="#9AA0A6" stroke-width="0.6"/>'
        '<rect x="9" y="3" width="10" height="4" rx="1" fill="#4FA3E3"/>'
        '</g></svg>'
    )

# Plain document with a folded corner, in flat grey, as a quieter counterpart
# to the folder icon.
def file_svg(size=13, css_class="icon"):
    cls = f' class="{css_class}"' if css_class else ""
    # Native art is 14x18 - centered with 5px padding left/right, 3px top/bottom.
    return (
        f'<svg{cls} width="{size}" height="{size}" viewBox="0 0 {ICON_CANVAS} {ICON_CANVAS}" '
        'xmlns="http://www.w3.org/2000/svg">'
        '<g transform="translate(5,3)">'
        '<path d="M2 1H9L13 5V17H2V1Z" fill="#E4E4E4" stroke="#9a9a9a" '
        'stroke-width="1"/>'
        '<path d="M9 1V5H13L9 1Z" fill="#cfcfcf"/>'
        '</g></svg>'
    )

# ------------------------------------------------------------------ theming
# The Desktop shell writes ls_theme / ls_accent cookies. Cookies are scoped by
# host and ignore the port, so one set by the shell on :8095 is sent here on
# :8093 as well - which is how the shell restyles an app it otherwise can't
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

PAGE = """<!doctype html>
<html><head><title>Files</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
{theme_vars}
/* Form controls don't inherit colour by default, so on a dark field they'd
   render with the platform's black text and be unreadable. */
input,select,textarea{{color:var(--text);}}
option{{background:var(--panel-strong);color:var(--text);}}
/* Full width rather than a centred fixed-width column: this page is embedded
   in a window that can be far wider than 960px, and centring left big empty
   margins while squeezing the icon grid into the middle. */
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:18px 20px 40px;background:transparent;color:var(--text);}}
/* Text and icons that float directly on the transparent page (no card
   behind them) get a soft light glow instead - it keeps dark text/icons
   readable over any wallpaper without drawing a visible box edge. */
.glow{{text-shadow:0 0 6px var(--glow),0 0 2px var(--glow);}}
.crumbs{{font-size:12.5px;color:var(--text);margin-bottom:12px;}}
.crumbs,.crumbs a{{text-shadow:0 0 6px var(--glow),0 0 2px var(--glow);}}
.crumbs a{{color:var(--accent-dark);text-decoration:none;}}
.crumbs a:hover{{text-decoration:underline;}}
.up-link{{display:inline-block;font-weight:600;margin-right:10px;padding-right:10px;border-right:1px solid var(--line-strong);}}
.toolbar{{margin-bottom:14px;}}
.action-row{{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start;}}
.action-row + .action-row{{margin-top:10px;}}
.action{{position:relative;}}
.action-corner{{margin-left:auto;}}
.popover-right{{left:auto;right:0;}}
.action > summary{{list-style:none;cursor:pointer;}}
.action > summary::-webkit-details-marker{{display:none;}}
.popover{{position:absolute;top:calc(100% + 6px);left:0;z-index:30;background:var(--panel-strong);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--panel-line);border-radius:8px;padding:12px;box-shadow:0 6px 20px var(--field-line);min-width:220px;}}
.popover form{{display:flex;flex-direction:column;gap:8px;}}
.popover-row{{display:flex;gap:6px;align-items:center;}}
.popover .tag{{white-space:nowrap;}}
.popover input[type=text],.popover select{{padding:7px;border:1px solid var(--field-line);border-radius:5px;font-size:12.5px;background:var(--field);}}
.popover input[type=text]:focus,.popover select:focus{{outline:2px solid var(--accent);outline-offset:1px;border-color:var(--accent);background:#fff;}}
.popover input[type=file]{{font-size:12px;max-width:190px;}}
.popover-sep{{border:none;border-top:1px solid var(--line);margin:10px 0;}}
/* Two visibly different upload choices. The native file input is hidden
   behind a labelled tile because the browser renders both modes as an
   identical "Browse..." button - which made picking the wrong one, and then
   hunting for a Select button that the file chooser never shows, far too
   easy. */
.popover-upload{{min-width:260px;}}
.up-form{{display:block;}}
.up-choice{{display:flex;align-items:center;gap:10px;padding:9px 10px;border:1px solid var(--field-line);border-radius:7px;cursor:pointer;transition:border-color .12s ease,background .12s ease;}}
.up-choice:hover{{border-color:var(--accent);background:var(--accent-soft);}}
.up-ico{{flex:none;display:flex;align-items:center;justify-content:center;width:26px;}}
.up-txt{{display:flex;flex-direction:column;line-height:1.3;}}
.up-txt b{{font-size:12.5px;color:var(--text);font-weight:600;}}
.up-txt small{{font-size:10.5px;color:var(--text-faint);}}
.up-picked{{margin-top:7px;font-size:11.5px;color:var(--text-muted);word-break:break-word;}}
.up-picked[hidden]{{display:none;}}
.up-go{{margin-top:8px;width:100%;}}
.up-go[hidden]{{display:none;}}
.up-hint{{margin:11px 0 0;font-size:10.5px;line-height:1.45;color:var(--text-faint);}}
button,.btn,.action>summary{{padding:8px 14px;background:var(--accent);color:#fff;border:none;border-radius:5px;font-weight:600;cursor:pointer;font-size:12.5px;text-decoration:none;display:inline-block;}}
button:hover,.btn:hover{{background:var(--accent-dark);}}
.btn-ghost{{background:transparent;color:var(--text-muted);border:1px solid var(--field-line);}}
.btn-ghost:hover{{background:var(--hover);}}
.btn-danger{{background:transparent;color:var(--danger);border:1px solid var(--danger-line);}}
.btn-danger:hover{{background:var(--danger-soft);}}
.table-wrap{{background:var(--panel);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--panel-line);border-radius:8px;overflow:hidden;box-shadow:0 2px 10px var(--shadow-col);}}
table{{width:100%;border-collapse:collapse;background:transparent;}}
td,th{{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:13px;}}
tr:last-child td{{border-bottom:none;}}
th{{color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:0.03em;}}
th a{{color:inherit;text-decoration:none;}}
th a:hover{{color:var(--accent);}}
a.name{{color:var(--text);text-decoration:none;font-weight:500;}}
a.name:hover{{color:var(--accent);}}
.thumb{{object-fit:cover;border-radius:4px;background:var(--thumb-bg);}}
#jobs-bar{{margin:0 0 12px;}}
.job{{padding:10px 13px;margin-bottom:8px;border-radius:10px;background:var(--panel-strong);border:1px solid var(--panel-line);font-size:13px;color:var(--text);}}
.job-head{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;}}
.job-sub{{color:var(--text-muted);font-size:12px;margin-top:3px;word-break:break-word;}}
.job-bar{{height:6px;border-radius:4px;background:var(--panel-line);margin-top:8px;overflow:hidden;}}
.job-bar > i{{display:block;height:100%;background:var(--accent);transition:width .4s ease;}}
.job-bar.indet > i{{animation:jobslide 1.2s ease-in-out infinite;}}
@keyframes jobslide{{0%{{margin-left:-35%;}}100%{{margin-left:100%;}}}}
.job-error{{border-color:var(--danger);}}
.job-error .job-sub{{color:var(--danger);}}
.job-x{{background:none;border:0;color:var(--text-muted);cursor:pointer;font-size:16px;line-height:1;padding:0 2px;}}
.thumb-inline{{vertical-align:-9px;margin-right:5px;}}
.icon{{display:inline-block;vertical-align:-4px;margin-right:3px;}}
.empty{{padding:20px;text-align:center;color:var(--text-faint);font-size:13px;}}
.tiles-grid{{display:grid;gap:14px;padding:6px;}}
.tile{{position:relative;border-radius:8px;padding:10px;display:flex;flex-direction:column;align-items:center;text-align:center;gap:6px;cursor:grab;}}
.tile.drag-over{{background:var(--accent-soft);outline:2px solid var(--accent);}}
.tile-thumb-wrap{{position:relative;margin:0 auto;flex:1;}}
.tile-thumb{{display:flex;align-items:center;justify-content:center;text-decoration:none;width:100%;height:100%;}}
.tile-thumb svg,.tile-thumb img.thumb{{filter:drop-shadow(0 2px 5px var(--icon-shadow));}}
.tile.selected{{background:var(--accent-soft);outline:2px solid var(--accent);}}
.tile-name{{font-size:12.5px;font-weight:600;color:var(--text);text-shadow:0 0 6px var(--glow),0 0 2px var(--glow);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;}}
.ctx-menu{{position:fixed;z-index:100;background:var(--panel-strong);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--panel-line);border-radius:8px;padding:6px;box-shadow:0 8px 24px var(--line-strong);min-width:160px;display:flex;flex-direction:column;gap:1px;}}
/* display:flex above has the same specificity as the browser's built-in
   [hidden]{{display:none}} rule, and author styles win ties over the user
   agent stylesheet - so without this, setting .hidden=true in JS silently
   had no visual effect at all and the menu could never actually close. */
.ctx-menu[hidden],.ctx-sub[hidden]{{display:none !important;}}
.ctx-menu button{{background:transparent;color:var(--text);border:none;text-align:left;padding:7px 10px;font-size:13px;font-weight:500;border-radius:5px;cursor:pointer;}}
.ctx-menu button:hover{{background:var(--accent-soft);}}
.ctx-menu button.danger{{color:var(--danger);}}
.ctx-menu button.danger:hover{{background:var(--danger-soft);}}
.ctx-menu button:disabled{{opacity:0.35;cursor:not-allowed;}}
.ctx-menu button:disabled:hover{{background:transparent;}}
.ctx-sub{{position:fixed;z-index:110;min-width:220px;}}
.center-card{{background:var(--panel);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid var(--panel-line);padding:20px;border-radius:8px;overflow:hidden;box-shadow:0 2px 10px var(--shadow-col);max-width:420px;margin:40px auto;}}
.center-card h2{{font-size:16px;margin:0 0 10px;}}
.center-card p{{font-size:13px;color:var(--text-muted);margin:0 0 16px;}}
.row{{display:flex;gap:10px;}}
.tag{{font-size:10.5px;color:var(--text-faint);font-weight:400;}}
tr[draggable=true]{{cursor:grab;}}
tr.drag-over td{{background:var(--accent-soft);}}
</style></head>
<body>
{content}
</body></html>
"""

def breadcrumbs(relpath):
    parts = [p for p in relpath.split("/") if p]
    out = ['<a href="/">Files</a>']
    acc = ""
    for p in parts:
        acc = f"{acc}/{p}" if acc else p
        out.append(f'<a href="/?path={urllib.parse.quote(acc)}">{esc(p)}</a>')
    return " / ".join(out)

def sort_link(relpath, q, sort, direction, column, label):
    next_dir = "desc" if (sort == column and direction == "asc") else "asc"
    arrow = ""
    if sort == column:
        arrow = " &#9650;" if direction == "asc" else " &#9660;"
    params = {"path": relpath, "sort": column, "dir": next_dir}
    if q:
        params["q"] = q
    return f'<a href="/?{urllib.parse.urlencode(params)}">{label}{arrow}</a>'

def build_item(e, st, relpath, thumb_px):
    """Everything the table and tiles renderers both need for one entry,
    computed once so the two layouts can't drift out of sync with each
    other (e.g. one gaining a preview link the other forgets)."""
    entry_rel = f"{relpath}/{e.name}" if relpath else e.name
    modified = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    q_enc = urllib.parse.quote(entry_rel)
    sel = esc(entry_rel)
    is_dir = e.is_dir(follow_symlinks=False)
    if is_dir:
        # A mounted USB stick is a real filesystem mountpoint (that's what the
        # automount script gives it), unlike an ordinary folder underneath it -
        # so checking that, rather than matching on a name like "USB", finds
        # the actual drive wherever it's plugged in and however it's labelled,
        # while the plain folder that contains it stays a normal folder icon.
        try:
            is_usb_mount = os.path.ismount(e.path)
        except OSError:
            is_usb_mount = False
        thumb_html = (usb_svg(thumb_px, css_class="") if is_usb_mount
                      else folder_svg(thumb_px, css_class=""))
        name_href, name_target = f"/?path={q_enc}", ""
        extract_link = download_link = ""
    else:
        if is_image(e.name):
            thumb_html = (f'<img class="thumb" loading="lazy" src="/view?path={q_enc}" '
                          f'style="width:{thumb_px}px;height:{thumb_px}px;">')
        else:
            thumb_html = file_svg(thumb_px, css_class="")
        if is_previewable(e.name):
            name_href, name_target = f"/view?path={q_enc}", ' target="_blank"'
        else:
            name_href, name_target = f"/download?path={q_enc}", ""
        extract_link = (f'<a href="/extract?path={q_enc}">extract</a>'
                         if archive_kind(e.name) else "")
        download_link = f'<a href="/download?path={q_enc}">download</a>'
    return {
        "name": e.name, "sel": sel, "q_enc": q_enc, "is_dir": is_dir,
        "is_archive": bool(archive_kind(e.name)),
        "size": "&mdash;" if is_dir else fmt_bytes(st.st_size),
        "modified": modified, "thumb_html": thumb_html,
        "name_href": name_href, "name_target": name_target,
        "extract_link": extract_link, "download_link": download_link,
    }

def render_table(items, relpath, q, sort, direction):
    rows = ""
    for it in items:
        isdir_attr = ' data-isdir="1"' if it["is_dir"] else ""
        archive_attr = ' data-archive="1"' if it["is_archive"] else ""
        thumb_cls = "" if it["is_dir"] else ' class="thumb-inline"'
        rows += (
            f'<tr draggable="true" data-relpath="{it["sel"]}"{isdir_attr}{archive_attr}>'
            f'<td><input type="checkbox" name="sel" value="{it["sel"]}"> '
            f'<span{thumb_cls}>{it["thumb_html"]}</span> '
            f'<a class="name" href="{it["name_href"]}"{it["name_target"]}>{esc(it["name"])}</a></td>'
            f'<td>{it["size"]}</td><td>{it["modified"]}</td></tr>'
        )
    if not rows:
        rows = '<tr><td colspan="4" class="empty">' + (
            "No matches." if q else "This folder is empty.") + '</td></tr>'
    return f"""<div class="table-wrap" id="file-table">
<table>
<tr><th></th>
<th>{sort_link(relpath, q, sort, direction, "name", "Name")}</th>
<th>{sort_link(relpath, q, sort, direction, "size", "Size")}</th>
<th>{sort_link(relpath, q, sort, direction, "modified", "Modified")}</th></tr>
{rows}
</table>
</div>"""

def render_tiles(items, thumb_px, q):
    tiles = ""
    for it in items:
        isdir_attr = ' data-isdir="1"' if it["is_dir"] else ""
        archive_attr = ' data-archive="1"' if it["is_archive"] else ""
        # No visible checkbox in Tiles view - selection is click/ctrl-click on
        # the tile itself (handled by the script below), which just toggles
        # this same hidden checkbox under the hood so /bulk's server-side
        # handling needs no changes at all. Rename/download/delete/etc. no
        # longer show under each tile either - they're all on right-click now.
        tiles += (
            f'<div class="tile" draggable="true" data-relpath="{it["sel"]}"{isdir_attr}{archive_attr}>'
            f'<input type="checkbox" name="sel" value="{it["sel"]}" hidden>'
            f'<div class="tile-thumb-wrap" style="width:{thumb_px}px;min-height:{thumb_px}px;">'
            f'<a class="tile-thumb" href="{it["name_href"]}"{it["name_target"]}>{it["thumb_html"]}</a>'
            f'</div>'
            f'<div class="tile-name" title="{esc(it["name"])}">{esc(it["name"])}</div>'
            f'</div>'
        )
    if not tiles:
        tiles = '<div class="empty">' + (
            "No matches." if q else "This folder is empty.") + '</div>'
    # Desktop icon grids (Finder, Explorer, Nautilus) scale the gap between
    # icons with icon size - roughly a fifth to a quarter of it - rather than
    # using one fixed gap for every size, which is what made Large thumbnails
    # look cramped and Small ones look oddly spaced out before this.
    gap = max(8, round(thumb_px * 0.22))
    col_min = thumb_px + 60
    return (f'<div class="tiles-grid" id="file-table" '
            f'style="grid-template-columns:repeat(auto-fill,minmax({col_min}px,1fr));'
            f'gap:{gap}px;">'
            f'{tiles}</div>')

# Progress strip for background copy/move/zip/extract jobs (see job_start). It
# is a plain string rather than part of the f-string below so the script's
# braces don't need escaping. It only polls while something is running.
JOBS_BAR = """<div id="jobs-bar"></div>
<script>
(function(){
  var bar = document.getElementById("jobs-bar");
  var running = {};   // jobs this page has watched running
  var timer = null;
  function fmt(n){
    var u = ["B","KB","MB","GB","TB"], i = 0;
    while(n >= 1024 && i < u.length - 1){ n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  function esc(s){ var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
  function render(jobs){
    bar.innerHTML = jobs.map(function(j){
      var known = j.total > 0;
      var pct = known ? Math.min(100, Math.floor(j.done * 100 / j.total)) : 0;
      var sub = j.state === "running"
        ? (known ? fmt(j.done) + " of " + fmt(j.total) + " (" + pct + "%)" : "Working...")
        : (j.state === "done" ? "Finished" : j.error);
      var notes = (j.notes || []).map(function(n){ return "<div class='job-sub'>" + esc(n) + "</div>"; }).join("");
      var meter = j.state === "running"
        ? "<div class='job-bar" + (known ? "" : " indet") + "'><i style='width:" + (known ? pct : 35) + "%'></i></div>" : "";
      var x = j.state === "running" ? "" :
        "<button type='button' class='job-x' data-dismiss='" + j.id + "' title='Dismiss'>&times;</button>";
      return "<div class='job job-" + j.state + "'><div class='job-head'><b>" + esc(j.label) + "</b>" + x + "</div>"
           + "<div class='job-sub'>" + esc(sub) + "</div>" + notes + meter + "</div>";
    }).join("");
  }
  function poll(){
    clearTimeout(timer);
    fetch("/jobs", {credentials: "same-origin"}).then(function(r){ return r.json(); }).then(function(jobs){
      render(jobs);
      var anyRunning = false;
      jobs.forEach(function(j){
        if(j.state === "running"){ running[j.id] = true; anyRunning = true; }
        else if(running[j.id]){
          // Watched it finish: the listing behind it is stale now, so reload
          // once. (Not for jobs that were already over when the page loaded.)
          delete running[j.id];
          try{
            if(sessionStorage.getItem("job-done-" + j.id)) return;
            sessionStorage.setItem("job-done-" + j.id, "1");
          }catch(e){}
          location.reload();
        }
      });
      if(anyRunning) timer = setTimeout(poll, 2000);
    }).catch(function(){ timer = setTimeout(poll, 5000); });
  }
  bar.addEventListener("click", function(e){
    var id = e.target.getAttribute && e.target.getAttribute("data-dismiss");
    if(!id) return;
    fetch("/jobs-dismiss", {method: "POST", body: new URLSearchParams({id: id})}).then(poll);
  });
  poll();
})();
</script>
"""

def render_listing(relpath, q="", sort="name", direction="asc", view="list", thumb="small"):
    real_dir, relpath = safe_path(relpath)
    try:
        entries = list(os.scandir(real_dir))
    except OSError:
        entries = []

    if q:
        entries = [e for e in entries if q.lower() in e.name.lower()]
    entries = sort_entries(entries, sort, direction)

    thumb_px = THUMB_PX.get(thumb, THUMB_PX["small"])
    items = []
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        items.append(build_item(e, st, relpath, thumb_px))

    if view == "tiles":
        listing_html = render_tiles(items, thumb_px, q)
    else:
        listing_html = render_table(items, relpath, q, sort, direction)

    clear = (f'<a class="btn btn-ghost" href="/?path={urllib.parse.quote(relpath)}">Clear</a>'
             if q else "")
    dest_options = folder_options_html(ROOT_DIR)

    up_link = (f'<a class="up-link" href="/?path={urllib.parse.quote(os.path.dirname(relpath))}">'
               f'&#8593; Up</a>' if relpath else "")
    # At the root the trail would just read "Files", duplicating the window
    # title directly above it - so the whole row is dropped there and only
    # appears once you are somewhere it actually tells you something.
    crumbs_html = (f'<div class="crumbs">{up_link}{breadcrumbs(relpath)}</div>'
                   if relpath else "")

    view_label = f"{view.capitalize()} &middot; {thumb.capitalize()} thumbs"
    search_label = f'Search: &ldquo;{esc(q)}&rdquo;' if q else "Search"

    content = f"""
{JOBS_BAR}
<div class="toolbar">
<div class="action-row">
<details class="action" name="toolbar-nav"{' open' if q else ''}>
<summary>&#128269; {search_label}</summary>
<div class="popover">
<form method="GET" action="/">
<input type="hidden" name="path" value="{esc(relpath)}">
<input type="text" name="q" value="{esc(q)}" placeholder="Search this folder" autofocus>
<button type="submit">Find</button>
{clear}
</form>
</div>
</details>
<details class="action" name="toolbar-nav">
<summary>&#43; New Folder</summary>
<div class="popover">
<form method="POST" action="/mkdir">
<input type="hidden" name="path" value="{esc(relpath)}">
<input type="text" name="foldername" placeholder="Folder name" required autofocus>
<button type="submit">Create</button>
</form>
</div>
</details>
<details class="action" name="toolbar-nav">
<summary>&#8593; Upload</summary>
<div class="popover popover-upload">
<form method="POST" action="/upload" enctype="multipart/form-data" class="up-form" id="up-files-form">
<input type="hidden" name="path" value="{esc(relpath)}">
<label class="up-choice">
  <span class="up-ico">{file_svg(22, css_class="")}</span>
  <span class="up-txt"><b>Choose files</b><small>Pick one or more individual files</small></span>
  <input type="file" name="file" id="up-files" multiple hidden>
</label>
<div class="up-picked" id="up-files-picked" hidden></div>
<button type="submit" class="up-go" id="up-files-go" hidden>Upload</button>
</form>
<hr class="popover-sep">
<form method="POST" action="/upload" enctype="multipart/form-data" class="up-form" id="up-dir-form">
<input type="hidden" name="path" value="{esc(relpath)}">
<label class="up-choice">
  <span class="up-ico">{folder_svg(22, css_class="")}</span>
  <span class="up-txt"><b>Choose a whole folder</b><small>Keeps the folders inside it</small></span>
  <input type="file" name="file" id="up-dir" webkitdirectory directory multiple hidden>
</label>
<div class="up-picked" id="up-dir-picked" hidden></div>
<button type="submit" class="up-go" id="up-dir-go" hidden>Upload folder</button>
</form>
<p class="up-hint">Uploading a folder? Use the second option. In the plain file
chooser, <b>Open</b> steps <i>into</i> a folder instead of choosing it.</p>
</div>
</details>
<details class="action action-corner" name="toolbar-nav">
<summary title="{view_label}">&#9638; View</summary>
<div class="popover popover-right">
<form method="GET" action="/">
<input type="hidden" name="path" value="{esc(relpath)}">
<input type="hidden" name="q" value="{esc(q)}">
<input type="hidden" name="sort" value="{esc(sort)}">
<input type="hidden" name="dir" value="{esc(direction)}">
<select name="view">
<option value="list"{' selected' if view == 'list' else ''}>List</option>
<option value="tiles"{' selected' if view == 'tiles' else ''}>Tiles</option>
</select>
<select name="thumb">
<option value="small"{' selected' if thumb == 'small' else ''}>Small thumbs</option>
<option value="medium"{' selected' if thumb == 'medium' else ''}>Medium thumbs</option>
<option value="large"{' selected' if thumb == 'large' else ''}>Large thumbs</option>
</select>
<button type="submit">Apply</button>
</form>
</div>
</details>
</div>
</div>
{crumbs_html}
<form id="bulk-form" method="POST" action="/bulk">
<input type="hidden" name="path" value="{esc(relpath)}">
<input type="hidden" name="op" id="ctx-op">
{listing_html}
</form>

<!-- Right-click menu: replaces the old always-visible rename/delete/etc
     links under every item. Built once here, positioned and filled in by
     the script below depending on what's selected when you right-click. -->
<div id="ctx-menu" class="ctx-menu" hidden>
<button type="button" data-act="download">Download</button>
<button type="button" data-act="rename">Rename</button>
<button type="button" data-act="extract">Extract</button>
<button type="button" data-act="moveto">Move to&hellip;</button>
<button type="button" data-act="copyto">Copy to&hellip;</button>
<button type="button" data-act="compress">Compress&hellip;</button>
<button type="button" data-act="delete" class="danger">Delete</button>
</div>
<div id="ctx-moveto" class="popover ctx-sub" hidden>
<div class="popover-row"><span class="tag">to</span><select form="bulk-form" name="dest">{dest_options}</select></div>
<div class="popover-row"><span class="tag">or new</span><input form="bulk-form" type="text" name="dest_new" placeholder="new/folder/path"></div>
<div class="popover-row"><button type="button" id="ctx-moveto-go">Go</button></div>
</div>
<div id="ctx-compress" class="popover ctx-sub" hidden>
<div class="popover-row">
<input form="bulk-form" type="text" name="archive_name" placeholder="archive.zip">
<button type="button" id="ctx-compress-go">Compress</button>
</div>
</div>
<script>
(function(){{
  var root = document.getElementById('file-table');
  var dragSrc = null;
  // Selectors use [data-relpath]/[data-isdir] with no tag name so the same
  // script drives both the <tr> rows in List view and the <div class="tile">
  // cards in Tiles view without caring which one is on screen.
  root.addEventListener('dragstart', function(e){{
    var el = e.target.closest('[data-relpath]');
    if (!el) return;
    dragSrc = el.getAttribute('data-relpath');
  }});
  root.addEventListener('dragover', function(e){{
    var el = e.target.closest('[data-isdir="1"]');
    if (el) {{ e.preventDefault(); el.classList.add('drag-over'); }}
  }});
  root.addEventListener('dragleave', function(e){{
    var el = e.target.closest('[data-isdir="1"]');
    if (el) el.classList.remove('drag-over');
  }});
  root.addEventListener('drop', function(e){{
    var el = e.target.closest('[data-isdir="1"]');
    if (!el || !dragSrc) return;
    e.preventDefault();
    el.classList.remove('drag-over');
    var destPath = el.getAttribute('data-relpath');
    if (destPath === dragSrc) return;
    fetch('/drag-move', {{method: 'POST', body: JSON.stringify({{src: dragSrc, dest: destPath}})}})
      .then(function(){{ location.reload(); }});
  }});

  function clearSelection(){{
    root.querySelectorAll('input[type=checkbox]:checked').forEach(function(cb){{ cb.checked = false; }});
    root.querySelectorAll('.tile.selected').forEach(function(t){{ t.classList.remove('selected'); }});
  }}
  function setSelected(item, on){{
    var cb = item.querySelector('input[type=checkbox]');
    if (cb) cb.checked = on;
    if (item.classList.contains('tile')) item.classList.toggle('selected', on);
  }}
  function getSelectedPaths(){{
    return Array.from(root.querySelectorAll('input[type=checkbox]:checked')).map(function(cb){{ return cb.value; }});
  }}

  // Tiles view has no visible checkbox - click selects (replacing any
  // other selection), ctrl/cmd-click toggles this tile into a multi-
  // selection, and double-click opens it. List view keeps its native
  // checkboxes as the selection mechanism there. Either way the same
  // hidden "sel" checkboxes end up set, so nothing server-side changes.
  root.addEventListener('click', function(e){{
    var tile = e.target.closest('.tile');
    if (!tile) {{
      // Clicked the empty canvas within the listing - not on any item at
      // all - so there's nothing to select or toggle: just clear whatever
      // was selected before. Without this, a ctrl-click multi-selection
      // (or List view's own checkboxes) had no way to be cleared at all.
      if (!e.target.closest('[data-relpath]')) clearSelection();
      return;
    }}
    var checkbox = tile.querySelector('input[type=checkbox]');
    if (!checkbox) return;
    e.preventDefault();
    if (e.ctrlKey || e.metaKey) {{
      setSelected(tile, !checkbox.checked);
    }} else {{
      var wasSoleSelection = tile.classList.contains('selected') &&
        root.querySelectorAll('.tile.selected').length === 1;
      clearSelection();
      setSelected(tile, !wasSoleSelection);
    }}
  }});

  root.addEventListener('dblclick', function(e){{
    var tile = e.target.closest('.tile');
    if (!tile) return;
    var link = tile.querySelector('a.tile-thumb');
    if (!link) return;
    if (link.target === '_blank') window.open(link.href, '_blank');
    else window.location = link.href;
  }});

  // Right-click menu - replaces every rename/download/extract/delete link
  // that used to sit under each row or tile, plus move/copy/compress which
  // used to be dedicated toolbar buttons. One selected item shows the
  // single-item actions (download/rename/extract); several selected items
  // (via ctrl-click or ticked checkboxes) show only the actions that make
  // sense on a whole batch (move/copy/compress/delete).
  var menu = document.getElementById('ctx-menu');
  var moveto = document.getElementById('ctx-moveto');
  var compress = document.getElementById('ctx-compress');
  var opField = document.getElementById('ctx-op');
  var bulkForm = document.getElementById('bulk-form');
  var ctxTargetPath = null;

  function closeAll(){{
    menu.hidden = true;
    moveto.hidden = true;
    compress.hidden = true;
  }}

  function placeAt(el, x, y){{
    el.style.left = x + 'px';
    el.style.top = y + 'px';
    el.hidden = false;
    var rect = el.getBoundingClientRect();
    if (rect.right > window.innerWidth) el.style.left = Math.max(8, window.innerWidth - rect.width - 8) + 'px';
    if (rect.bottom > window.innerHeight) el.style.top = Math.max(8, window.innerHeight - rect.height - 8) + 'px';
  }}

  root.addEventListener('contextmenu', function(e){{
    var item = e.target.closest('[data-relpath]');
    if (!item) return;
    e.preventDefault();
    closeAll();

    if (!item.querySelector('input[type=checkbox]').checked) {{
      clearSelection();
      setSelected(item, true);
    }}
    var sels = getSelectedPaths();
    ctxTargetPath = sels.length === 1 ? sels[0] : null;

    // All seven actions always show, in the same order every time - the
    // ones that don't apply (Extract on a non-archive, Download on a
    // folder or a multi-selection, etc.) are disabled and faded instead of
    // disappearing, so the menu shape stays predictable and it's clear
    // *why* something can't be done rather than it just not being there.
    var single = sels.length === 1;
    var isArchive = single && item.hasAttribute('data-archive');
    var isDir = single && item.hasAttribute('data-isdir');
    menu.querySelector('[data-act=download]').disabled = !single || isDir;
    menu.querySelector('[data-act=rename]').disabled = !single;
    menu.querySelector('[data-act=extract]').disabled = !isArchive;
    placeAt(menu, e.clientX, e.clientY);
  }});

  menu.addEventListener('click', function(e){{
    var btn = e.target.closest('button');
    if (!btn || btn.disabled) return;
    var act = btn.getAttribute('data-act');
    if (act === 'download') window.location = '/download?path=' + encodeURIComponent(ctxTargetPath);
    else if (act === 'rename') window.location = '/rename?path=' + encodeURIComponent(ctxTargetPath);
    else if (act === 'extract') window.location = '/extract?path=' + encodeURIComponent(ctxTargetPath);
    else if (act === 'delete') {{
      opField.value = 'delete';
      menu.hidden = true;
      bulkForm.requestSubmit();
    }} else if (act === 'moveto' || act === 'copyto') {{
      opField.value = act === 'moveto' ? 'move' : 'copy';
      var r = menu.getBoundingClientRect();
      menu.hidden = true;
      placeAt(moveto, r.left, r.top);
    }} else if (act === 'compress') {{
      opField.value = 'compress';
      var r2 = menu.getBoundingClientRect();
      menu.hidden = true;
      placeAt(compress, r2.left, r2.top);
    }}
  }});

  document.getElementById('ctx-moveto-go').addEventListener('click', function(){{
    moveto.hidden = true;
    bulkForm.requestSubmit();
  }});
  document.getElementById('ctx-compress-go').addEventListener('click', function(){{
    compress.hidden = true;
    bulkForm.requestSubmit();
  }});

  // mousedown (not just click) closes the menu the instant you press down
  // anywhere else - this page runs inside the Desktop shell's iframe, and a
  // plain 'click' can be swallowed by whatever the click landed on before it
  // ever reaches document; mousedown fires earlier and more reliably.
  document.addEventListener('mousedown', function(e){{
    if (!e.target.closest('.ctx-menu') && !e.target.closest('.ctx-sub')) closeAll();
  }});
  // Same deal for selection: the root-level click handler only knows how to
  // react to clicks that land ON a file item, so it can never notice a click
  // that lands somewhere else entirely - the wide margin beside the grid,
  // the toolbar, anywhere. This is the one place that's aware of the whole
  // page, so it's what clears a stale multi-selection with nowhere else to go.
  document.addEventListener('mousedown', function(e){{
    if (e.target.closest('.ctx-menu') || e.target.closest('.ctx-sub')) return;
    if (!e.target.closest('[data-relpath]')) clearSelection();
  }});
  document.addEventListener('click', function(e){{
    if (!e.target.closest('.ctx-menu') && !e.target.closest('.ctx-sub')) closeAll();
  }});

  // A <details> popover only closes by clicking its own <summary> again -
  // so an opened Search/New Folder/Upload/View panel had no way to be
  // dismissed if you changed your mind, which is exactly what made the
  // Upload panel feel stuck. Close any open one when the click lands
  // outside it. mousedown would fire before the file picker opens, so
  // this one deliberately uses click.
  // Since the native inputs are hidden behind the tiles above, the page has
  // to report the selection itself - otherwise you'd click "Choose files",
  // pick something, and get no confirmation that anything was chosen.
  function wireUpload(inputId, pickedId, goId, isFolder){{
    var input = document.getElementById(inputId);
    var picked = document.getElementById(pickedId);
    var go = document.getElementById(goId);
    if(!input || !picked || !go) return;
    input.addEventListener('change', function(){{
      var files = input.files;
      if(!files || !files.length){{
        picked.hidden = true; go.hidden = true; return;
      }}
      var label;
      if(isFolder){{
        // webkitRelativePath is "TopFolder/sub/file.ext"; the first segment
        // is the folder the user actually chose.
        var rel = files[0].webkitRelativePath || '';
        var top = rel.split('/')[0] || 'folder';
        label = 'Folder: ' + top + ' \\u2014 ' + files.length +
                (files.length === 1 ? ' file' : ' files');
      }} else {{
        label = files.length === 1 ? files[0].name
              : files.length + ' files selected';
      }}
      picked.textContent = label;
      picked.hidden = false;
      go.hidden = false;
    }});
  }}
  wireUpload('up-files', 'up-files-picked', 'up-files-go', false);
  wireUpload('up-dir', 'up-dir-picked', 'up-dir-go', true);

  function closePopovers(except){{
    document.querySelectorAll('details.action[open]').forEach(function(d){{
      if (d !== except) d.removeAttribute('open');
    }});
  }}
  document.addEventListener('click', function(e){{
    closePopovers(e.target.closest('details.action'));
  }});
  document.addEventListener('keydown', function(e){{
    if (e.key === 'Escape') {{ closeAll(); closePopovers(null); }}
  }});
  // Clicking outside the iframe entirely (the Desktop shell's own wallpaper/
  // dock/other windows) never reaches this document at all, but it does
  // blur this iframe's window - catch that too so the menu can't get stuck
  // open just because the click landed one frame over.
  window.addEventListener('blur', closeAll);
}})();
</script>
"""
    return content

def render_rename(relpath):
    real_path, relpath = safe_path(relpath)
    current_name = os.path.basename(real_path) if relpath else ""
    if not relpath or not os.path.exists(real_path):
        return render_error("Nothing to rename.")
    parent = os.path.dirname(relpath)
    return f"""
<div class="center-card">
<h2>Rename</h2>
<p>Renaming <b>{esc(current_name)}</b></p>
<form method="POST" action="/do-rename">
<input type="hidden" name="path" value="{esc(relpath)}">
<input type="text" name="newname" value="{esc(current_name)}" required
  style="width:100%;padding:9px;border:1px solid var(--field-line);border-radius:5px;box-sizing:border-box;font-size:14px;margin-bottom:14px;">
<div class="row">
<button type="submit">Rename</button>
<a class="btn btn-ghost" href="/?path={urllib.parse.quote(parent)}">Cancel</a>
</div>
</form>
</div>
"""

def render_confirm_delete(relpath):
    real_path, relpath = safe_path(relpath)
    if not relpath or not os.path.exists(real_path):
        return render_error("Nothing to delete.")
    name = os.path.basename(real_path)
    is_dir = os.path.isdir(real_path)
    parent = os.path.dirname(relpath)
    kind = "folder and everything inside it" if is_dir else "file"
    return f"""
<div class="center-card">
<h2>Delete {kind}?</h2>
<p><b>{esc(name)}</b> {"and all its contents will" if is_dir else "will"} be permanently deleted. This can't be undone.</p>
<form method="POST" action="/do-delete">
<input type="hidden" name="path" value="{esc(relpath)}">
<div class="row">
<button type="submit" style="background:var(--danger);">Yes, delete</button>
<a class="btn btn-ghost" href="/?path={urllib.parse.quote(parent)}">Cancel</a>
</div>
</form>
</div>
"""

def render_confirm_bulk_delete(cur_rel, sels):
    hidden = "".join(f'<input type="hidden" name="sel" value="{esc(s)}">' for s in sels)
    shown = ", ".join(esc(os.path.basename(s)) for s in sels[:8])
    more = f" and {len(sels) - 8} more" if len(sels) > 8 else ""
    return f"""
<div class="center-card">
<h2>Delete {len(sels)} item(s)?</h2>
<p>{shown}{more} will be permanently deleted. This can't be undone.</p>
<form method="POST" action="/bulk-do">
<input type="hidden" name="path" value="{esc(cur_rel)}">
{hidden}
<div class="row">
<button type="submit" style="background:var(--danger);">Yes, delete</button>
<a class="btn btn-ghost" href="/?path={urllib.parse.quote(cur_rel)}">Cancel</a>
</div>
</form>
</div>
"""

def render_error(msg):
    return f'<div class="center-card"><h2>Not found</h2><p>{esc(msg)}</p><a class="btn btn-ghost" href="/">Back to Files</a></div>'

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def send_html(self, content, code=200, cookies=None):
        theme_vars = theme_from_cookies(self.headers.get("Cookie", ""))
        body = PAGE.format(content=content, theme_vars=theme_vars).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (cookies or []):
            self.send_header(
                "Set-Cookie",
                f"{name}={urllib.parse.quote(value)}; Path=/; Max-Age=31536000")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, path):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def setup(self):
        super().setup()
        # Each connection now holds a thread. Without keepalive, a client that
        # vanishes mid-download (laptop lid, dropped wifi) would pin its thread
        # forever; with it the dead connection is noticed within ~2 minutes.
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt, val in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
                if hasattr(socket, opt):
                    self.connection.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        except OSError:
            pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, real_path, ctype, attachment=False):
        """Stream a file, honouring Range so an interrupted download can pick
        up where it stopped (and video previews can seek). The bytes go out
        via sendfile - straight from the page cache to the socket - instead of
        64KB at a time through Python, which is the slow way to serve a 12GB
        file from an 800MHz core."""
        try:
            st = os.stat(real_path)
        except OSError:
            self.send_html(render_error("File not found."), code=404)
            return
        size = st.st_size
        etag = '"%x-%x"' % (st.st_mtime_ns, size)
        last_mod = formatdate(st.st_mtime, usegmt=True)

        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "").strip()
        if_range = self.headers.get("If-Range", "").strip()
        # If-Range: only honour the range if the file is unchanged since the
        # client's first attempt - otherwise send it all again, so a resumed
        # download can never splice two different versions together.
        if rng.startswith("bytes=") and "," not in rng and (not if_range or if_range in (etag, last_mod)):
            first, _, last = rng[6:].strip().partition("-")
            try:
                if first == "":
                    s, e = max(0, size - int(last)), size - 1
                else:
                    s = int(first)
                    e = min(int(last), size - 1) if last else size - 1
            except ValueError:
                s = e = None  # malformed Range: ignore it and send the whole file
            if s is not None:
                if s > e or s >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end, status = s, e, 206
        length = end - start + 1

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if attachment:
            name = os.path.basename(real_path)
            # Header values are latin-1 only, so a name with any other
            # character used to raise mid-response and kill the download;
            # RFC 5987's filename* carries the real name, the plain one is a
            # printable-ASCII fallback for clients that ignore it.
            fallback = re.sub(r'[^\x20-\x7e]|["\\]', "_", name)
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{fallback}\"; "
                             f"filename*=UTF-8''{urllib.parse.quote(name)}")
        self.end_headers()
        self.close_connection = True

        try:
            self.wfile.flush()
            with open(real_path, "rb") as f:
                infd, sock = f.fileno(), self.connection.fileno()
                offset, remaining, fast = start, length, True
                while remaining > 0:
                    if fast:
                        try:
                            sent = os.sendfile(sock, infd, offset, min(remaining, 4 * 1024 * 1024))
                        except OSError as e:
                            if e.errno not in (errno.EINVAL, errno.ENOSYS):
                                raise
                            fast = False
                            continue
                    else:
                        f.seek(offset)
                        buf = f.read(min(remaining, 256 * 1024))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        sent = len(buf)
                    if sent == 0:
                        break
                    offset += sent
                    remaining -= sent
        except OSError:
            pass  # the client went away (closed the tab, cancelled, dropped off wifi)

    def do_GET(self):
        if not is_authenticated(self):
            send_login_page(self)
            return
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        rel = qs.get("path", [""])[0]

        if parsed.path == "/":
            q = qs.get("q", [""])[0]
            sort = qs.get("sort", [""])[0] or "name"
            direction = qs.get("dir", [""])[0] or "asc"
            cookie_header = self.headers.get("Cookie", "")
            view = qs.get("view", [""])[0] or parse_cookie(cookie_header, "fm_view", "list")
            thumb = qs.get("thumb", [""])[0] or parse_cookie(cookie_header, "fm_thumb", "small")
            if view not in ("list", "tiles"):
                view = "list"
            if thumb not in ("small", "medium", "large"):
                thumb = "small"
            # Only re-set the cookie when the view form was actually
            # submitted (the params are present in the query string) - not
            # on every plain folder-navigation click, which carries neither.
            cookies = []
            if "view" in qs:
                cookies.append(("fm_view", view))
            if "thumb" in qs:
                cookies.append(("fm_thumb", thumb))
            self.send_html(render_listing(rel, q, sort, direction, view, thumb), cookies=cookies)
        elif parsed.path == "/rename":
            self.send_html(render_rename(rel))
        elif parsed.path == "/confirm-delete":
            self.send_html(render_confirm_delete(rel))
        elif parsed.path == "/extract":
            real_path, relpath = safe_path(rel)
            parent_rel = os.path.dirname(relpath)
            if relpath and os.path.isfile(real_path):
                stem = archive_stem(os.path.basename(real_path))
                dest_dir = os.path.join(os.path.dirname(real_path), stem)
                if os.path.exists(dest_dir):
                    dest_dir += "-extracted-" + datetime.now().strftime("%H%M%S")
                def work(job, real_path=real_path, dest_dir=dest_dir):
                    try:
                        extract_archive(real_path, dest_dir)
                    except BaseException:
                        shutil.rmtree(dest_dir, ignore_errors=True)  # no half-extracted folder left behind
                        raise
                job_start(f"Extracting {os.path.basename(real_path)}", work)
            self.redirect("/?path=" + urllib.parse.quote(parent_rel))
        elif parsed.path == "/download":
            real_path, relpath = safe_path(rel)
            if not relpath or not os.path.isfile(real_path):
                self.send_html(render_error("File not found."), code=404)
                return
            self.send_file(real_path, "application/octet-stream", attachment=True)
        elif parsed.path == "/view":
            real_path, relpath = safe_path(rel)
            if not relpath or not os.path.isfile(real_path):
                self.send_html(render_error("File not found."), code=404)
                return
            # No Content-Disposition: attachment here - it lets the browser
            # render this inline (image/video/audio/pdf) instead of forcing a
            # save dialog, which is what makes it a preview.
            self.send_file(real_path, mimetypes.guess_type(real_path)[0] or "application/octet-stream")
        elif parsed.path == "/jobs":
            self.send_json(jobs_snapshot())
        else:
            self.send_html(render_error("Page not found."), code=404)

    def handle_upload(self):
        content_length = int(self.headers.get("Content-Length", 0))
        content_type = self.headers.get("Content-Type", "")

        # Streaming means RAM is never the limit, but disk still is - check
        # free space up front rather than discovering it mid-write. The
        # multipart framing overhead is negligible next to a real movie or
        # model file, so Content-Length is a fine estimate to check against.
        try:
            free = shutil.disk_usage(ROOT_DIR).free
        except OSError:
            free = None
        if free is not None and content_length > free:
            self.send_html(render_error(
                f"Not enough free space on the NAS for this upload "
                f"({fmt_bytes(content_length)} needed, {fmt_bytes(free)} free)."
            ), code=507)
            self.rfile.read(content_length)
            return

        resolved = {"dir": ROOT_DIR, "rel": ""}
        def resolve_dest_dir(path_value):
            real, rel = safe_path(path_value)
            resolved["dir"], resolved["rel"] = real, rel
            return real

        stream_multipart(self.rfile, content_length, content_type, resolve_dest_dir)
        self.redirect("/?path=" + urllib.parse.quote(resolved["rel"]))

    def handle_drag_move(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            data = {}
        src = str(data.get("src", ""))
        dest = str(data.get("dest", ""))
        if src:
            do_bulk_move_or_copy([src], dest, "move")
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            handle_login_post(self)
            return
        if not is_authenticated(self):
            self.send_error(401)
            return
        if parsed.path == "/upload":
            self.handle_upload()
            return
        if parsed.path == "/drag-move":
            self.handle_drag_move()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        fields = urllib.parse.parse_qs(body)

        if parsed.path == "/mkdir":
            cur = fields.get("path", [""])[0]
            foldername = fields.get("foldername", [""])[0].strip()
            real_dir, relcur = safe_path(cur)
            if foldername:
                safe_name = os.path.basename(foldername)
                try:
                    os.makedirs(os.path.join(real_dir, safe_name), exist_ok=True)
                except OSError:
                    pass
            self.redirect("/?path=" + urllib.parse.quote(relcur))

        elif parsed.path == "/do-rename":
            rel = fields.get("path", [""])[0]
            newname = fields.get("newname", [""])[0].strip()
            real_path, relpath = safe_path(rel)
            parent_rel = os.path.dirname(relpath)
            if newname and relpath and os.path.exists(real_path):
                safe_name = os.path.basename(newname)
                new_real = os.path.join(os.path.dirname(real_path), safe_name)
                try:
                    if not os.path.exists(new_real):
                        os.rename(real_path, new_real)
                except OSError:
                    pass
            self.redirect("/?path=" + urllib.parse.quote(parent_rel))

        elif parsed.path == "/do-delete":
            rel = fields.get("path", [""])[0]
            real_path, relpath = safe_path(rel)
            parent_rel = os.path.dirname(relpath)
            if relpath and os.path.exists(real_path):
                try:
                    if os.path.isdir(real_path):
                        shutil.rmtree(real_path)
                    else:
                        os.remove(real_path)
                except OSError:
                    pass
            self.redirect("/?path=" + urllib.parse.quote(parent_rel))

        elif parsed.path == "/bulk":
            cur = fields.get("path", [""])[0]
            op = fields.get("op", [""])[0]
            sels = [s for s in fields.get("sel", []) if s]
            # A typed new-folder path wins over the picker, so you're never
            # stuck if the folder you want doesn't exist yet.
            dest_input = (fields.get("dest_new", [""])[0].strip()
                          or fields.get("dest", [""])[0].strip())
            archive_name = fields.get("archive_name", [""])[0].strip()
            cur_real, cur_rel = safe_path(cur)
            if sels:
                if op == "delete":
                    self.send_html(render_confirm_bulk_delete(cur_rel, sels))
                    return
                elif op in ("move", "copy"):
                    do_bulk_move_or_copy(sels, dest_input, op)
                elif op == "compress":
                    do_bulk_compress(cur_real, sels, archive_name)
            self.redirect("/?path=" + urllib.parse.quote(cur_rel))

        elif parsed.path == "/bulk-do":
            cur = fields.get("path", [""])[0]
            sels = [s for s in fields.get("sel", []) if s]
            _cur_real, cur_rel = safe_path(cur)
            for rel in sels:
                real, relp = safe_path(rel)
                if relp and os.path.exists(real):
                    try:
                        if os.path.isdir(real):
                            shutil.rmtree(real)
                        else:
                            os.remove(real)
                    except OSError:
                        pass
            self.redirect("/?path=" + urllib.parse.quote(cur_rel))

        elif parsed.path == "/jobs-dismiss":
            try:
                jid = int(fields.get("id", [""])[0])
            except ValueError:
                jid = None
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if job and job["state"] != "running":
                    del JOBS[jid]
            self.send_json({"ok": True})

        else:
            self.send_html(render_error("Page not found."), code=404)

class ReusableServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """One thread per connection. It used to be single-threaded, which meant a
    single 12GB download (or a multi-minute copy) held the only handler and
    every other request - even loading the page - queued behind it."""
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

if __name__ == "__main__":
    os.makedirs(ROOT_DIR, exist_ok=True)
    # Threads default to an 8MB stack; on a 256MB box a handful of open
    # connections shouldn't be able to add up to anything worth noticing.
    threading.stack_size(1024 * 1024)
    with ReusableServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()
