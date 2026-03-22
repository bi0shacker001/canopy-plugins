"""
Proxmox VE plugin for Canopy.

Connects to a Proxmox VE cluster (or Trellis) via the PVE API.
Provides VM/CT/node management with optional Trellis enhanced features.

Single URL — auto-detects Trellis if present. User can override with
trellis_mode: auto | on | off.
"""
from __future__ import annotations

import httpx
import urllib3

# Suppress SSL warnings for self-signed PVE certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ProxmoxPlugin:
    """Canopy plugin for Proxmox VE management."""

    def domain(self) -> str:
        return "proxmox"

    def name(self) -> str:
        return "Proxmox VE"

    def version(self) -> str:
        return "0.1.0"

    def setup(self, credentials: dict, config: dict) -> bool:
        """Configure the PVE client from credentials."""
        self._url = config.get("url", "").rstrip("/")
        self._token = credentials.get("token", "")
        self._username = credentials.get("username", "")
        self._password = credentials.get("password", "")
        self._trellis_mode = config.get("trellis_mode", "auto")
        self._ticket = ""
        self._csrf = ""
        self._has_trellis = False

        if not self._url:
            return False

        # Authenticate
        if self._token:
            # API token auth — no login needed
            self._auth_headers = {"Authorization": f"PVEAPIToken={self._token}"}
        elif self._username and self._password:
            # Ticket auth — login to get ticket
            if not self._login():
                return False
        else:
            return False

        # Detect Trellis
        if self._trellis_mode == "on":
            self._has_trellis = True
        elif self._trellis_mode == "off":
            self._has_trellis = False
        else:
            self._detect_trellis()

        return True

    def teardown(self) -> None:
        pass

    def fetch_resources(self) -> list[dict]:
        """Fetch all VMs, CTs, and nodes from the PVE cluster."""
        resources = self._get("/api2/json/cluster/resources")
        if not resources:
            return []

        result = []
        data = resources.get("data", [])

        for r in data:
            rtype = r.get("type", "")

            if rtype in ("qemu", "lxc"):
                if r.get("template", 0) == 1:
                    continue

                kind = "ct" if rtype == "lxc" else "vm"
                vmid = r.get("vmid", 0)
                result.append({
                    "resource_id": f"proxmox.{kind}_{vmid}",
                    "domain": "proxmox",
                    "state": r.get("status", "unknown"),
                    "attributes": {
                        "name": r.get("name", f"{kind.upper()} {vmid}"),
                        "type": kind,
                        "vmid": vmid,
                        "node": r.get("node", ""),
                        "subtitle": f"on {r.get('node', '?')} · VMID {vmid}",
                        "kind": kind,
                        "cpu": r.get("cpu", 0),
                        "mem": r.get("mem", 0),
                        "maxmem": r.get("maxmem", 0),
                        "disk": r.get("disk", 0),
                        "maxdisk": r.get("maxdisk", 0),
                        "uptime": r.get("uptime", 0),
                        "tags": r.get("tags", ""),
                        "trellis_enhanced": self._has_trellis,
                    },
                })

            elif rtype == "node":
                node = r.get("node", "")
                cpu_pct = r.get("cpu", 0) * 100
                mem_pct = (r.get("mem", 0) / r.get("maxmem", 1)) * 100 if r.get("maxmem") else 0
                result.append({
                    "resource_id": f"proxmox.node_{node}",
                    "domain": "proxmox",
                    "state": r.get("status", "unknown"),
                    "attributes": {
                        "name": node,
                        "type": "node",
                        "node": node,
                        "subtitle": f"CPU: {cpu_pct:.1f}% · RAM: {mem_pct:.1f}%",
                        "cpu": r.get("cpu", 0),
                        "mem": r.get("mem", 0),
                        "maxmem": r.get("maxmem", 0),
                        "uptime": r.get("uptime", 0),
                        "trellis_enhanced": self._has_trellis,
                    },
                })

        return result

    def get_actions(self) -> list[dict]:
        """Return all actions this plugin provides."""
        actions = [
            {"action": "start_vm", "description": "Start a virtual machine", "params": ["node", "vmid"]},
            {"action": "stop_vm", "description": "Stop a virtual machine", "params": ["node", "vmid"]},
            {"action": "shutdown_vm", "description": "Gracefully shut down a VM", "params": ["node", "vmid"]},
            {"action": "reboot_vm", "description": "Reboot a VM", "params": ["node", "vmid"]},
            {"action": "start_ct", "description": "Start a container", "params": ["node", "vmid"]},
            {"action": "stop_ct", "description": "Stop a container", "params": ["node", "vmid"]},
            {"action": "shutdown_ct", "description": "Shut down a container", "params": ["node", "vmid"]},
        ]

        if self._has_trellis:
            actions.extend([
                {"action": "run_checks", "description": "Run health checks (Trellis)", "params": ["node", "kind", "vmid"]},
                {"action": "exec_guest", "description": "Execute command in guest (Trellis)", "params": ["node", "kind", "vmid", "command"]},
            ])

        return actions

    def execute_action(self, action: str, params: dict) -> dict:
        """Execute a management action."""
        node = params.get("node", "")
        vmid = params.get("vmid", 0)

        action_map = {
            "start_vm":    ("qemu", "start"),
            "stop_vm":     ("qemu", "stop"),
            "shutdown_vm": ("qemu", "shutdown"),
            "reboot_vm":   ("qemu", "reboot"),
            "start_ct":    ("lxc", "start"),
            "stop_ct":     ("lxc", "stop"),
            "shutdown_ct": ("lxc", "shutdown"),
        }

        if action in action_map:
            kind, cmd = action_map[action]
            result = self._post(f"/api2/json/nodes/{node}/{kind}/{vmid}/status/{cmd}")
            return {"success": result is not None, "message": f"{cmd} sent", "data": result or {}}

        if action == "run_checks" and self._has_trellis:
            kind = params.get("kind", "vm")
            result = self._get(f"/enhanced/{node}/{kind}/{vmid}/checks")
            return {"success": True, "data": result or {}}

        if action == "exec_guest" and self._has_trellis:
            kind = params.get("kind", "vm")
            command = params.get("command", "")
            result = self._post(f"/enhanced/{node}/{kind}/{vmid}/exec", {"command": command})
            return {"success": result is not None, "data": result or {}}

        return {"success": False, "message": f"Unknown action: {action}"}

    # ── Private HTTP helpers ──────────────────────────────────────────────

    def _login(self) -> bool:
        """Get PVE ticket via standard /api2/json/access/ticket endpoint."""
        try:
            resp = httpx.post(
                f"{self._url}/api2/json/access/ticket",
                data={"username": self._username, "password": self._password},
                verify=False, timeout=15,
            )
            if resp.status_code != 200:
                return False
            data = resp.json().get("data", {})
            self._ticket = data.get("ticket", "")
            self._csrf = data.get("CSRFPreventionToken", "")
            if not self._ticket:
                return False
            self._auth_headers = {"Cookie": f"PVEAuthCookie={self._ticket}"}
            return True
        except Exception:
            return False

    def _detect_trellis(self) -> None:
        """Probe for Trellis-specific endpoint."""
        try:
            resp = httpx.get(
                f"{self._url}/trellis/privileges",
                headers=self._auth_headers, verify=False, timeout=5,
            )
            self._has_trellis = resp.status_code == 200
        except Exception:
            self._has_trellis = False

    def _get(self, path: str) -> dict | None:
        try:
            resp = httpx.get(
                f"{self._url}{path}",
                headers=self._auth_headers, verify=False, timeout=30,
            )
            if resp.status_code >= 400:
                return None
            return resp.json()
        except Exception:
            return None

    def _post(self, path: str, body: dict | None = None) -> dict | None:
        try:
            headers = dict(self._auth_headers)
            if self._csrf:
                headers["CSRFPreventionToken"] = self._csrf
            resp = httpx.post(
                f"{self._url}{path}",
                json=body, headers=headers, verify=False, timeout=30,
            )
            if resp.status_code >= 400:
                return None
            return resp.json()
        except Exception:
            return None


# Entry point — the core calls this to instantiate the plugin
def create_plugin():
    return ProxmoxPlugin()
