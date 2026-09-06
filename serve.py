#!/usr/bin/env python3
import http.server
import socketserver
import subprocess
import os
import json
import hashlib
import hmac
import time
import urllib.request
import urllib.error
import urllib.parse

CONFIG_FILE = "/etc/nas-control-plane.conf"

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
    who hasn't touched /etc/nas-control-plane.conf."""
    return os.environ.get(key, _FILE_CONFIG.get(key, default))

GLANCES_BASE = "http://127.0.0.1:61208/api/3"
PORT = int(config("PORT_DESKTOP", "8095"))

# ---------- optional login gate ----------
# A separate file (not the main config) so it can be locked down to 0600 -
# it holds a password hash and the secret used to sign session cookies.
# Auth stays off (everything works exactly as before) until install.sh's
# auth step actually creates this file with a real password.
AUTH_FILE = "/etc/nas-control-plane-auth.conf"

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

_AUTH = _load_auth_file()
AUTH_ENABLED = bool(_AUTH.get("AUTH_HASH"))
SESSION_COOKIE = "nascp_session"
SESSION_LIFETIME = 60 * 60 * 24 * 14  # 14 days

def _sign(expiry):
    # Session "tokens" are self-verifying (expiry + HMAC of that expiry using
    # a secret shared by all three services), not looked up in a store - the
    # three services are independent processes with no shared memory, and a
    # signed value lets each one verify a cookie set by either of the others
    # with no IPC or shared file to keep in sync on every request.
    return hmac.new(_AUTH.get("AUTH_SECRET", "").encode(), str(expiry).encode(), hashlib.sha256).hexdigest()

def make_session_cookie():
    expiry = int(time.time()) + SESSION_LIFETIME
    return f"{expiry}.{_sign(expiry)}"

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
    return hmac.compare_digest(sig, _sign(expiry))

def check_password(password):
    try:
        salt = bytes.fromhex(_AUTH.get("AUTH_SALT", ""))
    except ValueError:
        return False
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000).hex()
    return hmac.compare_digest(computed, _AUTH.get("AUTH_HASH", ""))

def get_cookie(handler, name):
    header = handler.headers.get("Cookie", "")
    for part in header.split(";"):
        part = part.strip()
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return None

def is_authenticated(handler):
    if not AUTH_ENABLED:
        return True
    return verify_session_cookie(get_cookie(handler, SESSION_COOKIE))

LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><title>Sign in</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#dff4f1;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}}
form{{background:#fff;padding:32px;border-radius:14px;box-shadow:0 8px 28px rgba(0,0,0,.12);width:280px}}
h2{{margin:0 0 16px;color:#1f2b2a}}
input{{width:100%;box-sizing:border-box;padding:10px;margin:6px 0;border:1px solid #ccc;
border-radius:8px;font-size:14px}}
button{{width:100%;padding:10px;margin-top:8px;background:#2E9B95;color:#fff;border:0;
border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}}
button:hover{{background:#278a85}}
.err{{color:#c0392b;font-size:13px;margin-bottom:4px}}
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
    body = LOGIN_PAGE.format(error=error_html).encode()
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
    ok = hmac.compare_digest(username, _AUTH.get("AUTH_USER", "")) and check_password(password)
    if not ok:
        time.sleep(1)  # slow down automated guessing
        send_login_page(handler, failed=True)
        return
    cookie = make_session_cookie()
    handler.send_response(303)
    # Unlike filemanager.py/fetcher.py, this service has no handler for "/"
    # itself - it falls through to SimpleHTTPRequestHandler's raw directory
    # listing. The real page is desktop.html.
    handler.send_header("Location", "/desktop.html")
    handler.send_header(
        "Set-Cookie",
        f"{SESSION_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_LIFETIME}")
    handler.send_header("Content-Length", "0")
    handler.end_headers()

# Power actions. This service runs as the unprivileged 'debian' user; a narrow
# rule in /etc/sudoers.d/nas-control-plane-power grants passwordless access to exactly
# these two commands and nothing else.
POWER_ACTIONS = {
    "/system/restart": ("/usr/sbin/reboot", "Restarting"),
    "/system/shutdown": ("/usr/sbin/poweroff", "Shutting down"),
}

class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Without this, Firefox can keep serving a stale cached copy of
        # desktop.html (or the glances proxy) even across closing and
        # reopening the tab, making a real deploy look like it never landed.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_GET(self):
        if self.path == "/logout":
            # HttpOnly means client script can't clear this cookie itself
            # (that's the point - it also stops an XSS bug from doing it) -
            # only a real Set-Cookie response header can, so logout has to
            # be a genuine request, not a document.cookie write in the page.
            self.send_response(303)
            self.send_header("Location", "/desktop.html")
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not is_authenticated(self):
            send_login_page(self)
            return
        if self.path == "/api/config":
            # Lets desktop.html build the Files/Fetcher iframe URLs from
            # wherever it's actually being loaded from, instead of a
            # hardcoded host+port baked into the page.
            body = json.dumps({
                "port_files": int(config("PORT_FILES", "8093")),
                "port_fetcher": int(config("PORT_FETCHER", "8092")),
                "data_mount": config("DATA_MOUNT", "/mnt/data"),
                "auth_enabled": AUTH_ENABLED,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/glances/"):
            endpoint = self.path[len("/glances/"):]
            try:
                with urllib.request.urlopen(GLANCES_BASE + "/" + endpoint, timeout=5) as resp:
                    data = resp.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                body = str(e).encode()
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/login":
            handle_login_post(self)
            return
        if not is_authenticated(self):
            self.send_error(401)
            return
        action = POWER_ACTIONS.get(self.path)
        if action is None:
            self.send_error(404)
            return
        # Any website the user happens to be visiting could POST to this LAN
        # address from a plain HTML form, and a simple form post triggers no
        # CORS preflight - so a bare endpoint here would let an unrelated page
        # reboot the NAS. Demanding a custom header forces a preflight, and
        # this server sends no CORS headers, so the browser refuses it. The
        # desktop's own same-origin fetch sets the header and passes.
        if self.headers.get("X-NASCP-Confirm") != "yes":
            self.send_error(403, "Missing confirmation header")
            return

        command, verb = action
        body = verb.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        try:
            self.wfile.flush()
        except OSError:
            pass
        # Detached, and after a beat, so the reply is actually delivered -
        # running it inline would kill this process mid-response and the page
        # would just see a dropped connection instead of a confirmation.
        subprocess.Popen(
            ["/bin/sh", "-c", f"sleep 1; sudo -n {command}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def log_message(self, fmt, *args):
        pass

class ReusableServer(socketserver.TCPServer):
    allow_reuse_address = True

if __name__ == "__main__":
    with ReusableServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()
