"""Regression tests for big-file handling in the Files app (filemanager.py).

Covers what used to break: a single-threaded server that froze during any long
download or copy, copies that died silently and left truncated files, no way to
resume an interrupted download, and USB drives that cannot hold a big file.

Run from anywhere:  python3 tests/test_filemanager_bigfiles.py
Needs curl on the PATH (used for the resume test). Touches only a temp dir.
"""
import errno, hashlib, json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.parse, urllib.request, urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import filemanager as fm

ROOT = tempfile.mkdtemp(prefix="fm_root_")
fm.ROOT_DIR = ROOT
fm.AUTH_FILE = "/nonexistent/auth.conf"          # auth off, as on an install that never enabled it
srv = fm.ReusableServer(("127.0.0.1", 0), fm.Handler)   # port 0: let the OS pick a free one
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"

fails = []
def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        fails.append(name)

def get(path, headers=None, timeout=20):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def wait_job(jid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = [x for x in fm.jobs_snapshot() if x["id"] == jid]
        if j and j[0]["state"] != "running":
            return j[0]
        time.sleep(0.05)
    return None

# ---- fixtures ----------------------------------------------------------------
os.makedirs(os.path.join(ROOT, "src"))
os.makedirs(os.path.join(ROOT, "dst"))
big = os.path.join(ROOT, "src", "big.bin")
with open(big, "wb") as f:
    f.write(os.urandom(50 * 1024 * 1024))
big_md5 = md5(big)
odd = os.path.join(ROOT, "src", "août — модель 日本.gguf")
open(odd, "wb").write(b"x" * 1000)

# ---- 1. the server no longer freezes during a download ------------------------
sparse = os.path.join(ROOT, "src", "sparse.bin")
with open(sparse, "wb") as f:
    f.truncate(3 * 1024 ** 3)       # 3 GB, so a throttled client is still mid-download when we probe
slow = subprocess.Popen(["curl", "-s", "-o", "/dev/null", "--limit-rate", "2M",
                         f"{BASE}/download?path=src/sparse.bin"])
time.sleep(1.5)
t0 = time.time()
st, _, body = get("/?path=src", timeout=10)
dt = time.time() - t0
check("Files page still answers while a big download is in flight", st == 200 and dt < 3, f"status={st} {dt:.1f}s")
slow.kill(); slow.wait()
os.remove(sparse)

# ---- 2. Range / resume ----------------------------------------------------------
st, h, body = get("/download?path=src/big.bin")
check("full download is byte-exact", st == 200 and hashlib.md5(body).hexdigest() == big_md5)
check("advertises Accept-Ranges + validators", h.get("Accept-Ranges") == "bytes" and "ETag" in h and "Last-Modified" in h)
data = open(big, "rb").read()
st, h, body = get("/download?path=src/big.bin", {"Range": "bytes=100-199"})
check("206 for a middle range", st == 206 and body == data[100:200] and h["Content-Range"] == f"bytes 100-199/{len(data)}")
st, h, body = get("/download?path=src/big.bin", {"Range": "bytes=1000000-"})
check("open-ended range", st == 206 and body == data[1000000:])
st, h, body = get("/download?path=src/big.bin", {"Range": "bytes=-50"})
check("suffix range", st == 206 and body == data[-50:])
st, h, body = get("/download?path=src/big.bin", {"Range": f"bytes={len(data)}-"})
check("unsatisfiable range -> 416", st == 416 and h.get("Content-Range") == f"bytes */{len(data)}")
st, h, body = get("/download?path=src/big.bin", {"Range": "bytes=zzz-1"})
check("malformed range ignored -> full 200", st == 200 and len(body) == len(data))
st, h, body = get("/download?path=src/big.bin", {"Range": "bytes=0-9", "If-Range": '"someone-elses-etag"'})
check("If-Range mismatch -> whole file, never a spliced one", st == 200 and len(body) == len(data))
etag = get("/download?path=src/big.bin")[1]["ETag"]
st, h, body = get("/download?path=src/big.bin", {"Range": "bytes=0-9", "If-Range": etag})
check("If-Range match -> 206", st == 206 and body == data[:10])
# real resume with curl -C -
part = os.path.join(ROOT, "resumed.bin")
open(part, "wb").write(data[:20_000_000])
subprocess.run(["curl", "-s", "-C", "-", "-o", part, f"{BASE}/download?path=src/big.bin"], check=True)
check("curl -C - resumes to a byte-identical file", md5(part) == big_md5)

# ---- 3. non-latin-1 filenames used to kill the download -----------------------------
st, h, body = get("/download?path=" + urllib.parse.quote("src/août — модель 日本.gguf"))
check("download of a non-latin-1 filename works", st == 200 and body == b"x" * 1000, str(st))
check("...with an RFC 5987 filename*", "filename*=UTF-8''" in h.get("Content-Disposition", ""))

# ---- 4. copy job ----------------------------------------------------------------------
job = fm.do_bulk_move_or_copy(["src/big.bin"], "dst", "copy")
j = wait_job(job["id"])
check("copy job finishes ok", j and j["state"] == "done", str(j))
check("copy is byte-identical, source kept", md5(os.path.join(ROOT, "dst", "big.bin")) == big_md5 and os.path.exists(big))
check("progress reached 100%", j and j["total"] == len(data) and j["done"] == len(data), str(j))
check("no .part file left behind", not [n for n in os.listdir(os.path.join(ROOT, "dst")) if n.endswith(".part")])

job = fm.do_bulk_move_or_copy(["src/big.bin"], "dst", "copy")
j = wait_job(job["id"])
check("existing destination is skipped with a note, not clobbered", j["state"] == "done" and any("already" in n for n in j["notes"]), str(j))

# ---- 5. same-filesystem move is an instant rename -----------------------------------
open(os.path.join(ROOT, "src", "small.txt"), "w").write("hi")
job = fm.do_bulk_move_or_copy(["src/small.txt"], "dst", "move")
check("same-fs move finished before job_start even returned", job["state"] == "done" and os.path.exists(os.path.join(ROOT, "dst", "small.txt")))

# ---- 6. move across filesystems (simulate EXDEV) copies then removes source -----------
real_rename = os.rename
def fake_rename(a, b):
    raise OSError(errno.EXDEV, "cross-device")
os.rename = fake_rename
try:
    shutil.copy(big, os.path.join(ROOT, "src", "mv.bin"))
    job = fm.do_bulk_move_or_copy(["src/mv.bin"], "dst", "move")
    j = wait_job(job["id"])
finally:
    os.rename = real_rename
check("cross-device move completes", j["state"] == "done" and md5(os.path.join(ROOT, "dst", "mv.bin")) == big_md5, str(j))
check("...and only then removes the source", not os.path.exists(os.path.join(ROOT, "src", "mv.bin")))

# ---- 7. a copy that dies halfway ----------------------------------------------------------
real_sendfile = os.sendfile
calls = {"n": 0}
def flaky_sendfile(out, inp, off, cnt):
    calls["n"] += 1
    if calls["n"] == 3:
        raise OSError(errno.EIO, "Input/output error")
    return real_sendfile(out, inp, off, min(cnt, 4 * 1024 * 1024))
shutil.copy(big, os.path.join(ROOT, "src", "dies.bin"))   # fixture made BEFORE sendfile is sabotaged
os.sendfile = flaky_sendfile
try:
    job = fm.do_bulk_move_or_copy(["src/dies.bin"], "dst", "copy")
    j = wait_job(job["id"])
finally:
    os.sendfile = real_sendfile
dstnames = os.listdir(os.path.join(ROOT, "dst"))
check("failure is REPORTED (old code swallowed it)", j["state"] == "error" and "dies.bin" in j["error"] and "Input/output" in j["error"], str(j))
check("no truncated file left wearing the real name", "dies.bin" not in dstnames)
check("no leftover .part either", not [n for n in dstnames if n.endswith(".part")])
check("source untouched", md5(os.path.join(ROOT, "src", "dies.bin")) == big_md5)

# ---- 8. free-space check up front --------------------------------------------------------------
real_du = shutil.disk_usage
shutil.disk_usage = lambda p: shutil._ntuple_diskusage(100, 100, 10) if hasattr(shutil, "_ntuple_diskusage") else type("U", (), {"free": 10})()
try:
    job = fm.do_bulk_move_or_copy(["src/dies.bin"], "dst", "copy")
    j = wait_job(job["id"])
finally:
    shutil.disk_usage = real_du
check("not enough free space is reported before any copying", j["state"] == "error" and "free space" in j["error"], str(j))

# ---- 9. folder into itself ---------------------------------------------------------------------------
job = fm.do_bulk_move_or_copy(["src"], "src/inner", "copy")
j = wait_job(job["id"])
check("copying a folder into itself is refused, not recursed forever", j["state"] == "error" and "itself" in j["error"], str(j))

# ---- 10. folder copy + zip + jobs endpoint + dismiss ------------------------------------------------------
job = fm.do_bulk_move_or_copy(["src"], "dst2", "copy")
j = wait_job(job["id"])
check("folder copy works", j["state"] == "done" and os.path.exists(os.path.join(ROOT, "dst2", "src", "big.bin")), str(j))
job = fm.do_bulk_compress(ROOT, ["src/big.bin", "src/dies.bin"], "pack")
j = wait_job(job["id"])
import zipfile
check("zip job produces a valid archive", j["state"] == "done" and zipfile.ZipFile(os.path.join(ROOT, "pack.zip")).testzip() is None, str(j))
st, _, body = get("/jobs")
listed = json.loads(body)
check("/jobs lists jobs as JSON", st == 200 and len(listed) >= 5 and {"id", "label", "state", "done", "total", "error", "notes"} <= set(listed[0]))
req = urllib.request.Request(BASE + "/jobs-dismiss", data=urllib.parse.urlencode({"id": listed[0]["id"]}).encode(), method="POST")
urllib.request.urlopen(req, timeout=5).read()
check("dismiss removes a finished job", all(x["id"] != listed[0]["id"] for x in json.loads(get("/jobs")[2])))


# ---- 12. FAT32 destinations: refuse files that can never fit, before copying -------------
real_fs_type = fm.fs_type_of
os.makedirs(os.path.join(ROOT, "usb"), exist_ok=True)
usb_real = os.path.realpath(os.path.join(ROOT, "usb"))
fm.fs_type_of = lambda p: "vfat" if os.path.realpath(p).startswith(usb_real) else real_fs_type(p)
with open(os.path.join(ROOT, "src", "huge.bin"), "wb") as f:
    f.truncate(5 * 1024 ** 3)                                  # sparse 5 GiB
os.makedirs(os.path.join(ROOT, "src", "folder"), exist_ok=True)
with open(os.path.join(ROOT, "src", "folder", "huge2.bin"), "wb") as f:
    f.truncate(4 * 1024 ** 3 + 5)
try:
    job = fm.do_bulk_move_or_copy(["src/huge.bin", "src/big.bin", "src/folder"], "usb", "copy")
    j = wait_job(job["id"])
finally:
    fm.fs_type_of = real_fs_type
check("oversize file is refused with a plain-English FAT32 reason",
      j["state"] == "error" and "huge.bin is over 4 GB" in j["error"] and "FAT32" in j["error"], str(j))
check("a folder containing an oversize file is refused too", "folder is over 4 GB" in j["error"], str(j))
check("the file that does fit still copied", os.path.exists(os.path.join(ROOT, "usb", "big.bin")))
check("nothing was started for the oversize file (no wasted copying)",
      not os.path.exists(os.path.join(ROOT, "usb", "huge.bin")) and not os.path.exists(os.path.join(ROOT, "usb", "huge.bin.part")))
check("progress total counts only what will really be copied", j["total"] == len(data), str(j))
check("fs_type_of resolves a real path to some filesystem type", isinstance(fm.fs_type_of(ROOT), str))

# ---- 11. HTTP flow: POST /bulk returns at once and the strip is on the page ------------------------------------
st, _, page = get("/?path=")
check("listing page carries the progress strip", st == 200 and b'id="jobs-bar"' in page and b"/jobs" in page)

print()
print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(1 if fails else 0)
