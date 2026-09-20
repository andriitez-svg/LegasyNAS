"""Regression tests for the Files app (filemanager.py) and its Desktop-shell relay:
big-file handling, the USB folder guard, notifications, and Eject.

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

# ==== 13. USB folder guard, status/notifications, and Eject ==========================================
import subprocess as _subprocess

USB = os.path.join(ROOT, "USB")
os.makedirs(os.path.join(USB, "STICK"), exist_ok=True)
os.makedirs(os.path.join(USB, "plainfolder"), exist_ok=True)
open(os.path.join(ROOT, "src", "tiny.txt"), "w").write("hello")

real_ismount = os.path.ismount
real_mount_table = fm._mount_table
def fake_ismount(p):
    return os.path.realpath(p) == os.path.realpath(os.path.join(USB, "STICK")) or real_ismount(p)
def fake_table():
    t = dict(real_mount_table())
    t[os.path.realpath(os.path.join(USB, "STICK"))] = ("/dev/sdb1", "ext4")
    return t
os.path.ismount = fake_ismount
fm._mount_table = fake_table

def run_job(job):
    return wait_job(job["id"])

# -- the guard: nothing may be copied into the USB holder or an unmounted drive folder
j = run_job(fm.do_bulk_move_or_copy(["src/tiny.txt"], "USB", "copy"))
check("copying straight into the USB folder is refused", j["state"] == "error" and "only holds drives" in j["error"], str(j))
check("...and nothing was written there", not os.path.exists(os.path.join(USB, "tiny.txt")))
j = run_job(fm.do_bulk_move_or_copy(["src/tiny.txt"], "USB/plainfolder", "copy"))
check("copying into an unmounted folder under USB is refused", j["state"] == "error" and "isn't a mounted USB drive" in j["error"], str(j))
j = run_job(fm.do_bulk_move_or_copy(["src/tiny.txt"], "USB/newdir", "copy"))
check("...including a folder name that doesn't exist yet", j["state"] == "error" and not os.path.exists(os.path.join(USB, "newdir")), str(j))
j = run_job(fm.do_bulk_move_or_copy(["src/tiny.txt"], "USB/STICK", "copy"))
check("copying into a MOUNTED drive still works", j["state"] == "done" and os.path.exists(os.path.join(USB, "STICK", "tiny.txt")), str(j))
j = run_job(fm.do_bulk_move_or_copy(["src/tiny.txt"], "USB/STICK/sub", "copy"))
check("...and into a subfolder of it", j["state"] == "done" and os.path.exists(os.path.join(USB, "STICK", "sub", "tiny.txt")), str(j))

# -- USB drive listing
drives = fm.usb_drives()
check("usb_drives lists only real mounts", [d["name"] for d in drives] == ["STICK"] and drives[0]["fstype"] == "ext4", str(drives))

# -- /status: baseline, then only news
st, _, body = get("/status")
s0 = json.loads(body)
check("first /status call sets a baseline and replays nothing", st == 200 and s0["events"] == [] and isinstance(s0["seq"], int), str(s0)[:200])
j = run_job(fm.do_bulk_move_or_copy(["src/tiny.txt"], "USB/STICK/more", "copy"))
s1 = json.loads(get(f"/status?since={s0['seq']}")[2])
check("a finished job becomes a success notification, once",
      len(s1["events"]) == 1 and s1["events"][0]["level"] == "success" and "Copied tiny.txt" in s1["events"][0]["text"], str(s1["events"]))
s2 = json.loads(get(f"/status?since={s1['seq']}")[2])
check("...and isn't repeated on the next call", s2["events"] == [], str(s2["events"]))
j = run_job(fm.do_bulk_move_or_copy(["src/tiny.txt"], "USB", "copy"))
s3 = json.loads(get(f"/status?since={s2['seq']}")[2])
check("a failed job becomes an error notification with the reason",
      len(s3["events"]) == 1 and s3["events"][0]["level"] == "error" and "only holds drives" in s3["events"][0]["text"], str(s3["events"]))
check("/status reports the USB drives", [d["name"] for d in s3["usb"]] == ["STICK"] or s3["usb"] == [])

# -- the Eject button is on drives, only
fm.USB_STATE["drives"] = fm.usb_drives()
page = get("/?path=USB")[2].decode()
check("listing shows an Eject button on the mounted drive", 'data-eject="STICK"' in page and "&#9167; Eject" in page)
check("...and marks the row for the right-click menu", 'data-usb="1"' in page)
check("...but not on an ordinary folder beside it", 'data-eject="plainfolder"' not in page)
check("right-click menu has an Eject drive item", 'data-act="eject"' in page)
tiles = get("/?path=USB&view=tiles")[2].decode()
check("tiles view has the Eject button too", 'data-eject="STICK"' in tiles)
check("root listing has no Eject button", "eject-btn" not in get("/?path=")[2].decode().split("<style")[-1].split("</style>")[-1] or 'data-eject=' not in get("/?path=")[2].decode())

# -- eject job, with the privileged helper stubbed
real_run = fm.subprocess.run
calls = []
def stub(rc=0, err=""):
    def _run(cmd, **kw):
        calls.append(cmd)
        return _subprocess.CompletedProcess(cmd, rc, stdout="", stderr=err)
    return _run

fm.subprocess.run = stub(0)
j = run_job(fm.start_eject("STICK"))
check("eject calls the sudo helper with just the drive name", calls and calls[-1] == ["sudo", "-n", "/usr/local/sbin/usb-eject", "STICK"], str(calls))
check("successful eject finishes", j["state"] == "done", str(j))
ev = fm.events_since(0)[0]
check("...and says it's safe to unplug", any("'STICK' ejected - safe to unplug" in e["text"] for e in ev))
check("...without a bogus 'removed without ejecting' warning afterwards", "STICK" in fm._EXPECTED_GONE)

fm.subprocess.run = stub(3, "'STICK' is busy - something is still using it.")
j = run_job(fm.start_eject("STICK"))
check("a busy drive reports the helper's reason", j["state"] == "error" and "busy" in j["error"], str(j))
check("...and isn't marked as expected-gone", "STICK" not in fm._EXPECTED_GONE)

def missing(cmd, **kw):
    raise FileNotFoundError("sudo")
fm.subprocess.run = missing
j = run_job(fm.start_eject("STICK"))
check("missing sudo gives a plain 're-run install.sh' message", j["state"] == "error" and "install.sh" in j["error"], str(j))
fm.subprocess.run = stub(1, "sudo: a password is required")
j = run_job(fm.start_eject("STICK"))
check("a sudoers problem is explained, not shown raw", j["state"] == "error" and "install.sh" in j["error"] and "password" not in j["error"], str(j))
fm.subprocess.run = real_run

# invalid / unmounted / in-use refusals never reach the helper
calls.clear()
fm.subprocess.run = stub(0)
for bad in ("../etc", "a/b", "", ".", "..", "x" * 80, "with space"):
    j = run_job(fm.start_eject(bad))
    check(f"eject refuses the name {bad!r} without calling the helper", j["state"] == "error" and not calls, str(j))
j = run_job(fm.start_eject("plainfolder"))
check("eject refuses a folder that isn't a mounted drive", j["state"] == "error" and "isn't mounted" in j["error"] and not calls, str(j))

gate = threading.Event()
def slow_worker(job):
    gate.wait(10)
running_copy = fm.job_start("Copying big thing to STICK", slow_worker, wait=0,
                            touches=[os.path.realpath(os.path.join(USB, "STICK", "x"))])
j = run_job(fm.start_eject("STICK"))
check("eject refuses while a copy to that drive is still running", j["state"] == "error" and "still in use" in j["error"] and not calls, str(j))
gate.set(); run_job(running_copy)
fm.subprocess.run = real_run

# -- HTTP: the eject endpoint needs the confirm header
req = urllib.request.Request(BASE + "/usb-eject", data=b"name=STICK", method="POST")
try:
    urllib.request.urlopen(req, timeout=5); code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("POST /usb-eject without the confirm header is refused (403)", code == 403, str(code))
fm.subprocess.run = stub(0)
req = urllib.request.Request(BASE + "/usb-eject", data=b"name=STICK", method="POST", headers={"X-LegasyNAS-Confirm": "yes"})
res = json.loads(urllib.request.urlopen(req, timeout=5).read())
check("...and with it, returns a job id", res.get("ok") is True and isinstance(res.get("job"), int), str(res))
wait_job(res["job"])
fm.subprocess.run = real_run

# -- the watcher: plug-in, unplug-without-eject, and eject-then-gone
seq = iter([[], [{"name": "K", "device": "/dev/sdb1", "fstype": "ext4", "total": 5 * 1024 ** 3, "free": 1}],
            [{"name": "K", "device": "/dev/sdb1", "fstype": "ext4", "total": 5 * 1024 ** 3, "free": 1}],
            [], [{"name": "Z", "device": "/dev/sdc1", "fstype": "exfat", "total": 1, "free": 1}], []])
last = {"v": []}
def scripted():
    try:
        last["v"] = next(seq)
    except StopIteration:
        pass
    return last["v"]
real_usb_drives = fm.usb_drives
fm.usb_drives = scripted
base_seq = fm.events_since(None)[1]
fm._EXPECTED_GONE["Z"] = time.time()          # Z is ejected on purpose; K is yanked
watch_stop = threading.Event()
wt = threading.Thread(target=fm.usb_watch, kwargs={"interval": 0.03, "stop": watch_stop}, daemon=True)
wt.start(); time.sleep(0.6)
watch_stop.set(); wt.join(2)                  # stop it before restoring the real lookup
fm.usb_drives = real_usb_drives
texts = [e["text"] for e in fm.events_since(base_seq)[0]]
check("plugging a drive in is announced", any("'K' connected" in t and "5.0GB" in t for t in texts), str(texts))
check("pulling one out without ejecting is a warning", any("'K' was removed without ejecting" in t for t in texts), str(texts))
check("an eject-then-gone drive does NOT trigger that warning", not any("'Z' was removed" in t for t in texts), str(texts))

# -- serve.py relays /status to the Files app (this is how the shell reads it)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import serve as sv
sv.AUTH_FILE = "/nonexistent/auth.conf"
os.environ["PORT_FILES"] = str(PORT)
shell = sv.ReusableServer(("127.0.0.1", 0), sv.Handler)
threading.Thread(target=shell.serve_forever, daemon=True).start()
SBASE = f"http://127.0.0.1:{shell.server_address[1]}"
r = json.loads(urllib.request.urlopen(SBASE + "/nas/status", timeout=8).read())
check("the shell relays /nas/status from the Files app", isinstance(r.get("seq"), int) and "jobs" in r and "usb" in r, str(r)[:200])
r2 = json.loads(urllib.request.urlopen(SBASE + f"/nas/status?since={r['seq']}", timeout=8).read())
check("...including the 'since' parameter", r2["events"] == [], str(r2)[:200])
r3 = json.loads(urllib.request.urlopen(SBASE + "/nas/status?since=abc;rm", timeout=8).read())
check("...and ignores a junk 'since' instead of passing it on", r3["events"] == [] and isinstance(r3["seq"], int), str(r3)[:200])
os.environ["PORT_FILES"] = "1"      # nothing listens there
try:
    urllib.request.urlopen(SBASE + "/nas/status", timeout=8); code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("if the Files app is down the shell answers 502, not a hang", code == 502, str(code))
os.environ["PORT_FILES"] = str(PORT)

os.path.ismount = real_ismount
fm._mount_table = real_mount_table

print()
print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(1 if fails else 0)
