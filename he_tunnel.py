#!/usr/bin/env python3
"""Configure a SIT (6in4) IPv6 tunnel on a systemd Linux host."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


INTERFACE = "he-ipv6"
CONFIG_PATH = Path("/etc/he-tunnel/config.json")
SCRIPT_PATH = Path("/usr/local/sbin/he-tunnel")
UNIT_PATH = Path("/etc/systemd/system/he-tunnel.service")
SERVICE = "he-tunnel.service"


class TunnelError(Exception):
    pass


def run(*args, check=True):
    try:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise TunnelError(f"Cannot run {args[0]}: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise TunnelError(f"{' '.join(args)}: {detail}")
    return result


def require_root():
    if os.geteuid() != 0:
        raise TunnelError("Run this command as root (for example, with sudo).")


def require_commands(*names):
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise TunnelError(f"Missing required commands: {', '.join(missing)}")


def parse_config(values):
    if not isinstance(values, dict):
        raise TunnelError("Tunnel configuration must be an object.")
    try:
        server_v4 = ipaddress.IPv4Address(values["server_ipv4"])
        client_v4 = ipaddress.IPv4Address(values["client_ipv4"])
        local_v4 = ipaddress.IPv4Address(values.get("local_ipv4") or values["client_ipv4"])
        server_v6 = ipaddress.IPv6Interface(values["server_ipv6"])
        client_v6 = ipaddress.IPv6Interface(values["client_ipv6"])
        routed_value = values.get("routed_prefix")
        routed = ipaddress.IPv6Network(routed_value, strict=True) if routed_value else None
        route_policy = values["default_route"]
    except (ValueError, KeyError) as exc:
        raise TunnelError(f"Invalid tunnel address or prefix: {exc}") from exc

    if not (server_v4.is_global and client_v4.is_global):
        raise TunnelError("The server and client IPv4 addresses must be public addresses.")
    if server_v4 == client_v4:
        raise TunnelError("The server and client IPv4 addresses must differ.")
    if local_v4.is_loopback or local_v4.is_link_local or local_v4.is_multicast or local_v4.is_unspecified:
        raise TunnelError("The local IPv4 address must be a usable host address.")
    if server_v6.network.prefixlen != 64 or client_v6.network.prefixlen != 64:
        raise TunnelError("The endpoint IPv6 addresses must include /64.")
    if server_v6.network != client_v6.network or server_v6.ip == client_v6.ip:
        raise TunnelError("The IPv6 endpoints must be different addresses in the same /64.")
    if routed and (not 48 <= routed.prefixlen <= 64 or routed.overlaps(client_v6.network)):
        raise TunnelError("The routed prefix must be a separate IPv6 /48 to /64.")
    if not (server_v6.ip.is_global and client_v6.ip.is_global) or (routed and not routed.is_global):
        raise TunnelError("The IPv6 endpoints and routed prefix must be global addresses.")
    if route_policy not in ("auto", "yes", "no"):
        raise TunnelError("default_route must be auto, yes, or no.")

    return {
        "server_ipv4": str(server_v4),
        "client_ipv4": str(client_v4),
        "local_ipv4": str(local_v4),
        "server_ipv6": str(server_v6),
        "client_ipv6": str(client_v6),
        "routed_prefix": str(routed) if routed else None,
        "routed_address": str(routed.network_address + 1) if routed else None,
        "default_route": route_policy,
    }


def local_ipv4_present(address):
    result = run("ip", "-j", "-4", "addr", "show")
    try:
        links = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TunnelError(f"Cannot read local IPv4 addresses: {exc}") from exc
    return any(info.get("local") == address for link in links for info in link.get("addr_info", []))


def check_local_ipv4(config):
    if not local_ipv4_present(config["local_ipv4"]):
        raise TunnelError(
            f"Local IPv4 {config['local_ipv4']} is not assigned to this host. "
            "Set --local-ipv4 to the address on the host's network interface."
        )
    route = run("ip", "-4", "route", "get", config["server_ipv4"])
    if not re.search(rf"\bsrc {re.escape(config['local_ipv4'])}\b", route.stdout):
        raise TunnelError("The IPv4 route to the tunnel server does not use the configured local IPv4.")


def interface_exists():
    return run("ip", "link", "show", "dev", INTERFACE, check=False).returncode == 0


def existing_default_route():
    return bool(run("ip", "-6", "route", "show", "default").stdout.strip())


def tunnel_up(config):
    require_root()
    require_commands("ip")
    check_local_ipv4(config)
    if interface_exists():
        raise TunnelError(f"Interface {INTERFACE} already exists; refusing to replace it.")

    run("ip", "tunnel", "add", INTERFACE, "mode", "sit", "remote", config["server_ipv4"],
        "local", config["local_ipv4"], "ttl", "255")
    try:
        run("ip", "link", "set", "dev", INTERFACE, "mtu", "1480", "up")
        run("ip", "-6", "addr", "add", config["client_ipv6"], "dev", INTERFACE)
        if config["routed_address"]:
            run("ip", "-6", "addr", "add", f"{config['routed_address']}/128", "dev", INTERFACE)
        if config["default_route"] == "yes" or (
            config["default_route"] == "auto" and not existing_default_route()
        ):
            run("ip", "-6", "route", "add", "default", "via",
                config["server_ipv6"].split("/")[0], "dev", INTERFACE, "metric", "2048")
    except Exception as exc:
        cleanup = run("ip", "tunnel", "del", INTERFACE, check=False)
        if cleanup.returncode:
            raise TunnelError(
                f"Tunnel setup failed: {exc}; cleanup failed: {cleanup.stderr.strip()}"
            ) from exc
        raise


def tunnel_down(config):
    require_root()
    require_commands("ip")
    if not interface_exists():
        return
    details = run("ip", "tunnel", "show", INTERFACE).stdout
    if not ("ipv6/ip" in details and
            re.search(rf"\bremote {re.escape(config['server_ipv4'])}\b", details) and
            re.search(rf"\blocal {re.escape(config['local_ipv4'])}\b", details)):
        raise TunnelError(f"Interface {INTERFACE} does not match this tunnel; refusing to delete it.")
    run("ip", "tunnel", "del", INTERFACE)


def load_config():
    try:
        with CONFIG_PATH.open(encoding="utf-8") as handle:
            return parse_config(json.load(handle))
    except (OSError, json.JSONDecodeError) as exc:
        raise TunnelError(f"Cannot read {CONFIG_PATH}: {exc}") from exc


def write_new_file(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        path.chmod(mode)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def install(config):
    require_root()
    require_commands("ip", "systemctl", "python3")
    check_local_ipv4(config)
    for path in (CONFIG_PATH, SCRIPT_PATH, UNIT_PATH):
        if path.exists():
            raise TunnelError(f"{path} already exists; refusing to overwrite it.")
    if interface_exists():
        raise TunnelError(f"Interface {INTERFACE} already exists; refusing to replace it.")

    unit = """[Unit]
Description=IPv6 SIT tunnel
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/env python3 /usr/local/sbin/he-tunnel up
ExecStop=/usr/bin/env python3 /usr/local/sbin/he-tunnel down

[Install]
WantedBy=multi-user.target
"""
    created = []
    started = False
    enabling = False
    try:
        write_new_file(CONFIG_PATH, json.dumps(config, indent=2) + "\n", 0o600)
        created.append(CONFIG_PATH)
        write_new_file(SCRIPT_PATH, Path(__file__).resolve().read_text(encoding="utf-8"), 0o755)
        created.append(SCRIPT_PATH)
        write_new_file(UNIT_PATH, unit, 0o644)
        created.append(UNIT_PATH)
        run("systemctl", "daemon-reload")
        run("systemctl", "start", SERVICE)
        started = True
        enabling = True
        run("systemctl", "enable", SERVICE)
    except Exception as exc:
        cleanup_errors = []
        if enabling:
            result = run("systemctl", "disable", SERVICE, check=False)
            if result.returncode:
                cleanup_errors.append(f"disable: {result.stderr.strip()}")
        if started:
            result = run("systemctl", "stop", SERVICE, check=False)
            if result.returncode:
                cleanup_errors.append(f"stop: {result.stderr.strip()}")
        if interface_exists():
            cleanup_errors.append(f"interface {INTERFACE} still exists")
        if cleanup_errors:
            raise TunnelError(
                f"Install failed: {exc}; cleanup failed: {'; '.join(cleanup_errors)}. "
                "Installed files were kept for recovery."
            ) from exc
        for path in reversed(created):
            path.unlink(missing_ok=True)
        result = run("systemctl", "daemon-reload", check=False)
        if result.returncode:
            raise TunnelError(f"Install failed: {exc}; daemon-reload failed: {result.stderr.strip()}") from exc
        raise


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "install"):
        command = subparsers.add_parser(name, help=f"{name} the tunnel configuration")
        command.add_argument("--server-ipv4", required=True)
        command.add_argument("--client-ipv4", required=True)
        command.add_argument("--local-ipv4", help="IPv4 on this host, if the public client IPv4 is NAT mapped")
        command.add_argument("--server-ipv6", required=True, help="server IPv6, including /64")
        command.add_argument("--client-ipv6", required=True, help="client IPv6, including /64")
        command.add_argument("--routed-prefix", help="optional routed IPv6 /48 to /64")
        command.add_argument("--default-route", choices=("auto", "yes", "no"), default="auto")
    subparsers.add_parser("up", help="bring up the installed tunnel")
    subparsers.add_parser("down", help="remove the installed tunnel interface")
    subparsers.add_parser("status", help="show interface and route state")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.command in ("preview", "install"):
            config = parse_config(vars(args))
            if args.command == "preview":
                print(json.dumps(config, indent=2))
                return 0
            install(config)
            print(f"Installed and started {SERVICE} on {INTERFACE}.")
        elif args.command == "up":
            tunnel_up(load_config())
        elif args.command == "down":
            tunnel_down(load_config())
        else:
            require_commands("ip")
            if not interface_exists():
                raise TunnelError(f"Interface {INTERFACE} is not present.")
            print(run("ip", "tunnel", "show", INTERFACE).stdout, end="")
            print(run("ip", "-6", "addr", "show", "dev", INTERFACE, check=False).stdout, end="")
            print(run("ip", "-6", "route", "show", "default", check=False).stdout, end="")
    except TunnelError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
