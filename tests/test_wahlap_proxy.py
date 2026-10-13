"""Offline checks for proxy secrets, fail-closed routing and ownership-safe lifecycle."""

import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deploy import wahlap_proxy as proxy

NODE = {
    "name": "fixture-node",
    "type": "vless",
    "server": "node.example.com",
    "port": 443,
    "uuid": "d09e5e59-5dc4-44b7-b97a-f7dc57d5fd50",
    "udp": True,
    "tls": True,
    "skip-cert-verify": False,
    "flow": "xtls-rprx-vision",
    "client-fingerprint": "chrome",
    "servername": "example.com",
    "reality-opts": {"public-key": "a" * 43, "short-id": "abcd"},
}


class FakeHost:
    def __init__(self):
        self.commands = []
        self.namespaces = []
        self.links = []
        self.routes = [{"dst": "default", "dev": "eth0"}, {"dst": "172.26.176.0/20", "dev": "eth0"}]
        self.ns_routes = [{"dst": str(proxy.SUBNET), "dev": proxy.PEER_LINK}]
        self.pids = ""
        self.fail_nft = False
        self.active_workers = ""
        self.empty_netns_output = False

    def run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[:2] == ["systemctl", "list-units"]:
            return self.active_workers
        if command == ["ip", "-j", "netns", "list"]:
            if self.empty_netns_output and not self.namespaces:
                return ""
            return json.dumps(self.namespaces)
        if command == ["ip", "-j", "link", "show"]:
            return json.dumps(self.links)
        if command == ["ip", "-j", "-4", "route", "show", "table", "all"]:
            return json.dumps(self.routes)
        if command == ["ip", "netns", "pids", proxy.NETNS]:
            return self.pids
        if command[:4] == ["ip", "netns", "exec", proxy.NETNS]:
            inner = command[4:]
            if inner == ["nft", "-f", "-"] and self.fail_nft:
                raise proxy.ProxyError("Injected nft failure")
            if inner == ["nft", "list", "table", "inet", "wahlap"]:
                return proxy.firewall()
            if inner == ["ip", "-j", "route", "show"]:
                return json.dumps(self.ns_routes)
        return ""


class ProxyConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.node = copy.deepcopy(NODE)

    def test_reality_node_is_copied_without_importing_user_routing(self):
        selected = proxy.selected_node(self.node)
        self.assertEqual(selected["name"], "EGRESS")
        self.assertEqual(selected["reality-opts"], self.node["reality-opts"])
        self.assertFalse(selected["udp"])
        self.assertTrue(self.node["udp"])
        selected["reality-opts"]["short-id"] = "ef"
        self.assertEqual(self.node["reality-opts"]["short-id"], "abcd")

    def test_both_configs_have_one_mandatory_exit_and_no_public_control_port(self):
        egress, tun = proxy.configurations(self.node, "fixture-password")
        for config in (egress, tun):
            self.assertEqual(config["rules"], ["MATCH,EGRESS"])
            self.assertEqual(len(config["proxies"]), 1)
            self.assertNotIn("proxy-groups", config)
            self.assertNotIn("proxy-providers", config)
            self.assertNotIn("external-controller", config)
            self.assertNotIn("DIRECT", json.dumps(config))
            self.assertEqual(config["log-level"], "silent")
        self.assertEqual(egress["bind-address"], proxy.HOST_IP)
        self.assertEqual(egress["lan-allowed-ips"], [proxy.PEER_IP + "/32"])
        self.assertEqual(tun["proxies"][0]["server"], proxy.HOST_IP)
        self.assertFalse(tun["tun"]["auto-route"])
        self.assertFalse(tun["tun"]["auto-detect-interface"])
        self.assertEqual(tun["dns"]["listen"], "127.0.0.1:53")
        self.assertTrue(all(item.endswith("#EGRESS") for item in tun["dns"]["nameserver"]))
        self.assertNotIn(self.node["uuid"], json.dumps(tun))

    def test_imported_provider_hooks_files_and_direct_routes_are_rejected(self):
        for name, value in {
            "dialer-proxy": "DIRECT",
            "interface-name": "eth0",
            "routing-mark": 1,
            "plugin": "obfs",
            "private-key": "/etc/passwd",
            "certificate": "/etc/passwd",
            "rules": ["MATCH,DIRECT"],
            "proxy-providers": {},
            "script": "secret payload",
        }.items():
            with self.subTest(name=name):
                with self.assertRaises(proxy.ProxyError) as result:
                    proxy.selected_node({**self.node, name: value})
                self.assertNotIn("secret payload", str(result.exception))

    def test_bad_field_types_and_tls_weakening_are_rejected_safely(self):
        for name, value in {
            "type": [],
            "port": True,
            "uuid": {},
            "server": "http://secret@example.com",
            "tls": False,
            "skip-cert-verify": True,
            "client-fingerprint": [],
            "flow": {},
            "network": [],
            "udp": "true",
            "reality-opts": {"public-key": 4, "short-id": 12},
        }.items():
            with self.subTest(name=name), self.assertRaises(proxy.ProxyError):
                proxy.selected_node({**self.node, name: value})

    def test_nonpublic_literal_proxy_addresses_are_rejected(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            with self.subTest(address=address), self.assertRaises(proxy.ProxyError):
                proxy.selected_node({**self.node, "server": address})

    def test_supported_encrypted_nodes_and_websocket_host(self):
        base = {"server": "node.example.com", "port": 443}
        variants = [
            {**base, "type": "ss", "password": "fixture", "cipher": "aes-128-gcm"},
            {**base, "type": "trojan", "password": "fixture", "sni": "example.com"},
            {
                **base,
                "type": "vmess",
                "uuid": NODE["uuid"],
                "alterId": 0,
                "network": "ws",
                "ws-opts": {"path": "/fixture", "headers": {"Host": "example.com"}},
            },
        ]
        for value in variants:
            with self.subTest(kind=value["type"]):
                self.assertEqual(proxy.selected_node(value)["type"], value["type"])
        variants[-1]["ws-opts"]["headers"]["Authorization"] = "secret"
        with self.assertRaises(proxy.ProxyError):
            proxy.selected_node(variants[-1])


class ProxyNetworkTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="mai-proxy-test-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.marker = self.folder / "marker.json"
        self.host = FakeHost()
        self.network = proxy.Network(self.host, self.marker)

    def owned(self):
        proxy.private_json(
            self.marker,
            {"namespace": proxy.NETNS, "host_link": proxy.HOST_LINK, "subnet": str(proxy.SUBNET)},
        )
        self.host.namespaces = [{"name": proxy.NETNS}]
        self.host.links = [{"ifname": proxy.HOST_LINK}]

    def test_setup_never_changes_host_default_forwarding_or_firewall(self):
        self.network.up()
        self.assertTrue(self.network.owned())
        commands = [command for command, _ in self.host.commands]
        host_commands = [command for command in commands if command[:3] != ["ip", "netns", "exec"]]
        self.assertFalse(
            any(command[0] in {"nft", "sysctl", "iptables"} for command in host_commands)
        )
        self.assertFalse(any("default" in command for command in host_commands))
        nft = commands.index(["ip", "netns", "exec", proxy.NETNS, "nft", "-f", "-"])
        up = commands.index(
            ["ip", "netns", "exec", proxy.NETNS, "ip", "link", "set", proxy.PEER_LINK, "up"]
        )
        self.assertLess(nft, up)
        self.assertEqual(proxy.firewall().count("policy drop"), 3)
        self.assertNotIn("udp dport", proxy.firewall())
        self.assertIn(f"ip daddr {proxy.HOST_IP} tcp dport {proxy.SOCKS_PORT}", proxy.firewall())

    def test_existing_owned_setup_is_checked_without_recreating(self):
        self.owned()
        self.network.up()
        commands = [command for command, _ in self.host.commands]
        self.assertFalse(any("add" in command or "delete" in command for command in commands))

    def test_refuses_foreign_resources_and_route_collision_before_mutation(self):
        cases = ("namespace", "link", "subnet")
        for case in cases:
            host = FakeHost()
            if case == "namespace":
                host.namespaces = [{"name": proxy.NETNS}]
            elif case == "link":
                host.links = [{"ifname": proxy.HOST_LINK}]
            else:
                host.routes.append({"dst": "10.0.0.0/8", "dev": "foreign"})
            with self.subTest(case=case), self.assertRaises(proxy.ProxyError):
                proxy.Network(host, self.marker).up()
            self.assertFalse(
                any("add" in command or "delete" in command for command, _ in host.commands)
            )

    def test_failed_firewall_setup_cleans_only_new_resources(self):
        self.host.fail_nft = True
        with self.assertRaises(proxy.ProxyError):
            self.network.up()
        commands = [command for command, _ in self.host.commands]
        self.assertIn(["ip", "link", "delete", proxy.HOST_LINK], commands)
        self.assertIn(["ip", "netns", "delete", proxy.NETNS], commands)
        self.assertFalse(self.marker.exists())

    def test_down_refuses_to_destroy_active_worker_namespace(self):
        self.owned()
        self.host.pids = "123\n"
        with self.assertRaisesRegex(proxy.ProxyError, "running processes"):
            self.network.down()
        self.assertTrue(self.marker.exists())
        self.assertFalse(any("delete" in command for command, _ in self.host.commands))

    def test_down_is_idempotent_when_absent_and_removes_owned_namespace(self):
        self.network.down()
        self.owned()
        self.network.down()
        self.assertFalse(self.marker.exists())
        self.assertIn((["ip", "netns", "delete", proxy.NETNS], {}), self.host.commands)

    def test_empty_iproute_netns_output_is_an_empty_inventory(self):
        self.host.empty_netns_output = True
        self.network.down()
        self.assertFalse(any("delete" in command for command, _ in self.host.commands))
        self.network.up()
        self.assertTrue(self.network.owned())
        self.assertIn((["ip", "netns", "add", proxy.NETNS], {}), self.host.commands)

    def test_check_rejects_default_route_outside_tun(self):
        self.owned()
        self.host.ns_routes.append({"dst": "default", "dev": proxy.PEER_LINK})
        with self.assertRaisesRegex(proxy.ProxyError, "unexpected default route"):
            self.network.check()

    def test_route_ready_refuses_host_network_and_uses_only_tun(self):
        with patch.object(
            proxy.os, "stat", side_effect=[SimpleNamespace(st_ino=1), SimpleNamespace(st_ino=2)]
        ):
            with self.assertRaisesRegex(proxy.ProxyError, "Wahlap namespace"):
                proxy.route_ready(self.host)
        self.assertEqual(self.host.commands, [])
        self.host.links = [{"ifname": proxy.TUN_LINK}]
        with patch.object(proxy.os, "stat", return_value=SimpleNamespace(st_ino=7)):
            proxy.route_ready(self.host)
        self.assertEqual(
            self.host.commands[-1][0], ["ip", "route", "replace", "default", "dev", proxy.TUN_LINK]
        )

    def test_private_json_rejects_symlink_and_does_not_leave_temporary_secrets(self):
        path = self.folder / "config.json"
        proxy.private_json(path, {"password": "fixture-secret"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.folder.iterdir()), [path])
        alias = self.folder / "alias"
        alias.symlink_to(path)
        with self.assertRaises(proxy.ProxyError):
            proxy.private_json(alias, {})
        self.assertEqual(json.loads(path.read_text()), {"password": "fixture-secret"})

    def test_untrusted_binary_never_replaces_existing_executable(self):
        archive = self.folder / "mihomo.gz"
        target = self.folder / "mihomo"
        target.write_bytes(b"previous")
        archive.write_bytes(gzip.compress(b"\x7fELF\x02fixture"))
        with self.assertRaisesRegex(proxy.ProxyError, "checksum"):
            proxy.install_binary(archive, target)
        self.assertEqual(target.read_bytes(), b"previous")
        with patch.object(
            proxy, "ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()
        ):
            proxy.install_binary(archive, target)
        self.assertEqual(target.read_bytes(), b"\x7fELF\x02fixture")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_installer_rejects_fifo_before_reading_credentials(self):
        path = self.folder / "fifo"
        os.mkfifo(path, 0o600)
        with (
            patch.object(proxy.os, "geteuid", return_value=0),
            patch.object(proxy.os, "uname", return_value=SimpleNamespace(machine="x86_64")),
            patch.object(proxy.sys, "platform", "linux"),
        ):
            with self.assertRaisesRegex(proxy.ProxyError, "private regular file"):
                proxy.install(path, host=self.host)
        self.assertEqual(self.host.commands, [])

    def test_installer_refuses_live_workers_before_changing_any_services_or_secrets(self):
        node_file = self.folder / "node.json"
        proxy.private_json(node_file, NODE)
        self.host.active_workers = "mai-journal-wahlap@123.service loaded active running"
        with (
            patch.object(proxy.os, "geteuid", return_value=0),
            patch.object(proxy.os, "uname", return_value=SimpleNamespace(machine="x86_64")),
            patch.object(proxy.sys, "platform", "linux"),
            patch.object(proxy.shutil, "which", return_value="/usr/bin/fixture"),
        ):
            with self.assertRaisesRegex(proxy.ProxyError, "active Wahlap workers"):
                proxy.install(node_file, host=self.host)
        self.assertEqual(len(self.host.commands), 1)
        self.assertEqual(self.host.commands[0][0][:2], ["systemctl", "list-units"])

    def test_cli_and_host_do_not_echo_raw_subprocess_or_config_errors(self):
        output = io.StringIO()
        with (
            patch.object(proxy.sys, "argv", ["proxy", "install", "--node-file", "fixture"]),
            patch.object(proxy, "install", side_effect=ValueError("super-private-credential")),
            contextlib.redirect_stderr(output),
        ):
            self.assertEqual(proxy.main(), 1)
        self.assertNotIn("super-private-credential", output.getvalue())
        result = subprocess.CompletedProcess(["tool"], 1, "super-private-credential", "private")
        with patch.object(proxy.subprocess, "run", return_value=result):
            with self.assertRaises(proxy.ProxyError) as error:
                proxy.Host().run(["tool"])
        self.assertNotIn("private", str(error.exception))

    def test_units_keep_proxy_namespace_and_dns_away_from_main_web_service(self):
        deploy = Path(proxy.__file__).parent
        dropin = (deploy / "mai-journal-wahlap-proxy.conf").read_text()
        tun = (deploy / "mai-journal-wahlap-tun.service").read_text()
        web = (deploy / "mai-journal.service").read_text()
        self.assertIn(f"NetworkNamespacePath=/run/netns/{proxy.NETNS}", dropin)
        self.assertIn(f"/etc/netns/{proxy.NETNS}/resolv.conf:/etc/resolv.conf", dropin)
        self.assertIn("DeviceAllow=/dev/net/tun rw", tun)
        self.assertNotIn("NetworkNamespacePath", web)


if __name__ == "__main__":
    unittest.main()
