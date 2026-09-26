import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import he_tunnel


HE_VALUES = {
    "server_ipv4": "1.1.1.1",
    "client_ipv4": "8.8.8.8",
    "server_ipv6": "2001:470:ffff:81c::1/64",
    "client_ipv6": "2001:470:ffff:81c::2/64",
    "routed_prefix": "2001:470:fffe:81c::/64",
    "default_route": "auto",
}


def result(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


class TunnelTests(unittest.TestCase):
    def test_valid_he_config_derives_single_routed_address(self):
        config = he_tunnel.parse_config(HE_VALUES)
        self.assertEqual(config["routed_address"], "2001:470:fffe:81c::1")
        self.assertEqual(config["client_ipv6"], "2001:470:ffff:81c::2/64")
        self.assertEqual(config["local_ipv4"], "8.8.8.8")

    def test_tunnel_endpoint_without_routed_prefix(self):
        config = he_tunnel.parse_config({key: value for key, value in HE_VALUES.items()
                                         if key != "routed_prefix"})
        self.assertIsNone(config["routed_prefix"])
        self.assertIsNone(config["routed_address"])

    def test_routed_56_derives_single_host_address(self):
        config = he_tunnel.parse_config({**HE_VALUES,
                                         "routed_prefix": "2001:470:fffe:8100::/56"})
        self.assertEqual(config["routed_address"], "2001:470:fffe:8100::1")

    def test_nat_mapping_uses_private_local_ipv4(self):
        config = he_tunnel.parse_config({**HE_VALUES, "client_ipv4": "9.9.9.9",
                                         "local_ipv4": "172.16.10.20"})
        self.assertEqual(config["client_ipv4"], "9.9.9.9")
        self.assertEqual(config["local_ipv4"], "172.16.10.20")

    def test_rejects_mismatched_endpoint_prefix(self):
        values = {**HE_VALUES, "client_ipv6": "2001:470:ffff:81d::2/64"}
        with self.assertRaisesRegex(he_tunnel.TunnelError, "same /64"):
            he_tunnel.parse_config(values)

    def test_rejects_nonpublic_client_ipv4(self):
        values = {**HE_VALUES, "client_ipv4": "192.168.1.2"}
        with self.assertRaisesRegex(he_tunnel.TunnelError, "public"):
            he_tunnel.parse_config(values)

    @patch.object(he_tunnel, "run", return_value=result(
        '[{"addr_info":[{"family":"inet","local":"8.8.8.8"}]}]'
    ))
    def test_finds_local_ipv4_in_ip_json(self, _run_mock):
        self.assertTrue(he_tunnel.local_ipv4_present("8.8.8.8"))
        self.assertFalse(he_tunnel.local_ipv4_present("8.8.4.4"))

    @patch.object(he_tunnel, "local_ipv4_present", return_value=True)
    @patch.object(he_tunnel, "run", return_value=result(
        "1.1.1.1 via 8.8.8.1 dev eth0 src 8.8.4.4"
    ))
    def test_rejects_wrong_ipv4_route_source(self, _run_mock, _present_mock):
        with self.assertRaisesRegex(he_tunnel.TunnelError, "route"):
            he_tunnel.check_local_ipv4(he_tunnel.parse_config(HE_VALUES))

    @patch.object(he_tunnel, "require_root")
    @patch.object(he_tunnel, "require_commands")
    @patch.object(he_tunnel, "check_local_ipv4")
    @patch.object(he_tunnel, "interface_exists", return_value=False)
    @patch.object(he_tunnel, "existing_default_route", return_value=True)
    @patch.object(he_tunnel, "run", return_value=result())
    def test_auto_route_preserves_existing_default(self, run_mock, *_):
        he_tunnel.tunnel_up(he_tunnel.parse_config(HE_VALUES))
        commands = [call.args for call in run_mock.call_args_list]
        self.assertFalse(any(args[:4] == ("ip", "-6", "route", "add") for args in commands))

    @patch.object(he_tunnel, "require_root")
    @patch.object(he_tunnel, "require_commands")
    @patch.object(he_tunnel, "check_local_ipv4")
    @patch.object(he_tunnel, "interface_exists", return_value=False)
    @patch.object(he_tunnel, "existing_default_route", return_value=False)
    @patch.object(he_tunnel, "run", return_value=result())
    def test_auto_route_adds_default_when_absent(self, run_mock, *_):
        he_tunnel.tunnel_up(he_tunnel.parse_config(HE_VALUES))
        self.assertIn(
            ("ip", "-6", "route", "add", "default", "via", "2001:470:ffff:81c::1",
             "dev", "he-ipv6", "metric", "2048"),
            [call.args for call in run_mock.call_args_list],
        )

    @patch.object(he_tunnel, "require_root")
    @patch.object(he_tunnel, "require_commands")
    @patch.object(he_tunnel, "check_local_ipv4")
    @patch.object(he_tunnel, "interface_exists", return_value=False)
    @patch.object(he_tunnel, "existing_default_route", return_value=False)
    @patch.object(he_tunnel, "run", return_value=result())
    def test_no_routed_prefix_adds_only_endpoint_address(self, run_mock, *_):
        config = he_tunnel.parse_config({key: value for key, value in HE_VALUES.items()
                                         if key != "routed_prefix"})
        he_tunnel.tunnel_up(config)
        address_commands = [call.args for call in run_mock.call_args_list
                            if call.args[:4] == ("ip", "-6", "addr", "add")]
        self.assertEqual(address_commands, [
            ("ip", "-6", "addr", "add", config["client_ipv6"], "dev", "he-ipv6")
        ])

    @patch.object(he_tunnel, "require_root")
    @patch.object(he_tunnel, "require_commands")
    @patch.object(he_tunnel, "check_local_ipv4")
    @patch.object(he_tunnel, "interface_exists", return_value=False)
    @patch.object(he_tunnel, "run")
    def test_failed_setup_removes_new_interface(self, run_mock, *_):
        def fake_run(*args, **_kwargs):
            if args[:4] == ("ip", "-6", "addr", "add"):
                raise he_tunnel.TunnelError("address add failed")
            return result()

        run_mock.side_effect = fake_run
        with self.assertRaisesRegex(he_tunnel.TunnelError, "address add failed"):
            he_tunnel.tunnel_up(he_tunnel.parse_config(HE_VALUES))
        self.assertIn(("ip", "tunnel", "del", "he-ipv6"),
                      [call.args for call in run_mock.call_args_list])

    @patch.object(he_tunnel, "require_root")
    @patch.object(he_tunnel, "require_commands")
    @patch.object(he_tunnel, "interface_exists", return_value=True)
    @patch.object(he_tunnel, "run", return_value=result("he-ipv6: ipv6/ip remote 1.2.3.4 local 5.6.7.8"))
    def test_down_refuses_unrelated_interface(self, run_mock, *_):
        with self.assertRaisesRegex(he_tunnel.TunnelError, "does not match"):
            he_tunnel.tunnel_down(he_tunnel.parse_config(HE_VALUES))
        self.assertNotIn(("ip", "tunnel", "del", "he-ipv6"),
                         [call.args for call in run_mock.call_args_list])

    @patch.object(he_tunnel, "require_root")
    @patch.object(he_tunnel, "require_commands")
    @patch.object(he_tunnel, "check_local_ipv4")
    @patch.object(he_tunnel, "interface_exists", return_value=False)
    @patch.object(he_tunnel, "run")
    def test_failed_service_start_removes_new_install_files(self, run_mock, *_):
        def fake_run(*args, **_kwargs):
            if args == ("systemctl", "start", "he-tunnel.service"):
                raise he_tunnel.TunnelError("service start failed")
            return result()

        run_mock.side_effect = fake_run
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(he_tunnel, "CONFIG_PATH", Path(temp_dir) / "etc" / "config.json"), \
                 patch.object(he_tunnel, "SCRIPT_PATH", Path(temp_dir) / "bin" / "he-tunnel"), \
                 patch.object(he_tunnel, "UNIT_PATH", Path(temp_dir) / "units" / "he-tunnel.service"):
                with self.assertRaisesRegex(he_tunnel.TunnelError, "service start failed"):
                    he_tunnel.install(he_tunnel.parse_config(HE_VALUES))
                self.assertFalse(he_tunnel.CONFIG_PATH.exists())
                self.assertFalse(he_tunnel.SCRIPT_PATH.exists())
                self.assertFalse(he_tunnel.UNIT_PATH.exists())


if __name__ == "__main__":
    unittest.main()
