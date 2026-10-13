"""Runtime checks with macOS process/network boundaries replaced, never real apps."""

import copy
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from uu_direct import runtime


IDS = ["com.netease.uuremote", "com.netease.uuremote.server",
       "com.netease.uuremote.agent", "com.netease.uuremote.daemon"]
EXTENSION = ("/Library/SystemExtensions/example/"
             "com.interceptsuite.ProxyBridge.extension.systemextension/Contents/MacOS/"
             "com.interceptsuite.ProxyBridge.extension")
START = "[Extension com.interceptsuite.ProxyBridge]: Started with error (null)"
LIFECYCLE_PREFIX = "[Extension com.interceptsuite.ProxyBridge]: "


def settings():
    return {"schema_version": 1, "project": "uu-direct-macos", "interface": "en0",
            "port": 18443, "dns_server": "223.5.5.5", "uu_app": "/Applications/UURemote.app",
            "proxybridge_app": "/Applications/ProxyBridge.app", "proxy_id": "test-upstream",
            "profile_name": "UU-only"}


def preferences():
    return {"activeProfile": "UU-only", "proxyConfigs": [
        {"id": "test-upstream", "type": "socks5", "host": "127.0.0.1", "port": 18443}],
        "proxyRules": [{"processNames": name, "targetHosts": "*", "targetPorts": "*",
                        "protocol": "BOTH", "action": "test-upstream", "enabled": True}
                       for name in IDS]}


def events(*messages, pid=22):
    return [{"timestamp": f"2026-10-13 10:00:{i:02d}.000000+0800", "processID": pid,
             "eventMessage": message} for i, message in enumerate(messages)]


class FakeMac:
    """Only external commands and the clock are simulated; orchestration is real."""

    def __init__(self, base, listening=True):
        self.base = base
        self.listening = listening
        self.commands = []
        self.now = 0.0
        self.prefs = preferences()
        self.logs = events(START)
        self.log_reads = 0
        self.stop_on_second_read = False
        self.exit_channel_on_second_read = False
        self.change_profile_on_second_read = False
        self.alive = True
        self.terminated = False
        self.spawned = False
        self.health_code = "404"
        self.health_returncode = 0
        self.identity_override = None

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(command, 0 if self.listening else 1,
                                               "11\n" if self.listening else "", "")
        if command[0] == "/bin/ps":
            if "-axo" in command:
                text = ("21 /Applications/ProxyBridge.app/Contents/MacOS/ProxyBridge\n"
                        f"22 {EXTENSION}\n")
            else:
                pid = command[command.index("-p") + 1]
                field = command[-1]
                if pid == "11":
                    if not self.alive:
                        return subprocess.CompletedProcess(command, 1, "", "")
                    identity = self.identity_override or (
                        str(self.base / "bin/sing-box"),
                        f"{self.base / 'bin/sing-box'} run -c {self.base / 'config.json'}",
                        "Tue Oct 13 10:00:00 2026")
                else:
                    identity = (EXTENSION, EXTENSION, "Tue Oct 13 10:00:00 2026")
                text = dict(zip(("comm=", "args=", "lstart="), identity))[field]
            return subprocess.CompletedProcess(command, 0, text, "")
        if command[0] == "/usr/bin/curl":
            return subprocess.CompletedProcess(command, self.health_returncode, self.health_code, "")
        if command[0] == "/usr/bin/defaults":
            return subprocess.CompletedProcess(command, 0, plistlib.dumps(self.prefs), b"")
        if command[0] == "/usr/bin/open":
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == "/usr/bin/log":
            self.log_reads += 1
            if self.log_reads >= 2 and self.exit_channel_on_second_read:
                self.alive = False
            if self.log_reads >= 2 and self.change_profile_on_second_read:
                self.prefs["activeProfile"] = "Other"
            result = (events(START, LIFECYCLE_PREFIX + "Calling stopTunnelWithReason: 1")
                      if self.stop_on_second_read and self.log_reads >= 2 else self.logs)
            return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
        raise AssertionError(f"Unexpected system command: {command}")

    def popen(self, command, **kwargs):
        self.commands.append(command)
        self.spawned = True
        self.listening = True
        owner = self

        class Child:
            pid = 11

            def poll(self):
                return None if owner.alive else 0

            def terminate(self):
                owner.terminated = True
                owner.alive = False

            def wait(self, timeout=None):
                return 0

        return Child()

    def sleep(self, seconds):
        self.now += seconds
        if any(c[0] == "/usr/bin/open" and c[-1] == "/Applications/UURemote.app"
               for c in self.commands):
            raise SystemExit(0)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve() / "install with spaces"
        self.base.mkdir()
        self.subject = runtime.Runtime(settings(), base=self.base)
        self.mac = FakeMac(self.base)
        self.addCleanup(patch.stopall)
        patch.object(runtime.subprocess, "run", side_effect=self.mac.run).start()
        patch.object(runtime.subprocess, "Popen", side_effect=self.mac.popen).start()
        patch.object(runtime.time, "sleep", side_effect=self.mac.sleep).start()
        patch.object(runtime.time, "monotonic", side_effect=lambda: self.mac.now).start()
        patch.object(runtime, "log").start()

    def opened_apps(self):
        return [c[-1] for c in self.mac.commands if c[0] == "/usr/bin/open"]

    def test_open_only_checks_channel_and_current_extension_before_uu(self):
        self.assertEqual(self.subject.run(open_only=True), 0)
        commands = self.mac.commands
        curl = next(i for i, c in enumerate(commands) if c[0] == "/usr/bin/curl")
        pb = next(i for i, c in enumerate(commands) if c[0] == "/usr/bin/open")
        logs = [i for i, c in enumerate(commands) if c[0] == "/usr/bin/log"]
        uu = next(i for i, c in enumerate(commands)
                  if c[0] == "/usr/bin/open" and c[-1] == "/Applications/UURemote.app")
        self.assertLess(curl, pb)
        self.assertLess(pb, logs[0])
        self.assertGreaterEqual(len(logs), 2)
        self.assertLess(logs[-1], uu)
        self.assertFalse(self.mac.spawned)
        self.assertFalse(self.mac.terminated)

    def test_open_only_missing_channel_never_spawns_or_opens_apps(self):
        self.mac.listening = False
        with self.assertRaises(RuntimeError):
            self.subject.run(open_only=True)
        self.assertFalse(self.mac.spawned)
        self.assertEqual(self.opened_apps(), [])

    def test_wrong_port_owner_is_rejected_without_opening_apps(self):
        self.mac.identity_override = ("/tmp/other", "/tmp/other run", "Tue Oct 13 10:00:00 2026")
        with self.assertRaisesRegex(RuntimeError, "different"):
            self.subject.run(open_only=True)
        self.assertEqual(self.opened_apps(), [])
        self.assertFalse(self.mac.spawned)

    def test_same_binary_different_config_or_legacy_binary_is_rejected(self):
        binary = str(self.base / "bin/sing-box")
        for identity in [(binary, f"{binary} run -c /tmp/other.json", "today"),
                         ("/old/bin/sing-box", "/old/bin/sing-box run -c /old/config.json", "today")]:
            with self.subTest(identity=identity):
                self.mac.identity_override = identity
                with self.assertRaises(RuntimeError):
                    self.subject.channel_owner()

    def test_curl_ignores_curlrc_and_environment_proxy_bypasses_with_verified_tls(self):
        self.assertTrue(self.subject.channel_healthy())
        command = self.mac.commands[-1]
        self.assertEqual(command[:2], ["/usr/bin/curl", "-q"])
        self.assertEqual(command[command.index("--proxy") + 1], "http://127.0.0.1:18443")
        self.assertEqual(command[command.index("--noproxy") + 1], "")
        self.assertNotIn("--insecure", command)
        self.assertNotIn("-k", command)
        self.assertTrue(command[-1].startswith("https://"))

    def test_health_accepts_http_errors_but_rejects_transport_failure(self):
        for code, result, expected in [("200", 0, True), ("404", 0, True), ("503", 0, True),
                                       ("000", 0, False), ("404", 60, False), ("garbage", 0, False)]:
            with self.subTest(code=code, result=result):
                self.mac.health_code, self.mac.health_returncode = code, result
                self.assertEqual(self.subject.channel_healthy(), expected)

    def test_proxybridge_accepts_list_json_text_and_data_encodings(self):
        for encode in (lambda x: x, json.dumps, lambda x: json.dumps(x).encode()):
            prefs = preferences()
            prefs["proxyConfigs"] = encode(prefs["proxyConfigs"])
            prefs["proxyRules"] = encode(prefs["proxyRules"])
            self.subject.validate_proxybridge(prefs)

    def test_proxybridge_rejects_wrong_active_profile_or_upstream_id(self):
        for key in ("profile", "id", "port", "multiple"):
            prefs = preferences()
            if key == "profile":
                prefs["activeProfile"] = "Other"
            elif key == "multiple":
                prefs["proxyConfigs"].append(copy.deepcopy(prefs["proxyConfigs"][0]))
            else:
                prefs["proxyConfigs"][0][key] = "other" if key == "id" else 12450
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.subject.validate_proxybridge(prefs)

    def test_proxybridge_rejects_mixed_missing_or_weakened_rules(self):
        mutations = [lambda r: r.append({**r[0], "processNames": "*"}), lambda r: r.pop(),
                     lambda r: r[0].update(processNames="com.example.Other"),
                     lambda r: r[0].update(action="DIRECT"), lambda r: r[0].update(enabled=False),
                     lambda r: r[0].update(enabled=1), lambda r: r[0].update(protocol="TCP")]
        for mutate in mutations:
            prefs = preferences()
            mutate(prefs["proxyRules"])
            with self.assertRaises(RuntimeError):
                self.subject.validate_proxybridge(prefs)

    def test_current_extension_pid_and_birth_time_scope_logs(self):
        self.subject.prepare_proxybridge(timeout=10)
        command = next(c for c in self.mac.commands if c[0] == "/usr/bin/log")
        self.assertEqual(command[command.index("--start") + 1], "2026-10-13 10:00:00")
        predicate = command[command.index("--predicate") + 1]
        self.assertIn("processIdentifier == 22", predicate)
        self.assertIn('eventMessage BEGINSWITH[c] "' + LIFECYCLE_PREFIX + 'Started with error"', predicate)
        self.assertNotIn('eventMessage BEGINSWITH[c] "' + LIFECYCLE_PREFIX + '"', predicate)

    def test_later_stop_failure_or_pending_restart_invalidates_started_event(self):
        for message in ("Stopped with error (null)", "Stop requested", "stopTunnel requested", "Provider failed",
                        "Provider failure", "Started with error permission denied", "startTunnel requested"):
            self.mac.logs = events(START, LIFECYCLE_PREFIX + message)
            with self.subTest(message=message), self.assertRaises(RuntimeError):
                self.subject.prepare_proxybridge(timeout=5)

    def test_flow_failures_do_not_invalidate_extension_start(self):
        self.mac.logs = events(START, "nwconnection failed: connection reset",
                               "SOCKS idle connection failure", "Provider failed",
                               LIFECYCLE_PREFIX + "provider rejected new flow TCP",
                               LIFECYCLE_PREFIX + "Calling handleNewFlow: connection failed")
        self.assertEqual(self.subject.run(open_only=True), 0)
        self.assertIn("/Applications/UURemote.app", self.opened_apps())

    def test_other_pid_started_event_cannot_open_uu(self):
        self.mac.logs = events(START, pid=999)
        with self.assertRaises(RuntimeError):
            self.subject.prepare_proxybridge(timeout=5)
        self.assertNotIn("/Applications/UURemote.app", self.opened_apps())

    def test_stop_during_configuration_grace_period_prevents_uu_launch(self):
        self.mac.stop_on_second_read = True
        with self.assertRaises(RuntimeError):
            self.subject.run(open_only=True)
        self.assertNotIn("/Applications/UURemote.app", self.opened_apps())
        self.assertFalse(self.mac.spawned)
        self.assertFalse(self.mac.terminated)

    def test_invalid_profile_prevents_proxybridge_and_uu_launch(self):
        self.mac.prefs["activeProfile"] = "Other"
        with self.assertRaises(RuntimeError):
            self.subject.run(open_only=True)
        self.assertEqual(self.opened_apps(), [])
        self.assertFalse(self.mac.terminated)

    def test_changed_profile_during_grace_period_prevents_uu_launch(self):
        self.mac.change_profile_on_second_read = True
        with self.assertRaises(RuntimeError):
            self.subject.run(open_only=True)
        self.assertNotIn("/Applications/UURemote.app", self.opened_apps())

    def test_channel_exit_during_proxybridge_start_prevents_uu_launch(self):
        self.mac.exit_channel_on_second_read = True
        with self.assertRaises(RuntimeError):
            self.subject.run(open_only=True)
        self.assertNotIn("/Applications/UURemote.app", self.opened_apps())
        self.assertFalse(self.mac.terminated)

    def test_saved_profile_cli_preflight_only_reads_preferences(self):
        runtime.validate_proxybridge_saved(settings())
        self.assertEqual([c[0] for c in self.mac.commands], ["/usr/bin/defaults"])
        self.assertFalse(self.mac.spawned)

    def test_cold_start_failure_only_cleans_up_its_own_child(self):
        self.mac.listening = False
        self.mac.prefs["activeProfile"] = "Other"
        with self.assertRaises(RuntimeError):
            self.subject.run()
        self.assertTrue(self.mac.spawned)
        self.assertTrue(self.mac.terminated)
        self.assertEqual(self.opened_apps(), [])

    def test_cold_start_completes_order_before_supervising_and_cleans_owned_child(self):
        self.mac.listening = False
        with self.assertRaises(SystemExit):
            self.subject.run()
        self.assertTrue(self.mac.spawned)
        self.assertEqual(self.opened_apps(), ["/Applications/ProxyBridge.app", "/Applications/UURemote.app"])
        self.assertTrue(self.mac.terminated)

    def test_settings_reject_bad_schema_and_unsafe_values_before_any_commands(self):
        for changed in ({"schema_version": 2}, {"project": "other"}, {"port": True},
                        {"port": 0}, {"port": 65536}, {"uu_app": "relative.app"},
                        {"interface": ""}, {"proxy_id": ""}, {"profile_name": ""}):
            with self.subTest(changed=changed), self.assertRaises((ValueError, RuntimeError)):
                runtime.Runtime({**settings(), **changed}, base=self.base)
        self.assertEqual(self.mac.commands, [])


if __name__ == "__main__":
    unittest.main()
