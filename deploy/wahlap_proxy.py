#!/usr/bin/env python3
"""Install a private Wahlap proxy and manage only its isolated network namespace."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

VERSION = "v1.19.32"
ARCHIVE_SHA256 = "ba3ce607747a07f948fc35780e108a4a7c7f552a38b9bd4d115f313ebcb89c20"
DOWNLOAD_URL = (
    f"https://github.com/MetaCubeX/mihomo/releases/download/{VERSION}/"
    f"mihomo-linux-amd64-compatible-{VERSION}.gz"
)
ROOT = Path("/opt/mai-journal-wahlap-proxy")
CONFIG = Path("/etc/mai-journal-wahlap-proxy")
NETNS_DIRECTORY = Path("/run/netns")
MARKER = Path("/run/mai-journal-wahlap-network.json")
NETNS = "mai-wahlap"
HOST_LINK = "mw-host"
PEER_LINK = "mw-peer"
TUN_LINK = "mw-tun"
SUBNET = ipaddress.ip_network("10.203.240.0/30")
HOST_IP = "10.203.240.1"
PEER_IP = "10.203.240.2"
SOCKS_PORT = 17891
EGRESS_USER = "mai-wahlap-proxy"
TUN_USER = "mai-wahlap-tun"
MAX_ARCHIVE = 40 * 1024 * 1024
MAX_BINARY = 100 * 1024 * 1024


class ProxyError(RuntimeError):
    """A safe operational error with no credentials or subprocess output."""


class Host:
    def run(self, arguments, *, data=None, timeout=30, allowed=(0,)):
        try:
            result = subprocess.run(
                arguments, input=data, text=True, capture_output=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProxyError(f"{Path(arguments[0]).name} could not complete") from None
        if result.returncode not in allowed:
            raise ProxyError(f"{Path(arguments[0]).name} failed (exit {result.returncode})")
        return result.stdout


def text_field(value, name, *, maximum=2048):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProxyError(f"Invalid node field: {name}")
    if any(ord(character) < 32 for character in value):
        raise ProxyError(f"Invalid node field: {name}")
    return value


def hostname(value, name):
    value = text_field(value, name, maximum=253)
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
            raise ProxyError(f"Invalid node field: {name}") from None
    else:
        if not address.is_global:
            raise ProxyError(f"Node {name} must use a public address")
    return value


def selected_node(value):
    """Accept one node, never an imported ruleset, provider, script or local-file option."""
    if not isinstance(value, dict):
        raise ProxyError("Node file must contain one JSON object")
    kind = value.get("type")
    type_fields = {
        "ss": {"cipher", "password"},
        "trojan": {"password", "sni", "alpn", "client-fingerprint"},
        "vmess": {"uuid", "alterId", "cipher", "tls", "servername", "client-fingerprint"},
        "vless": {"uuid", "tls", "servername", "flow", "client-fingerprint", "reality-opts"},
    }
    if not isinstance(kind, str) or kind not in type_fields:
        raise ProxyError("Supported node types: ss, trojan, vmess, vless")
    allowed = {"name", "type", "server", "port", "udp", "skip-cert-verify"} | type_fields[kind]
    if kind != "ss":
        allowed |= {"network", "ws-opts", "grpc-opts"}
    if set(value) - allowed:
        raise ProxyError("Node contains unsupported options; export only the selected node")
    if value.get("skip-cert-verify", False) is not False:
        raise ProxyError("Proxy TLS certificate verification must remain enabled")
    if "udp" in value and not isinstance(value["udp"], bool):
        raise ProxyError("Invalid node field: udp")
    if type(value.get("port")) is not int or not 1 <= value["port"] <= 65535:
        raise ProxyError("Invalid node field: port")
    node = {"name": "EGRESS", "type": kind, "server": hostname(value.get("server"), "server")}
    node.update(port=value["port"], udp=False)
    if kind in {"ss", "trojan"}:
        node["password"] = text_field(value.get("password"), "password")
    if kind == "ss":
        cipher = value.get("cipher")
        if not isinstance(cipher, str) or cipher not in {
            "aes-128-gcm",
            "aes-256-gcm",
            "chacha20-ietf-poly1305",
            "2022-blake3-aes-128-gcm",
            "2022-blake3-aes-256-gcm",
            "2022-blake3-chacha20-poly1305",
        }:
            raise ProxyError("Unsupported Shadowsocks cipher")
        node["cipher"] = cipher
    if kind in {"vmess", "vless"}:
        try:
            node["uuid"] = str(uuid.UUID(value["uuid"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ProxyError("Invalid node field: uuid") from None
        if "tls" in value and not isinstance(value["tls"], bool):
            raise ProxyError("Invalid node field: tls")
        node["tls"] = value.get("tls", True)
        if kind == "vless" and not node["tls"]:
            raise ProxyError("VLESS requires TLS")
    if kind == "vmess":
        if type(value.get("alterId", 0)) is not int or value.get("alterId", 0) != 0:
            raise ProxyError("Only VMess AEAD (alterId 0) is supported")
        node.update(alterId=0, cipher=value.get("cipher", "auto"))
        if not isinstance(node["cipher"], str) or node["cipher"] not in {
            "auto",
            "aes-128-gcm",
            "chacha20-poly1305",
        }:
            raise ProxyError("Unsupported VMess cipher")
    for key in ("sni", "servername"):
        if key in value:
            node[key] = hostname(value[key], key)
    if "client-fingerprint" in value:
        fingerprint = value["client-fingerprint"]
        if not isinstance(fingerprint, str) or fingerprint not in {
            "chrome",
            "firefox",
            "safari",
            "ios",
            "android",
            "edge",
            "random",
        }:
            raise ProxyError("Unsupported TLS client fingerprint")
        node["client-fingerprint"] = fingerprint
    if "alpn" in value:
        if (
            not isinstance(value["alpn"], list)
            or not value["alpn"]
            or any(
                not isinstance(item, str) or item not in {"h2", "http/1.1"}
                for item in value["alpn"]
            )
        ):
            raise ProxyError("Invalid node field: alpn")
        node["alpn"] = value["alpn"]
    if "flow" in value:
        if not isinstance(value["flow"], str) or value["flow"] not in {"", "xtls-rprx-vision"}:
            raise ProxyError("Unsupported VLESS flow")
        node["flow"] = value["flow"]
    if "reality-opts" in value:
        reality = value["reality-opts"]
        if not isinstance(reality, dict) or set(reality) != {"public-key", "short-id"}:
            raise ProxyError("Invalid Reality options")
        if (
            not isinstance(reality["public-key"], str)
            or not isinstance(reality["short-id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", reality["public-key"])
            or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", reality["short-id"])
        ):
            raise ProxyError("Invalid Reality key or short ID")
        if not node.get("tls"):
            raise ProxyError("Reality requires TLS")
        node["reality-opts"] = reality.copy()
    network = value.get("network", "tcp")
    if not isinstance(network, str) or network not in {"tcp", "ws", "grpc"}:
        raise ProxyError("Supported transports: tcp, ws, grpc")
    if network != "tcp":
        node["network"] = network
    if "ws-opts" in value:
        options = value["ws-opts"]
        if network != "ws" or not isinstance(options, dict) or set(options) - {"path", "headers"}:
            raise ProxyError("Invalid WebSocket options")
        path = text_field(options.get("path", "/"), "ws path")
        if not path.startswith("/"):
            raise ProxyError("WebSocket path must start with a slash")
        headers = options.get("headers", {})
        if not isinstance(headers, dict) or set(headers) - {"Host"}:
            raise ProxyError("Only the WebSocket Host header is supported")
        node["ws-opts"] = {"path": path}
        if headers:
            node["ws-opts"]["headers"] = {"Host": hostname(headers["Host"], "ws host")}
    if "grpc-opts" in value:
        options = value["grpc-opts"]
        if (
            network != "grpc"
            or not isinstance(options, dict)
            or set(options) != {"grpc-service-name"}
        ):
            raise ProxyError("Invalid gRPC options")
        node["grpc-opts"] = {
            "grpc-service-name": text_field(options["grpc-service-name"], "grpc service")
        }
    return node


def configurations(value, password):
    node = selected_node(value)
    text_field(password, "local proxy password")
    common = {
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "find-process-mode": "off",
        "profile": {"store-selected": False, "store-fake-ip": False},
        "rules": ["MATCH,EGRESS"],
    }
    egress = {
        **common,
        "socks-port": SOCKS_PORT,
        "allow-lan": True,
        "bind-address": HOST_IP,
        "lan-allowed-ips": [f"{PEER_IP}/32"],
        "authentication": [f"wahlap:{password}"],
        "proxies": [node],
    }
    tun = {
        **common,
        "proxies": [
            {
                "name": "EGRESS",
                "type": "socks5",
                "server": HOST_IP,
                "port": SOCKS_PORT,
                "username": "wahlap",
                "password": password,
                "udp": False,
            }
        ],
        "tun": {
            "enable": True,
            "device": TUN_LINK,
            "stack": "gvisor",
            "mtu": 1500,
            "auto-route": False,
            "auto-redirect": False,
            "auto-detect-interface": False,
            "dns-hijack": ["any:53", "tcp://any:53"],
        },
        "dns": {
            "enable": True,
            "listen": "127.0.0.1:53",
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": ["https://1.1.1.1/dns-query#EGRESS"],
            "use-system-hosts": False,
        },
    }
    return egress, tun


def private_json(path, value, *, gid=None):
    path = Path(path)
    if path.is_symlink():
        raise ProxyError("Configuration files must not be symbolic links")
    fd, temporary = tempfile.mkstemp(prefix=".proxy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600 if gid is None else 0o640)
            if gid is not None:
                os.fchown(stream.fileno(), 0, gid)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_binary(archive, target):
    with Path(archive).open("rb") as stream:
        compressed = stream.read(MAX_ARCHIVE + 1)
    if len(compressed) > MAX_ARCHIVE or hashlib.sha256(compressed).hexdigest() != ARCHIVE_SHA256:
        raise ProxyError("Mihomo archive checksum does not match the pinned official release")
    try:
        import io

        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            binary = stream.read(MAX_BINARY + 1)
    except (OSError, EOFError):
        raise ProxyError("Invalid Mihomo archive") from None
    if len(binary) > MAX_BINARY or binary[:5] != b"\x7fELF\x02":
        raise ProxyError("Mihomo archive did not contain a bounded Linux 64-bit executable")
    target = Path(target)
    if target.is_symlink():
        raise ProxyError("Mihomo executable must not be a symbolic link")
    fd, temporary = tempfile.mkstemp(prefix=".mihomo-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(binary)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o755)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def firewall():
    return f"""table inet wahlap {{
 chain output {{ type filter hook output priority 0; policy drop;
  oifname \"lo\" accept
  oifname \"{TUN_LINK}\" accept
  oifname \"{PEER_LINK}\" ip daddr {HOST_IP} tcp dport {SOCKS_PORT} accept
 }}
 chain input {{ type filter hook input priority 0; policy drop;
  iifname \"lo\" accept
  iifname \"{TUN_LINK}\" accept
  iifname \"{PEER_LINK}\" ip saddr {HOST_IP} tcp sport {SOCKS_PORT} ct state established accept
 }}
 chain forward {{ type filter hook forward priority 0; policy drop; }}
}}
"""


class Network:
    def __init__(self, host=None, marker=MARKER, namespace_directory=NETNS_DIRECTORY):
        self.host = host or Host()
        self.marker = Path(marker)
        self.namespace_directory = Path(namespace_directory)

    def prepare_namespace_directory(self):
        try:
            self.namespace_directory.mkdir(mode=0o755)
        except FileExistsError:
            if (
                self.namespace_directory.is_symlink()
                or not self.namespace_directory.is_dir()
                or not self.namespace_directory.stat().st_mode & 0o001
            ):
                raise ProxyError(
                    "Network namespace directory must already allow traversal"
                ) from None
        else:
            # The root setup unit uses UMask=0077; only adjust the directory we just created.
            self.namespace_directory.chmod(0o755)

    def ns(self, *arguments, **kwargs):
        return self.host.run(["ip", "netns", "exec", NETNS, *arguments], **kwargs)

    def owned(self):
        try:
            return json.loads(self.marker.read_text()) == {
                "namespace": NETNS,
                "host_link": HOST_LINK,
                "subnet": str(SUBNET),
            }
        except (OSError, ValueError):
            return False

    def up(self):
        namespaces = json.loads(self.host.run(["ip", "-j", "netns", "list"]).strip() or "[]")
        links = json.loads(self.host.run(["ip", "-j", "link", "show"]))
        ns_exists = any(item.get("name") == NETNS for item in namespaces)
        link_exists = any(item.get("ifname") == HOST_LINK for item in links)
        if ns_exists or link_exists or self.marker.exists():
            if not (self.owned() and ns_exists and link_exists):
                raise ProxyError(
                    "Reserved proxy network resources already exist; inspect before changing"
                )
            self.check()
            return
        routes = json.loads(self.host.run(["ip", "-j", "-4", "route", "show", "table", "all"]))
        for route in routes:
            destination = route.get("dst", "default")
            if destination == "default":
                continue
            if ipaddress.ip_network(destination, strict=False).overlaps(SUBNET):
                raise ProxyError("Reserved proxy subnet overlaps an existing host route")
        created = False
        try:
            self.prepare_namespace_directory()
            self.host.run(["ip", "netns", "add", NETNS])
            created = True
            self.host.run(
                ["ip", "link", "add", HOST_LINK, "type", "veth", "peer", "name", PEER_LINK]
            )
            self.host.run(["ip", "link", "set", PEER_LINK, "netns", NETNS])
            self.host.run(["ip", "addr", "add", f"{HOST_IP}/30", "dev", HOST_LINK])
            self.host.run(["ip", "link", "set", HOST_LINK, "up"])
            self.ns("ip", "addr", "add", f"{PEER_IP}/30", "dev", PEER_LINK)
            self.ns("sysctl", "-q", "-w", "net.ipv6.conf.all.disable_ipv6=1")
            self.ns("sysctl", "-q", "-w", "net.ipv6.conf.default.disable_ipv6=1")
            self.ns("nft", "-f", "-", data=firewall())
            self.ns("ip", "link", "set", "lo", "up")
            self.ns("ip", "link", "set", PEER_LINK, "up")
            private_json(
                self.marker, {"namespace": NETNS, "host_link": HOST_LINK, "subnet": str(SUBNET)}
            )
        except (ProxyError, OSError):
            if created:
                self.host.run(["ip", "link", "delete", HOST_LINK], allowed=(0, 1))
                self.host.run(["ip", "netns", "delete", NETNS], allowed=(0, 1))
            self.marker.unlink(missing_ok=True)
            raise

    def check(self):
        if not self.owned():
            raise ProxyError("Proxy namespace has no ownership marker")
        table = self.ns("nft", "list", "table", "inet", "wahlap")
        if "policy drop" not in table or f"tcp dport {SOCKS_PORT}" not in table:
            raise ProxyError("Proxy namespace firewall is missing")
        for route in json.loads(self.ns("ip", "-j", "route", "show")):
            if route.get("dst") == "default" and route.get("dev") != TUN_LINK:
                raise ProxyError("Proxy namespace contains an unexpected default route")

    def down(self):
        namespaces = json.loads(self.host.run(["ip", "-j", "netns", "list"]).strip() or "[]")
        if not any(item.get("name") == NETNS for item in namespaces):
            if self.marker.exists():
                raise ProxyError("Proxy namespace disappeared; inspect its ownership marker")
            return
        if not self.owned():
            raise ProxyError("Refusing to remove a network namespace without its ownership marker")
        if self.host.run(["ip", "netns", "pids", NETNS]).strip():
            raise ProxyError(
                "Proxy namespace still has running processes; stop the worker and TUN first"
            )
        self.host.run(["ip", "link", "delete", HOST_LINK])
        self.host.run(["ip", "netns", "delete", NETNS])
        self.marker.unlink()


def route_ready(host=None, *, timeout=20):
    """Called inside the namespace by the TUN service, never from the host network."""
    host = host or Host()
    if os.stat("/proc/self/ns/net").st_ino != os.stat(NETNS_DIRECTORY / NETNS).st_ino:
        raise ProxyError("TUN route setup must run in the Wahlap namespace")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        links = json.loads(host.run(["ip", "-j", "link", "show"]))
        if any(item.get("ifname") == TUN_LINK for item in links):
            host.run(["ip", "route", "replace", "default", "dev", TUN_LINK])
            return
        time.sleep(0.2)
    raise ProxyError("TUN interface did not become ready")


def install(node_file, archive=None, host=None):
    if os.geteuid() != 0:
        raise ProxyError("Installation requires root")
    host = host or Host()
    if os.uname().machine != "x86_64" or sys.platform != "linux":
        raise ProxyError("This pinned proxy runtime requires Linux x86_64")
    node_file = Path(node_file)
    if (
        node_file.is_symlink()
        or not stat.S_ISREG(node_file.stat().st_mode)
        or node_file.stat().st_mode & 0o077
        or node_file.stat().st_size > 65536
    ):
        raise ProxyError("Selected node JSON must be a private regular file under 64 KiB")
    value = json.loads(node_file.read_text())
    egress, tun = configurations(value, secrets.token_urlsafe(32))
    for tool in ("ip", "nft", "systemctl"):
        if shutil.which(tool) is None:
            raise ProxyError(f"Required executable is missing: {tool}")
    workers = host.run(
        [
            "systemctl",
            "list-units",
            "--no-legend",
            "--plain",
            "--state=active,activating,deactivating",
            "mai-journal-wahlap@*.service",
        ]
    )
    if workers.strip():
        raise ProxyError("Wait for active Wahlap workers to finish before changing the proxy")
    for name in ("netns", "egress", "tun"):
        state = host.run(
            ["systemctl", "is-active", f"mai-journal-wahlap-{name}.service"], allowed=(0, 3, 4)
        ).strip()
        if state in {"active", "activating", "deactivating"}:
            raise ProxyError(
                "Stop the Wahlap proxy services before replacing runtime or credentials"
            )
    for user in (EGRESS_USER, TUN_USER):
        try:
            pwd.getpwnam(user)
        except KeyError:
            host.run(
                [
                    "useradd",
                    "--system",
                    "--user-group",
                    "--no-create-home",
                    "--shell",
                    "/usr/sbin/nologin",
                    user,
                ]
            )
    for path in (ROOT, CONFIG):
        if path.is_symlink():
            raise ProxyError("Proxy installation directories must not be symbolic links")
        path.mkdir(parents=True, exist_ok=True, mode=0o755)
        path.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix="mai-proxy-install-") as temporary:
        if archive is None:
            archive = Path(temporary) / "mihomo.gz"
            with urllib.request.urlopen(DOWNLOAD_URL, timeout=45) as response:
                payload = response.read(MAX_ARCHIVE + 1)
            archive.write_bytes(payload)
        install_binary(archive, ROOT / "mihomo")
        for name, user, config in (("egress", EGRESS_USER, egress), ("tun", TUN_USER, tun)):
            candidate = Path(temporary) / f"{name}.json"
            private_json(candidate, config)
            host.run([str(ROOT / "mihomo"), "-t", "-d", temporary, "-f", str(candidate)])
            private_json(CONFIG / f"{name}.json", config, gid=pwd.getpwnam(user).pw_gid)
    resolver = Path(f"/etc/netns/{NETNS}")
    resolver.mkdir(parents=True, exist_ok=True)
    (resolver / "resolv.conf").write_text("nameserver 127.0.0.1\noptions timeout:2 attempts:2\n")
    (resolver / "resolv.conf").chmod(0o644)
    source = Path(__file__).resolve()
    if source != ROOT / source.name:
        shutil.copyfile(source, ROOT / source.name)
    (ROOT / source.name).chmod(0o755)
    for name in ("netns", "egress", "tun"):
        filename = f"mai-journal-wahlap-{name}.service"
        shutil.copyfile(source.parent / filename, Path("/etc/systemd/system") / filename)
    dropin = Path("/etc/systemd/system/mai-journal-wahlap@.service.d")
    dropin.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source.parent / "mai-journal-wahlap-proxy.conf", dropin / "proxy.conf")
    host.run(["systemctl", "daemon-reload"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("install")
    setup.add_argument("--node-file", type=Path, required=True)
    setup.add_argument("--binary-archive", type=Path)
    for command in ("network-up", "network-down", "network-check", "route-ready"):
        sub.add_parser(command)
    args = parser.parse_args()
    try:
        if args.command == "install":
            install(args.node_file, args.binary_archive)
        elif args.command == "route-ready":
            route_ready()
        else:
            if os.geteuid() != 0:
                raise ProxyError("Network lifecycle requires root")
            getattr(
                Network(),
                {"network-up": "up", "network-down": "down", "network-check": "check"}[
                    args.command
                ],
            )()
    except ProxyError as exc:
        print(f"Wahlap proxy operation failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError):
        # Configuration or transport exceptions can contain node credentials. Never echo them.
        print(
            "Wahlap proxy operation failed; configuration or network preconditions were not met.",
            file=sys.stderr,
        )
        return 1
    print("Wahlap proxy operation completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
