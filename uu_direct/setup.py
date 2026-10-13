"""Small, conservative installer. All operating-system changes are explicit."""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid

PROJECT = "uu-direct-macos"
LABEL = "local.uu-direct.community"
VERSION = "1.14.3"
PROXYBRIDGE_VERSION = "4.0.0"
PROFILE_NAME = "UU Direct (community)"
UU_IDS = tuple("com.netease.uuremote" + suffix for suffix in ("", ".server", ".agent", ".daemon"))
IMMUTABLE = ("runtime.py", "settings.json", "config.json", "proxybridge-profile.json", "bin/sing-box", "@launchagent")
LOG_FILES = ("logs/channel.log", "logs/channel-process.log", "logs/startup.log", "logs/startup-error.log")
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_BINARY = 200 * 1024 * 1024


class SetupError(RuntimeError):
    """An expected safety check failed."""


def locations():
    home = Path.home()
    return (home / "Library/Application Support/UU-Direct-Community",
            home / "Library/LaunchAgents" / (LABEL + ".plist"))


def require_macos():
    if platform.system() != "Darwin" or sys.version_info < (3, 10):
        raise SetupError("macOS and Python 3.10 or newer are required.")


def architecture():
    machine = platform.machine().lower()
    if machine not in ("arm64", "aarch64", "x86_64", "amd64"):
        raise SetupError("Unsupported architecture: " + machine)
    return "arm64" if machine in ("arm64", "aarch64") else "amd64"


def check_path(path):
    """Reject links at any existing component, without resolving them first."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SetupError("Symbolic links are not allowed here: " + str(part))
    return path


def regular_file(path):
    path = check_path(path)
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise SetupError("Expected a regular file: " + str(path))
    return path


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with regular_file(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command, *, timeout=15):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError("Command could not complete: " + command[0]) from error


def dependency():
    path = Path(__file__).resolve().parents[1] / "dependencies.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))["sing_box"]
    if metadata["version"] != VERSION:
        raise SetupError("Unsupported dependency version in dependencies.json.")
    arch = architecture()
    asset = metadata["assets"][arch]
    if not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
        raise SetupError("Missing or invalid official archive SHA-256.")
    expected_url = f"https://github.com/SagerNet/sing-box/releases/download/v{VERSION}/sing-box-{VERSION}-darwin-{arch}.tar.gz"
    if asset["url"] != expected_url:
        raise SetupError("Dependency URL must be the pinned official release asset.")
    return arch, asset


def fetch_archive(url):
    deadline = time.monotonic() + 120
    data = bytearray()
    request = urllib.request.Request(url, headers={"User-Agent": PROJECT})
    with urllib.request.urlopen(request, timeout=15) as response:
        if not response.geturl().startswith("https://"):
            raise SetupError("Download redirected away from HTTPS.")
        size = response.headers.get("Content-Length")
        if size is not None and int(size) > MAX_DOWNLOAD:
            raise SetupError("Download exceeds the size limit.")
        while True:
            if time.monotonic() > deadline:
                raise SetupError("Download exceeded its time limit.")
            block = response.read(1024 * 1024)
            if not block:
                break
            data.extend(block)
            if len(data) > MAX_DOWNLOAD:
                raise SetupError("Download exceeds the size limit.")
    return bytes(data)


def archive_binary(data, arch, expected_sha):
    if len(data) > MAX_DOWNLOAD or sha256(data) != expected_sha:
        raise SetupError("Archive SHA-256 verification failed.")
    wanted = f"sing-box-{VERSION}-darwin-{arch}/sing-box"
    found = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            total_size = 0
            for index, member in enumerate(archive):
                name = PurePosixPath(member.name)
                total_size += member.size
                if (index > 100 or name.is_absolute() or ".." in name.parts or member.issym() or member.islnk()
                        or member.size > MAX_BINARY or total_size > MAX_BINARY * 2):
                    raise SetupError("Unsafe archive member.")
                if member.name == wanted:
                    if not member.isfile() or member.size > MAX_BINARY:
                        raise SetupError("Invalid binary archive member.")
                    source = archive.extractfile(member)
                    if source is None:
                        raise SetupError("Missing archive binary.")
                    binary = source.read(MAX_BINARY + 1)
                    if len(binary) != member.size:
                        raise SetupError("Truncated or oversized archive binary.")
                    found.append(binary)
    except (tarfile.TarError, EOFError) as error:
        raise SetupError("Invalid release archive.") from error
    if len(found) != 1:
        raise SetupError("The release archive must contain exactly one expected binary.")
    return found[0]


class CreatedFiles:
    """Only roll back entries successfully created by this invocation."""

    def __init__(self):
        self.files = []
        self.dirs = []

    def mkdir(self, path):
        path = check_path(path)
        if path.exists():
            if not path.is_dir():
                raise SetupError("Expected a directory: " + str(path))
            return
        self.mkdir(path.parent)
        path.mkdir(mode=0o700)
        self.dirs.append(path)

    def write(self, path, data, mode=0o600):
        check_path(path)
        with path.open("xb") as target:
            info = os.fstat(target.fileno())
            self.files.append((path, info.st_dev, info.st_ino))
            target.write(data)
        path.chmod(mode)

    def new_directory(self, path):
        check_path(path)
        self.mkdir(path.parent)
        path.mkdir(mode=0o700)  # Exclusive: a concurrent installation must not be adopted.
        self.dirs.append(path)

    def rollback(self):
        for path, device, inode in reversed(self.files):
            try:
                check_path(path)
            except SetupError:
                continue
            if (not path.is_symlink() and path.is_file()
                    and (path.stat().st_dev, path.stat().st_ino) == (device, inode)):
                path.unlink()
        for path in reversed(self.dirs):
            try:
                check_path(path).rmdir()
            except (OSError, SetupError):
                pass  # A concurrently created or unknown file belongs to somebody else.


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def download(output):
    require_macos()
    output = check_path(output)
    if output.exists():
        raise SetupError("Download output already exists: " + str(output))
    arch, asset = dependency()
    binary = archive_binary(fetch_archive(asset["url"]), arch, asset["sha256"])
    if asset.get("binary_sha256") and sha256(binary) != asset["binary_sha256"]:
        raise SetupError("Official binary SHA-256 verification failed.")
    created = CreatedFiles()
    try:
        created.new_directory(output)
        created.write(output / "sing-box", binary, 0o700)
        created.write(output / "receipt.json", json_bytes({"project": PROJECT, "version": VERSION,
            "architecture": arch, "url": asset["url"], "archive_sha256": asset["sha256"], "binary_sha256": sha256(binary)}))
    except BaseException:
        created.rollback()
        raise
    print("Verified binary: " + str(output / "sing-box"))


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise SetupError(f"Port {port} is occupied or unavailable; no files were installed.") from error


def profile(settings):
    return {"name": settings["profile_name"], "trafficLoggingEnabled": False, "closeToMenuBar": False,
            "runAtStartup": False,
            "proxyConfigs": [{"id": settings["proxy_id"], "name": "UU local physical exit", "type": "socks5",
                              "host": "127.0.0.1", "port": settings["port"]}],
            "proxyRules": [{"name": "UU " + identifier.rsplit(".", 1)[-1] + " TCP and UDP",
                            "processNames": identifier, "targetHosts": "*", "targetPorts": "*",
                            "protocol": "BOTH", "action": settings["proxy_id"], "enabled": True} for identifier in UU_IDS]}


def config(settings, base):
    interface = settings["interface"]
    return {"log": {"level": "warn", "timestamp": True, "output": str(base / "logs/channel.log")},
            "dns": {"servers": [{"type": "udp", "tag": "dns-physical", "server": settings["dns_server"],
                                 "bind_interface": interface}], "strategy": "ipv4_only"},
            "inbounds": [{"type": "mixed", "tag": "uu-local", "listen": "127.0.0.1", "listen_port": settings["port"]}],
            "outbounds": [{"type": "direct", "tag": "physical-direct", "bind_interface": interface,
                           "domain_resolver": {"server": "dns-physical", "strategy": "ipv4_only"}}],
            "route": {"final": "physical-direct", "default_domain_resolver": "dns-physical"}}


def launch_plist(base):
    return {"Label": LABEL, "ProgramArguments": [str(Path(sys.executable).resolve()), str(base / "runtime.py")],
            "WorkingDirectory": str(base), "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "LimitLoadToSessionType": "Aqua", "ThrottleInterval": 30, "ExitTimeOut": 15,
            "StandardOutPath": str(base / "logs/startup.log"), "StandardErrorPath": str(base / "logs/startup-error.log")}


def manifest_path(base, plist, key):
    return plist if key == "@launchagent" else base / key


def install(args):
    require_macos()
    base, plist = map(check_path, locations())
    if base.exists() or plist.exists():
        raise SetupError("Installation directory or LaunchAgent already exists; nothing was overwritten.")
    if not re.fullmatch(r"en\d+", args.interface):
        raise SetupError("Interface must be an existing physical enN interface.")
    if run(["/sbin/ifconfig", args.interface]).returncode:
        raise SetupError("Interface does not exist: " + args.interface)
    if not 1024 <= args.port <= 65535:
        raise SetupError("Port must be between 1024 and 65535.")
    try:
        dns = str(ipaddress.IPv4Address(args.dns_server))
    except ipaddress.AddressValueError as error:
        raise SetupError("DNS server must be an IPv4 address.") from error
    apps = []
    for app, identifier in ((args.uu_app, "com.netease.uuremote"),
                            (args.proxybridge_app, "com.interceptsuite.ProxyBridge")):
        app = check_path(app)
        if app.suffix != ".app" or not (app / "Contents/Info.plist").is_file():
            raise SetupError("Application is missing: " + str(app))
        info = plistlib.loads(regular_file(app / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != identifier:
            raise SetupError("Unexpected application bundle identifier: " + str(app))
        if identifier == "com.interceptsuite.ProxyBridge" and info.get("CFBundleShortVersionString") != PROXYBRIDGE_VERSION:
            raise SetupError("This toolkit supports official ProxyBridge 4.0.0 only; another version may change system DNS behavior.")
        apps.append(str(app))
    port_available(args.port)
    binary = regular_file(args.sing_box)
    if binary.stat().st_size > MAX_BINARY:
        raise SetupError("Supplied binary exceeds the size limit.")
    _, asset = dependency()
    expected = asset.get("binary_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SetupError("Pinned binary_sha256 is missing from dependencies.json; refusing to execute the supplied binary.")
    binary_data = binary.read_bytes()
    if sha256(binary_data) != expected:
        raise SetupError("The supplied binary does not match the pinned official sing-box release.")
    settings = {"schema_version": 1, "project": PROJECT, "interface": args.interface, "port": args.port,
                "dns_server": dns, "uu_app": apps[0], "proxybridge_app": apps[1],
                "proxy_id": str(uuid.uuid4()).upper(), "profile_name": PROFILE_NAME}
    created = CreatedFiles()
    try:
        created.new_directory(base)
        for directory in (base / "bin", base / "logs", plist.parent):
            created.mkdir(directory)
        for name in LOG_FILES:
            created.write(base / name, b"")
        created.write(base / "bin/sing-box", binary_data, 0o700)
        created.write(base / "runtime.py", Path(__file__).with_name("runtime.py").read_bytes())
        for name, value in (("settings.json", settings), ("config.json", config(settings, base)),
                            ("proxybridge-profile.json", profile(settings))):
            created.write(base / name, json_bytes(value))
        checked = run([str(base / "bin/sing-box"), "check", "-c", str(base / "config.json")], timeout=30)
        if checked.returncode:
            raise SetupError("Official sing-box rejected the generated configuration: " + checked.stderr.strip()[:1000])
        created.write(plist, plistlib.dumps(launch_plist(base)))
        hashes = {key: file_hash(manifest_path(base, plist, key)) for key in IMMUTABLE}
        created.write(base / "manifest.json", json_bytes({"schema_version": 1, "project": PROJECT,
            "label": LABEL, "immutable": hashes, "mutable": list(LOG_FILES)}))
    except BaseException:
        created.rollback()
        raise
    print("Prepared (not started): " + str(base))
    print("Manually import " + str(base / "proxybridge-profile.json") + " in official ProxyBridge and activate this profile.")
    print("Approve its official system extension in macOS when prompted. Existing profiles are not changed by this CLI.")
    print(f"Set UU's own HTTP proxy to 127.0.0.1:{args.port}, then run start. Do not change the system proxy.")


def load_installation():
    base, plist = map(check_path, locations())
    manifest = json.loads(regular_file(base / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 1 or manifest.get("project") != PROJECT or manifest.get("label") != LABEL
            or set(manifest.get("immutable", {})) != set(IMMUTABLE) or manifest.get("mutable") != list(LOG_FILES)):
        raise SetupError("Unrecognized ownership manifest; refusing to operate.")
    for key, expected in manifest["immutable"].items():
        path = manifest_path(base, plist, key)
        if file_hash(path) != expected:
            raise SetupError("Installed immutable file was modified: " + str(path))
    settings = json.loads((base / "settings.json").read_text(encoding="utf-8"))
    if settings.get("project") != PROJECT or settings.get("schema_version") != 1 or settings.get("profile_name") != PROFILE_NAME:
        raise SetupError("Unrecognized settings identity.")
    if json.loads((base / "config.json").read_text()) != config(settings, base):
        raise SetupError("Unexpected network configuration.")
    if json.loads((base / "proxybridge-profile.json").read_text()) != profile(settings):
        raise SetupError("Unexpected ProxyBridge profile.")
    parsed = plistlib.loads(plist.read_bytes())
    expected_plist = launch_plist(base)
    # A different Python interpreter may invoke the CLI after installation.
    installed_python = parsed.get("ProgramArguments", [""])[0]
    expected_plist["ProgramArguments"][0] = installed_python
    if parsed != expected_plist or not Path(installed_python).is_absolute():
        raise SetupError("Unexpected LaunchAgent configuration.")
    for key in LOG_FILES:
        check_path(base / key)
    return base, plist, settings, parsed


def job_target():
    return f"gui/{os.getuid()}/{LABEL}"


def owned_job(plist, parsed):
    result = run(["/bin/launchctl", "print", job_target()])
    if result.returncode:
        # Distinguish an absent service from unexpected launchctl failures.
        if "Could not find service" in result.stderr or "Could not find service" in result.stdout:
            return None
        raise SetupError("Could not inspect this LaunchAgent: " + result.stderr.strip()[:300])
    output = result.stdout
    path_match = re.search(r"^\s*path = (.+)$", output, re.MULTILINE)
    args_match = re.search(r"^\s*arguments = \{\n(.*?)^\s*\}", output, re.MULTILINE | re.DOTALL)
    arguments = [line.strip() for line in args_match.group(1).splitlines() if line.strip()] if args_match else []
    if not path_match or path_match.group(1) != str(plist) or arguments != parsed["ProgramArguments"]:
        raise SetupError("The existing LaunchAgent label belongs to another job; refusing to touch it.")
    return output


def listener_owner(base, port):
    result = run(["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"])
    if result.returncode not in (0, 1):
        raise SetupError("Could not inspect the listening port.")
    pids = set(result.stdout.split())
    if not pids:
        port_available(port)
        return None
    if len(pids) != 1 or not next(iter(pids)).isdigit():
        raise SetupError("Port has an ambiguous owner; refusing to start another channel.")
    pid = next(iter(pids))
    binary = str(base / "bin/sing-box")
    executable = run(["/bin/ps", "-ww", "-p", pid, "-o", "comm="]).stdout.strip()
    args = run(["/bin/ps", "-ww", "-p", pid, "-o", "args="]).stdout.strip()
    if executable != binary or args != f"{binary} run -c {base / 'config.json'}":
        raise SetupError(f"Port {port} belongs to a different process (PID {pid}); it was not changed.")
    return int(pid)


def start():
    require_macos()
    base, plist, settings, parsed = load_installation()
    from .runtime import validate_proxybridge_saved
    validate_proxybridge_saved(settings)
    owner = listener_owner(base, settings["port"])
    existing = owned_job(plist, parsed)
    command = (["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)] if existing is None else
               ["/bin/launchctl", "kickstart", job_target()])
    result = run(command)
    if result.returncode:
        raise SetupError("launchctl did not accept the request: " + result.stderr.strip()[:500])
    print("Submitted this toolkit's LaunchAgent. Read status and its logs for readiness; UU connectivity is not yet verified.")
    if owner:
        print(f"Existing owned listener PID {owner} may be adopted; it was not restarted.")


def stop():
    require_macos()
    _, plist, _, parsed = load_installation()
    if owned_job(plist, parsed) is None:
        print("This LaunchAgent is not loaded. No process was stopped.")
        return
    result = run(["/bin/launchctl", "bootout", job_target()])
    if result.returncode:
        raise SetupError("Could not unload this LaunchAgent: " + result.stderr.strip()[:500])
    print("Unloaded this toolkit's LaunchAgent. An adopted, independently started channel may remain running.")


def status():
    require_macos()
    base, plist = map(check_path, locations())
    if not base.exists() and not plist.exists():
        print("Not installed.")
        return
    base, plist, settings, parsed = load_installation()
    job = owned_job(plist, parsed)
    state = re.search(r"^\s*state = (.+)$", job or "", re.MULTILINE)
    print("Runtime: " + str(base))
    print("LaunchAgent: " + (state.group(1) if state else ("loaded" if job else "not loaded")))
    owner = listener_owner(base, settings["port"])
    print(f"Listener 127.0.0.1:{settings['port']}: " + (f"owned PID {owner}" if owner else "absent"))
    print("This is a local ownership/status check, not proof that UU TCP, UDP, DNS and remote control all work.")


def wait_for_listener_exit(base, port):
    deadline = time.monotonic() + 6
    while listener_owner(base, port) is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SetupError("An owned channel is still running after unload. No files were removed; stop an independently started channel before uninstalling.")
        time.sleep(min(0.25, remaining))


def uninstall(yes=False):
    require_macos()
    base, plist, _, parsed = load_installation()
    owned_job(plist, parsed)  # Refuse to unload or erase a colliding launchd label.
    paths = [manifest_path(base, plist, key) for key in IMMUTABLE]
    paths += [base / key for key in LOG_FILES if (base / key).exists()]
    paths.append(base / "manifest.json")
    for path in paths:
        regular_file(path)
    print("Before uninstalling: activate another ProxyBridge profile and restore UU's own proxy settings manually.")
    print("ProxyBridge, its extension/profiles, UU, system proxies and routes will be kept.")
    print("Deletion plan:" if not yes else "Deleting only registered files:")
    for path in paths:
        print(str(path))
    if not yes:
        print("Dry run only. Repeat with --yes after detaching UU and the ProxyBridge profile.")
        return
    stop()
    # Recheck after bootout, before the first deletion.
    _, _, settings, _ = load_installation()
    wait_for_listener_exit(base, settings["port"])
    for path in paths:
        regular_file(path)
    for path in paths:
        path.unlink()
    for directory in (base / "logs", base / "bin", base):
        check_path(directory)
        try:
            directory.rmdir()
        except OSError:
            pass
    print("Registered files removed. Unknown files were preserved.")


def parser():
    result = argparse.ArgumentParser(description="Prepare and manage an isolated macOS UU Direct community runtime.")
    commands = result.add_subparsers(dest="command", required=True)
    download_cmd = commands.add_parser("download-sing-box", help="Download and verify the pinned official binary only")
    download_cmd.add_argument("--output", type=Path, required=True)
    install_cmd = commands.add_parser("install", help="Prepare files only; do not load the LaunchAgent")
    install_cmd.add_argument("--interface", required=True)
    install_cmd.add_argument("--port", type=int, default=18443)
    install_cmd.add_argument("--dns-server", default="223.5.5.5")
    install_cmd.add_argument("--sing-box", type=Path, required=True)
    install_cmd.add_argument("--uu-app", type=Path, default=Path("/Applications/UURemote.app"))
    install_cmd.add_argument("--proxybridge-app", type=Path, default=Path("/Applications/ProxyBridge.app"))
    for name in ("start", "stop", "status"):
        commands.add_parser(name)
    commands.add_parser("uninstall").add_argument("--yes", action="store_true", help="Delete only verified owned files")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "download-sing-box":
            download(args.output)
        elif args.command == "install":
            install(args)
        elif args.command == "uninstall":
            uninstall(args.yes)
        else:
            {"start": start, "stop": stop, "status": status}[args.command]()
        return 0
    except (SetupError, RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1
