#!/usr/bin/env python3
import http.server
import socketserver
import subprocess
import os
import json
import urllib.request
import urllib.error

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

# Power actions. This service runs as the unprivileged 'debian' user; a narrow
# rule in /etc/sudoers.d/ls210-power grants passwordless access to exactly
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
        if self.path == "/api/config":
            # Lets desktop.html build the Files/Fetcher iframe URLs from
            # wherever it's actually being loaded from, instead of a
            # hardcoded host+port baked into the page.
            body = json.dumps({
                "port_files": int(config("PORT_FILES", "8093")),
                "port_fetcher": int(config("PORT_FETCHER", "8092")),
                "data_mount": config("DATA_MOUNT", "/mnt/data"),
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
