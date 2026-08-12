"""Discover this Mac's Tailscale IPv4 address for the iPhone-facing listener.

Primary method: scan local interfaces for a utun* interface with an address
in Tailscale's CGNAT range (100.64.0.0/10) via psutil — this doesn't depend
on which Tailscale variant (standalone vs. sandboxed App Store build) is
installed, unlike shelling out to the CLI. `tailscale ip -4` is used only
as a secondary check if the interface scan finds nothing.
"""
import ipaddress
import logging
import subprocess

import psutil

logger = logging.getLogger(__name__)

TAILSCALE_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")


def discover_tailscale_ipv4() -> str | None:
    return _discover_via_interfaces() or _discover_via_cli()


def _discover_via_interfaces() -> str | None:
    try:
        for iface_name, addrs in psutil.net_if_addrs().items():
            if not iface_name.startswith("utun"):
                continue
            for addr in addrs:
                if addr.family.name != "AF_INET":
                    continue
                try:
                    if ipaddress.ip_address(addr.address) in TAILSCALE_CGNAT_RANGE:
                        return addr.address
                except ValueError:
                    continue
    except Exception:
        logger.exception("Failed to enumerate network interfaces via psutil")
    return None


def _discover_via_cli() -> str | None:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if lines:
                return lines[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None
