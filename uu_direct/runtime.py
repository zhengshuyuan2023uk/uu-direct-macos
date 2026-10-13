#!/usr/bin/env python3
"""Standalone, standard-library supervisor for an installed UU-only channel.

Copy this file beside settings.json and config.json; no package import is needed.
ProxyBridge exposes no rules-applied acknowledgement API. Its current process's
lifecycle logs provide best-effort evidence only, never proof that rules are live.
"""

import argparse
import datetime
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import subprocess
import time


BASE = Path(__file__).resolve().parent
UU_IDENTIFIERS = frozenset("com.netease.uuremote" + suffix
                           for suffix in ("", ".server", ".agent", ".daemon"))
PB_DOMAIN = "com.interceptsuite.ProxyBridge"
PB_EXTENSION = "com.interceptsuite.ProxyBridge.extension"
PB_LOG_PREFIX = "[Extension com.interceptsuite.ProxyBridge]: "
PB_STARTED = PB_LOG_PREFIX + "Started with error (null)"
PB_LIFECYCLE_PREFIXES = (
    "Started with error", "Stopped", "Stopping", "Stop requested", "Starting",
    "Calling startTunnel", "Calling stopTunnel", "startTunnel", "stopTunnel",
    "Provider failed", "Provider failure", "Failed to start", "Failed to stop",
)
PB_LIFECYCLE_EVENT = re.compile(
    r"^(?:Started with error|Stopped\b|Stopping\b|Stop requested\b|Starting\b|"
    r"(?:Calling )?(?:startTunnel|stopTunnel)(?:With\w*)?\b|"
    r"Provider (?:failed|failure)\b|Failed to (?:start|stop)\b)", re.IGNORECASE)


def log(message):
    print(f'{datetime.datetime.now().astimezone().isoformat(timespec="seconds")} {message}',
          flush=True)


def load_settings(path=BASE / "settings.json"):
    """Load settings without starting processes or modifying the installation."""
    with Path(path).open(encoding="utf-8") as source:
        settings = json.load(source)
    _validate_settings(settings)
    return settings


def _validate_settings(settings):
    if (not isinstance(settings, dict) or type(settings.get("schema_version")) is not int
            or settings["schema_version"] != 1 or settings.get("project") != "uu-direct-macos"):
        raise ValueError("Unsupported UU Direct settings schema/project")
    port = settings.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Channel port must be an integer between 1 and 65535")
    for key in ("interface", "proxy_id", "profile_name", "dns_server", "uu_app", "proxybridge_app"):
        value = settings.get(key)
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError(f"Settings require a nonempty, single-line {key}")
    for key in ("uu_app", "proxybridge_app"):
        if not Path(settings[key]).is_absolute():
            raise ValueError(f"{key} must be an absolute application path")
    try:
        ipaddress.ip_address(settings["dns_server"])
    except ValueError as error:
        raise ValueError("dns_server must be an IP address") from error


def _preference_list(value, key):
    """Defaults may export an array, UTF-8 JSON text, or plist Data containing JSON."""
    try:
        if isinstance(value, (bytes, str)):
            value = json.loads(value)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError(f"ProxyBridge {key} cannot be decoded") from error
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise RuntimeError(f"ProxyBridge {key} must contain an array of objects")
    return value


def _read_proxybridge_preferences():
    try:
        result = subprocess.run(["/usr/bin/defaults", "export", PB_DOMAIN, "-"],
                                capture_output=True, check=True, timeout=10)
        prefs = plistlib.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException) as error:
        raise RuntimeError("Could not read saved ProxyBridge preferences") from error
    if not isinstance(prefs, dict):
        raise RuntimeError("Saved ProxyBridge preferences are invalid")
    return prefs


def validate_proxybridge_saved(settings):
    """Read-only preflight used by the installer CLI; does not open either app."""
    Runtime(settings).validate_proxybridge(_read_proxybridge_preferences())


class Runtime:
    def __init__(self, settings, base=BASE):
        _validate_settings(settings)
        self.settings = dict(settings)
        self.base = Path(base).resolve()
        self.port = settings["port"]
        self.binary = str(self.base / "bin/sing-box")
        self.config = str(self.base / "config.json")

    @staticmethod
    def process_identity(pid):
        fields = []
        for field in ("comm=", "args=", "lstart="):
            result = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", field],
                                    capture_output=True, text=True, timeout=5,
                                    env={**os.environ, "LC_ALL": "C"})
            if result.returncode != 0 or not result.stdout.strip():
                return None
            fields.append(result.stdout.strip())
        return tuple(fields)

    def channel_owner(self):
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{self.port}",
                                 "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5)
        if result.returncode not in (0, 1):
            raise RuntimeError("Could not inspect the dedicated channel port")
        pids = set(result.stdout.split())
        if not pids:
            return None
        if len(pids) != 1 or not next(iter(pids)).isdigit():
            raise RuntimeError("Dedicated port has ambiguous owners; no process was changed")
        pid = int(next(iter(pids)))
        identity = self.process_identity(pid)
        if identity is None:
            return None  # The listener exited between lsof and ps.
        expected_args = f"{self.binary} run -c {self.config}"
        if identity[:2] != (self.binary, expected_args):
            raise RuntimeError("Dedicated port belongs to a different program/config; no process was changed")
        return pid, identity

    def channel_healthy(self):
        try:
            result = subprocess.run(
                ["/usr/bin/curl", "-q", "--silent", "--show-error", "--noproxy", "",
                 "--proxy", f"http://127.0.0.1:{self.port}", "--connect-timeout", "3",
                 "--max-time", "8", "--output", "/dev/null", "--write-out", "%{http_code}",
                 "https://api.nrd.nie.163.com/"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        # -q must be first to ignore curlrc; explicit proxy/noproxy defeat inherited
        # proxy bypasses. No -k: an HTTP 404 still proves CONNECT and verified TLS.
        code = result.stdout.strip()
        return result.returncode == 0 and code.isascii() and code.isdigit() and 100 <= int(code) <= 599

    def wait_until_ready(self, is_alive, expected_pid):
        attempts = 0
        while is_alive():
            owner = self.channel_owner()
            if owner is not None and owner[0] != expected_pid:
                raise RuntimeError("The dedicated channel listener changed during startup")
            if owner is not None and self.channel_healthy() and is_alive():
                log("channel_ready: HTTP CONNECT, DNS and verified TLS succeeded")
                return
            attempts += 1
            if attempts == 1 or attempts % 12 == 0:
                log("waiting_for_network: UU has not been started")
            time.sleep(5)
        raise RuntimeError("Dedicated channel exited before becoming ready")

    def validate_proxybridge(self, prefs):
        if not isinstance(prefs, dict) or prefs.get("activeProfile") != self.settings["profile_name"]:
            raise RuntimeError("ProxyBridge active profile does not match this installation")
        configs = _preference_list(prefs.get("proxyConfigs"), "proxyConfigs")
        rules = _preference_list(prefs.get("proxyRules"), "proxyRules")
        if len(configs) != 1:
            raise RuntimeError("ProxyBridge must have exactly one UU-only SOCKS5 upstream")
        config = configs[0]
        if (config.get("id"), config.get("type"), config.get("host"), config.get("port")) != (
                self.settings["proxy_id"], "socks5", "127.0.0.1", self.port) or type(config.get("port")) is not int:
            raise RuntimeError("ProxyBridge upstream does not match this dedicated UU channel")
        if any(config.get(key) for key in ("username", "password")):
            raise RuntimeError("The dedicated UU channel does not use proxy authentication")
        if len(rules) != 4 or any(not isinstance(r.get("processNames"), str) for r in rules):
            raise RuntimeError("ProxyBridge must have exactly the four UU rules")
        if {r["processNames"] for r in rules} != UU_IDENTIFIERS:
            raise RuntimeError("ProxyBridge must have exactly the four UU rules")
        if any(r.get("enabled") is not True or (
                r.get("protocol"), r.get("targetHosts"), r.get("targetPorts"), r.get("action")) != (
                    "BOTH", "*", "*", self.settings["proxy_id"]) for r in rules):
            raise RuntimeError("All four UU rules must use the dedicated channel for TCP and UDP")

    def _extension_started_best_effort(self, pid, identity):
        """Reject later negative lifecycle evidence; this is not a rules-ready ACK."""
        try:
            started = datetime.datetime.strptime(identity[2], "%a %b %d %H:%M:%S %Y")
            # The extension prefix alone is insufficient: NetworkExtension uses it
            # for every application's flows too. Filter lifecycle messages at the
            # log source, avoiding bulk flow metadata and unrelated socket failures.
            lifecycle_filter = " OR ".join(
                "eventMessage BEGINSWITH[c] " + json.dumps(PB_LOG_PREFIX + prefix)
                for prefix in PB_LIFECYCLE_PREFIXES)
            predicate = (f'processIdentifier == {int(pid)} AND process == "{PB_EXTENSION}" '
                         f"AND ({lifecycle_filter})")
            result = subprocess.run(
                ["/usr/bin/log", "show", "--start", started.strftime("%Y-%m-%d %H:%M:%S"),
                 "--style", "json", "--info", "--debug", "--predicate",
                 predicate],
                capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return False
            events = json.loads(result.stdout)
            if not isinstance(events, list):
                return False
            events = [event for event in events if isinstance(event, dict)
                      and str(event.get("processID")) == str(pid)
                      and isinstance(event.get("timestamp"), str)]
            events.sort(key=lambda event: event["timestamp"])
            started_evidence = False
            for event in events:
                message = event.get("eventMessage", "")
                if not isinstance(message, str) or not message.startswith(PB_LOG_PREFIX):
                    continue
                body = message[len(PB_LOG_PREFIX):]
                if message.strip() == PB_STARTED:
                    started_evidence = True
                elif PB_LIFECYCLE_EVENT.match(body):
                    # Conservatively invalidate earlier success, including a new
                    # pending start; only a subsequent successful start can restore it.
                    started_evidence = False
            return started_evidence
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return False

    def prepare_proxybridge(self, timeout=45):
        self.validate_proxybridge(_read_proxybridge_preferences())
        subprocess.run(["/usr/bin/open", "-g", "-a", self.settings["proxybridge_app"]],
                       check=True, timeout=20)
        deadline = time.monotonic() + timeout
        app_executable = str(Path(self.settings["proxybridge_app"]) / "Contents/MacOS/ProxyBridge")
        extension_suffix = f"/{PB_EXTENSION}.systemextension/Contents/MacOS/{PB_EXTENSION}"
        while time.monotonic() < deadline:
            processes = subprocess.run(["/bin/ps", "-ww", "-axo", "pid=,comm="],
                                       capture_output=True, text=True, check=True, timeout=5)
            entries = [line.strip().split(None, 1) for line in processes.stdout.splitlines()]
            entries = [entry for entry in entries if len(entry) == 2 and entry[0].isdigit()]
            app_running = any(path == app_executable for _, path in entries)
            for pid, path in entries if app_running else []:
                if not (path.startswith("/Library/SystemExtensions/") and path.endswith(extension_suffix)):
                    continue
                identity = self.process_identity(pid)
                if identity is None or identity[0] != path:
                    continue
                if not self._extension_started_best_effort(pid, identity):
                    continue
                # This grace interval and second read catch an observed stop during
                # startup. Neither establishes that the extension has applied rules.
                time.sleep(2)
                if (time.monotonic() >= deadline or self.process_identity(pid) != identity
                        or not self._extension_started_best_effort(pid, identity)):
                    continue
                self.validate_proxybridge(_read_proxybridge_preferences())
                if time.monotonic() >= deadline or self.process_identity(pid) != identity:
                    continue
                log("proxybridge_start_observed_best_effort: current extension process has no later "
                    "observed stop/failure; saved UU profile checked; no rules-applied ACK available")
                return
            time.sleep(2)
        raise RuntimeError(
            "Could not confirm ProxyBridge extension startup within the check deadline; opening UU deferred. "
            "Check logs/startup.log and logs/startup-error.log. If needed, reconnect the extension in "
            "ProxyBridge and retry; this tool does not restart the extension automatically.")

    def open_uu(self):
        subprocess.run(["/usr/bin/open", "-g", "-a", self.settings["uu_app"]], check=True, timeout=20)
        log("uu_launch_requested: channel healthy; ProxyBridge best-effort startup check passed")

    def run(self, open_only=False):
        child = output = None
        try:
            owner = self.channel_owner()
            if owner is None:
                if open_only:
                    raise RuntimeError("The managed channel is not listening yet")
                (self.base / "logs").mkdir(exist_ok=True)
                output = (self.base / "logs/channel-process.log").open("ab")
                child = subprocess.Popen([self.binary, "run", "-c", self.config], cwd=self.base,
                                         stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
                pid = child.pid
                is_alive = lambda: child.poll() is None
                log(f"channel_started: pid={pid}")
            else:
                pid, identity = owner
                is_alive = lambda: self.process_identity(pid) == identity
                log(f"channel_reused: pid={pid}; existing process remains independently owned")
            self.wait_until_ready(is_alive, pid)
            self.prepare_proxybridge()
            owner = self.channel_owner()
            if not is_alive() or owner is None or owner[0] != pid or not self.channel_healthy():
                raise RuntimeError("Dedicated channel became unavailable while ProxyBridge started; UU was not opened")
            self.open_uu()
            if open_only:
                return 0
            while is_alive():
                time.sleep(5)
            raise RuntimeError("Dedicated channel exited; launchd will restart ordered startup")
        finally:
            # Adopted listeners are never children of this invocation. Do not kill
            # them on a failed preflight, signal, or supervisor exit.
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if output is not None:
                output.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open-only", action="store_true",
                        help="Open UU using an existing matching channel; never start a new channel")
    options = parser.parse_args(argv)

    def stop(_signum, _frame):
        raise SystemExit(0)

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return Runtime(load_settings()).run(open_only=options.open_only)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log(f"startup_failed: {error}")
        raise SystemExit(1)
