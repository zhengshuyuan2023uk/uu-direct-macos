"""Safety tests use temporary homes, fake binaries and mocked OS commands only."""

import argparse
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from uu_direct import setup
from uu_direct.runtime import Runtime


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.home = self.temp / "中文用户 with spaces"
        self.home.mkdir()
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))
        self.stack.enter_context(patch.object(setup.platform, "system", return_value="Darwin"))
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.binary = self.temp / "sing-box"
        self.binary.write_bytes(b"fake official binary, never executed")
        self.asset = {"url": "https://example.invalid/fake.tar.gz", "sha256": "0" * 64,
                      "binary_sha256": setup.sha256(self.binary.read_bytes())}
        self.stack.enter_context(patch.object(setup, "dependency", return_value=("arm64", self.asset)))
        source = self.temp / "source"
        source.mkdir()
        (source / "runtime.py").write_text("# inert runtime fixture\n")
        self.stack.enter_context(patch.object(setup, "__file__", str(source / "setup.py")))
        self.commands = []
        self.command_mock = self.stack.enter_context(patch.object(setup, "run", side_effect=self.fake_run))
        self.port_mock = self.stack.enter_context(patch.object(setup, "port_available"))
        self.runtime = types.ModuleType("uu_direct.runtime")
        self.runtime.validate_proxybridge_saved = Mock()
        self.stack.enter_context(patch.dict(sys.modules, {"uu_direct.runtime": self.runtime}))
        apps = []
        for name, identifier in (("UU 测试.app", "com.netease.uuremote"), ("ProxyBridge.app", "com.interceptsuite.ProxyBridge")):
            app = self.temp / name
            (app / "Contents").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": identifier,
                                                                    "CFBundleShortVersionString": "4.0.0"}))
            apps.append(app)
        self.args = argparse.Namespace(interface="en0", port=19443, dns_server="223.5.5.5",
                                       sing_box=self.binary, uu_app=apps[0], proxybridge_app=apps[1])

    def fake_run(self, command, **kwargs):
        self.commands.append(command)
        if command[:2] == ["/bin/launchctl", "print"]:
            return subprocess.CompletedProcess(command, 113, "", "Could not find service")
        if command[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(command, 1, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    def install(self):
        setup.install(self.args)
        return setup.locations()

    def job_output(self, plist, parsed):
        arguments = "\n".join("\t" + value for value in parsed["ProgramArguments"])
        return f"job = {{\n path = {plist}\n state = running\n arguments = {{\n{arguments}\n }}\n}}"

    def test_install_prepares_only_and_all_ports_match_with_unicode_paths(self):
        base, plist = self.install()
        settings = json.loads((base / "settings.json").read_text())
        config = json.loads((base / "config.json").read_text())
        profile = json.loads((base / "proxybridge-profile.json").read_text())
        self.assertEqual(settings["port"], self.args.port)
        self.assertEqual(config["inbounds"][0]["listen_port"], self.args.port)
        self.assertEqual(profile["proxyConfigs"][0]["port"], self.args.port)
        self.assertEqual({rule["processNames"] for rule in profile["proxyRules"]}, set(setup.UU_IDS))
        self.assertEqual(len(profile["proxyRules"]), 4)
        self.assertFalse(profile["trafficLoggingEnabled"])
        self.assertFalse(profile["runAtStartup"])
        loaded = plistlib.loads(plist.read_bytes())
        self.assertEqual(loaded["ProgramArguments"][1], str(base / "runtime.py"))
        self.assertTrue(Path(loaded["ProgramArguments"][0]).is_absolute())
        self.assertEqual(config["outbounds"][0]["bind_interface"], "en0")
        self.assertEqual(config["dns"]["servers"][0]["bind_interface"], "en0")
        self.assertEqual(setup.load_installation()[2], settings)
        self.assertFalse(any(command[0] == "/bin/launchctl" for command in self.commands))

    def test_generated_profile_passes_real_runtime_validator(self):
        base, _ = self.install()
        settings = json.loads((base / "settings.json").read_text())
        preferences = json.loads((base / "proxybridge-profile.json").read_text())
        preferences["activeProfile"] = preferences["name"]
        Runtime(settings, base=base).validate_proxybridge(preferences)

    def test_occupied_port_has_no_installation_side_effects(self):
        self.port_mock.side_effect = setup.SetupError("occupied")
        with self.assertRaisesRegex(setup.SetupError, "occupied"):
            setup.install(self.args)
        base, plist = setup.locations()
        self.assertFalse(base.exists())
        self.assertFalse(plist.exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_failed_install_rolls_back_only_new_files(self):
        def failed_check(command, **kwargs):
            if len(command) > 1 and command[1] == "check":
                (setup.locations()[0] / "unknown.txt").write_text("created concurrently")
                return subprocess.CompletedProcess(command, 1, "", "bad config")
            return self.fake_run(command, **kwargs)
        self.command_mock.side_effect = failed_check
        with self.assertRaisesRegex(setup.SetupError, "rejected"):
            setup.install(self.args)
        base, plist = setup.locations()
        self.assertEqual([path.name for path in base.iterdir()], ["unknown.txt"])
        self.assertFalse(plist.exists())

    def test_untrusted_binary_is_not_executed(self):
        self.binary.write_bytes(b"different executable")
        with self.assertRaisesRegex(setup.SetupError, "pinned official"):
            setup.install(self.args)
        self.assertEqual(self.commands, [["/sbin/ifconfig", "en0"]])
        self.assertFalse(setup.locations()[0].exists())

    def test_existing_installation_and_symlink_parent_are_rejected(self):
        base, _ = self.install()
        before = (base / "manifest.json").read_bytes()
        with self.assertRaisesRegex(setup.SetupError, "already exists"):
            setup.install(self.args)
        self.assertEqual((base / "manifest.json").read_bytes(), before)
        with tempfile.TemporaryDirectory() as raw:
            fake_home = Path(raw).resolve() / "other"
            fake_home.mkdir()
            (fake_home / "Library").symlink_to(self.home / "Library", target_is_directory=True)
            with patch.object(Path, "home", return_value=fake_home), self.assertRaisesRegex(setup.SetupError, "Symbolic"):
                setup.install(self.args)

    def test_wrong_app_identity_and_proxybridge_version_leave_no_files(self):
        for app, info in ((self.args.uu_app, {"CFBundleIdentifier": "other.app"}),
                          (self.args.proxybridge_app, {"CFBundleIdentifier": "com.interceptsuite.ProxyBridge",
                                                      "CFBundleShortVersionString": "5.0.0"})):
            path = app / "Contents/Info.plist"
            original = path.read_bytes()
            path.write_bytes(plistlib.dumps(info))
            with self.subTest(info=info), self.assertRaises(setup.SetupError):
                setup.install(self.args)
            self.assertFalse(setup.locations()[0].exists())
            self.assertFalse(setup.locations()[1].exists())
            path.write_bytes(original)

    def test_uninstall_dry_run_then_preserves_unknown_files(self):
        base, plist = self.install()
        (base / "keep.txt").write_text("user content")
        (base / "logs/channel.log").write_text("mutable runtime log")
        setup.uninstall()
        self.assertTrue(plist.exists())
        self.assertTrue((base / "bin/sing-box").exists())
        setup.uninstall(yes=True)
        self.assertFalse(plist.exists())
        self.assertEqual([path.name for path in base.iterdir()], ["keep.txt"])
        self.assertTrue(self.args.proxybridge_app.exists())
        self.assertTrue(self.args.uu_app.exists())

    def test_uninstall_rejects_modified_immutable_file(self):
        base, plist = self.install()
        (base / "config.json").write_text("{}")
        with self.assertRaisesRegex(setup.SetupError, "modified"):
            setup.uninstall(yes=True)
        self.assertTrue(plist.exists())
        self.assertTrue((base / "bin/sing-box").exists())

    def test_uninstall_rejects_symlink_including_dangling_log(self):
        base, plist = self.install()
        (base / "logs/channel.log").unlink()
        (base / "logs/channel.log").symlink_to(self.temp / "absent")
        with self.assertRaisesRegex(setup.SetupError, "Symbolic"):
            setup.uninstall(yes=True)
        self.assertTrue(plist.exists())

    def test_uninstall_does_not_remove_running_adopted_channel(self):
        base, plist = self.install()
        with patch.object(setup, "listener_owner", return_value=123), \
                patch.object(setup.time, "monotonic", side_effect=[0, 7]), self.assertRaisesRegex(setup.SetupError, "still running"):
            setup.uninstall(yes=True)
        self.assertTrue((base / "bin/sing-box").exists())
        self.assertTrue(plist.exists())

    def test_uninstall_waits_for_own_channel_without_signalling_process(self):
        base, _ = self.install()
        with patch.object(setup, "listener_owner", side_effect=[123, None]), patch.object(setup.time, "sleep") as sleep:
            setup.uninstall(yes=True)
        sleep.assert_called_once()
        self.assertFalse(base.exists())

    def test_start_bootstraps_only_own_label_and_does_not_claim_ready(self):
        _, plist = self.install()
        setup.start()
        self.runtime.validate_proxybridge_saved.assert_called_once()
        mutations = [command for command in self.commands if command[0] == "/bin/launchctl" and command[1] != "print"]
        self.assertEqual(mutations, [["/bin/launchctl", "bootstrap", f"gui/{setup.os.getuid()}", str(plist)]])
        self.assertIn("not yet verified", self.output.getvalue())

    def test_existing_owned_job_uses_kickstart_without_k_and_stop_only_own_label(self):
        self.install()
        _, plist, _, parsed = setup.load_installation()
        output = self.job_output(plist, parsed)
        def running(command, **kwargs):
            if command[:2] == ["/bin/launchctl", "print"]:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 0, output, "")
            return self.fake_run(command, **kwargs)
        self.command_mock.side_effect = running
        setup.start()
        setup.stop()
        mutations = [command for command in self.commands if command[0] == "/bin/launchctl" and command[1] != "print"]
        self.assertEqual(mutations, [["/bin/launchctl", "kickstart", setup.job_target()],
                                     ["/bin/launchctl", "bootout", setup.job_target()]])

    def test_launchd_collision_never_touched(self):
        self.install()
        _, _, _, parsed = setup.load_installation()
        bad_output = self.job_output(Path("/somebody-else/job.plist"), parsed)
        def collision(command, **kwargs):
            if command[:2] == ["/bin/launchctl", "print"]:
                self.commands.append(command)
                return subprocess.CompletedProcess(command, 0, bad_output, "")
            return self.fake_run(command, **kwargs)
        self.command_mock.side_effect = collision
        for action in (setup.start, setup.stop, lambda: setup.uninstall(yes=True)):
            with self.assertRaisesRegex(setup.SetupError, "another job"):
                action()
        self.assertFalse(any(command[0] == "/bin/launchctl" and command[1] != "print" for command in self.commands))

    def test_listener_requires_exact_binary_and_config_not_name(self):
        base, _ = self.install()
        binary = str(base / "bin/sing-box")
        replies = [subprocess.CompletedProcess([], 0, "123\n", ""),
                   subprocess.CompletedProcess([], 0, binary + "\n", ""),
                   subprocess.CompletedProcess([], 0, binary + " run -c /other/config.json\n", "")]
        self.command_mock.side_effect = replies
        with self.assertRaisesRegex(setup.SetupError, "different process"):
            setup.listener_owner(base, self.args.port)

    def make_archive(self, entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, content in entries:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    def test_download_verifies_binary_and_creates_only_two_files(self):
        binary = self.binary.read_bytes()
        data = self.make_archive([(f"sing-box-{setup.VERSION}-darwin-arm64/sing-box", binary)])
        self.asset["sha256"] = setup.sha256(data)
        output = self.temp / "download here"
        with patch.object(setup, "fetch_archive", return_value=data):
            setup.download(output)
            with self.assertRaisesRegex(setup.SetupError, "already exists"):
                setup.download(output)
        self.assertEqual({path.name for path in output.iterdir()}, {"sing-box", "receipt.json"})
        self.assertEqual((output / "sing-box").read_bytes(), binary)

    def test_archive_hash_mismatch_traversal_and_duplicate_are_rejected(self):
        member = f"sing-box-{setup.VERSION}-darwin-arm64/sing-box"
        for entries, digest in (([(member, b"binary")], "0" * 64),
                                ([("../escape", b"bad"), (member, b"binary")], None),
                                ([(member, b"one"), (member, b"two")], None)):
            data = self.make_archive(entries)
            with self.subTest(entries=entries), self.assertRaises(setup.SetupError):
                setup.archive_binary(data, "arm64", digest or setup.sha256(data))

    def test_failed_download_does_not_create_output(self):
        output = self.temp / "bad-download"
        with patch.object(setup, "fetch_archive", return_value=b"bad"), self.assertRaises(setup.SetupError):
            setup.download(output)
        self.assertFalse(output.exists())

    def test_cli_has_no_arbitrary_runtime_option(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            setup.parser().parse_args(["uninstall", "--runtime", str(self.temp), "--yes"])


if __name__ == "__main__":
    unittest.main()
