"""Full-system e2e: the edge agent as a systemd service on Debian 13 arm64 in qemu-system-aarch64.

Run from the repo root (needs Docker with buildx, qemu-system-aarch64, an aarch64 UEFI
firmware, ssh, and `pip install httpx`):

    python edge/qemu/qemu_e2e.py

Boots a pinned Debian cloud image (verified by SHA-512). cloud-init installs the agent, the
unit file exactly as shipped in edge/packaging/, and a drop-in that only overrides
ExecStart, the way an operator would. Then it asserts what OpenFleet and the guest observe:

  1. the service starts at boot, as a DynamicUser, and the robot reports the guest's /proc
  2. a root-owned watched process is still visible to it (RUNNING -> STOPPED -> RUNNING)
  3. the hardening holds under a real systemd (exposure score, MemoryMax, no root)
  4. SIGKILL -> Restart=on-failure -> a new run, the old one ended
  5. `systemctl stop` ends the run ABORTED within TimeoutStopSec
  6. reboot: the run ends ABORTED during shutdown (the agent stops before the network goes
     down), and the enabled service starts again on boot
  7. poweroff ends the last run ABORTED, and nothing hit the syscall filter

The guest is a full arm64 Linux system under QEMU: hardware-accelerated (HVF/KVM) when the
host is arm64, otherwise emulated (TCG). Timings in the report carry that label. It's an
emulated computer, not robot hardware.
"""

import argparse
import hashlib
import http.server
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "edge" / "build" / "qemu"
UNIT = ROOT / "edge" / "packaging" / "openfleet-edge-agent.service"

IMAGE = "debian-13-genericcloud-arm64-20261026-2643.qcow2"
IMAGE_URL = f"https://cloud.debian.org/images/cloud/trixie/20261026-2643/{IMAGE}"
IMAGE_SHA512 = (
    "71aa5d5dbcb3d9ecfe96bc3f6dd41b6805a0a41c852204a32747d230b64b959"
    "12297fb453b11a6031fd54d84a0a0a430e137d07e6986dc9ebc9d41483be003c4"
)
FIRMWARE = [  # aarch64 UEFI: Homebrew's qemu, then Debian/Ubuntu's qemu-efi-aarch64
    "/opt/homebrew/share/qemu/edk2-aarch64-code.fd",
    "/usr/local/share/qemu/edk2-aarch64-code.fd",
    "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
    "/usr/share/AAVMF/AAVMF_CODE.fd",
]

API = "http://localhost:8000/api/v1"
AUTH = {"Authorization": "Bearer ofu_demo_viewer_public-not-secret"}
ROBOT = "edge-vm-01"
KEY = json.loads((ROOT / "examples" / "demo-robot-keys.json").read_text())[ROBOT]
COMPOSE = ["docker", "compose"]
SERVICE = "openfleet-edge-agent"
WORKER = "demo-worker"
GUEST_USER = "ofe2e"
# systemd allocates DynamicUser ids from this range (systemd.exec(5)).
DYNAMIC_UIDS = range(61184, 65520)
# A regression guard on the shipped unit's hardening: it scores 1.2 (was 3.4 before #036).
# The rest is what an agent that reads /proc and talks HTTP needs (network, /proc, the rtc).
MAX_EXPOSURE = float(os.environ.get("QEMU_E2E_MAX_EXPOSURE", "1.5"))


class Failed(Exception):
    pass


# --- host side -----------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg, flush=True)


def get(path: str) -> Any:
    r = httpx.get(API + path, timeout=5, headers=AUTH)
    r.raise_for_status()
    return r.json()


def server_now() -> datetime:
    """The backend's clock (Date header), so 'since' comparisons don't depend on ours."""
    r = httpx.get(API + "/health", timeout=5)
    return datetime.strptime(r.headers["date"], "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def wait_for(what: str, cond: Callable[[], Any], timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if value := cond():
                log(f"  ok  {what}")
                return value
        except (httpx.HTTPError, KeyError, ValueError, subprocess.SubprocessError) as exc:
            last = exc
        time.sleep(1)
    raise Failed(f"{what} (timeout {timeout:.0f}s; last error: {last})")


def check(what: str, ok: bool, detail: str = "") -> None:
    if not ok:
        raise Failed(f"{what}{': ' + detail if detail else ''}")
    log(f"  ok  {what}{' (' + detail + ')' if detail else ''}")


def sha512(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def fetch_image() -> Path:
    path = WORK / IMAGE
    if not path.exists():
        log(f"downloading {IMAGE}")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(IMAGE_URL, tmp)  # noqa: S310 (pinned https URL)
        tmp.rename(path)
    if sha512(path) != IMAGE_SHA512:
        path.unlink()
        raise Failed(f"{IMAGE}: SHA-512 mismatch (deleted; rerun to download again)")
    return path


def build_agent() -> Path:
    out = WORK / "bin"
    subprocess.run(
        ["docker", "buildx", "build", "-q", "-f", "edge/qemu/Dockerfile.build"]
        + ["--output", f"type=local,dest={out}", "."],
        cwd=ROOT,
        check=True,
    )
    return out / "openfleet-edge-agent"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def accel(force_tcg: bool = False) -> tuple[list[str], str]:
    """QEMU acceleration args and the label the report carries."""
    machine = platform.machine().lower()
    arm64_host = machine in ("aarch64", "arm64") and not force_tcg
    if arm64_host and sys.platform == "darwin":
        return ["-accel", "hvf", "-cpu", "host"], "QEMU, HVF-accelerated (arm64 host)"
    if arm64_host and os.access("/dev/kvm", os.R_OK | os.W_OK):
        return ["-accel", "kvm", "-cpu", "host"], "QEMU, KVM-accelerated (arm64 host)"
    # A concrete CPU, not `max`: Ubuntu's edk2 (2025.11, the CI runner's) hangs right after its
    # banner with `-cpu max`, and neoverse-n1 has no pointer auth, which is slow to emulate.
    return ["-accel", "tcg", "-cpu", "neoverse-n1"], f"QEMU, emulated (TCG on {machine})"


class SeedServer:
    """cloud-init NoCloud seed over HTTP (the guest reaches the host as 10.0.2.2)."""

    def __init__(self, directory: Path) -> None:
        handler = lambda *a: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(directory)
        )
        http.server.SimpleHTTPRequestHandler.log_message = lambda *_: None
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


def user_data(pubkey: str, port: int) -> str:
    seed = f"http://10.0.2.2:{port}"
    dropin = (
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart=/usr/local/bin/{SERVICE} --robot-id={ROBOT} --name=EDGE-VM-01 "
        "--model=qemu-virt-arm64 --software-version=qemu-e2e --api=http://10.0.2.2:8000 "
        f"--process={WORKER}\n"
    )
    # A stand-in robot process, owned by root: the agent (a DynamicUser with PrivateUsers)
    # must still see it in /proc.
    worker_unit = (
        "[Unit]\nDescription=stand-in robot process\n"
        f"[Service]\nExecStart=/usr/local/bin/{WORKER} infinity\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    config = {
        "hostname": ROBOT,
        "users": [
            {
                "name": GUEST_USER,
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "shell": "/bin/bash",
                "ssh_authorized_keys": [pubkey],
            }
        ],
        "ssh_pwauth": False,
        "write_files": [
            {
                "path": "/etc/openfleet/edge-agent.env",
                "permissions": "0600",
                "content": f"OPENFLEET_API_KEY={KEY}\n",
            },
            {
                "path": f"/etc/systemd/system/{SERVICE}.service.d/e2e.conf",
                "content": dropin,
            },
            {"path": f"/etc/systemd/system/{WORKER}.service", "content": worker_unit},
        ],
        # The binary and the shipped unit come from the seed server, byte for byte.
        "runcmd": [
            "set -e",
            f"python3 -c \"import urllib.request as u; u.urlretrieve('{seed}/{SERVICE}', "
            f"'/usr/local/bin/{SERVICE}'); u.urlretrieve('{seed}/{SERVICE}.service', "
            f"'/etc/systemd/system/{SERVICE}.service')\"",
            f"chmod 755 /usr/local/bin/{SERVICE}",
            "dpkg -s libcurl4t64 >/dev/null 2>&1 || "
            "(apt-get update -q && apt-get install -y -q libcurl4t64)",
            f"cp /usr/bin/sleep /usr/local/bin/{WORKER}",
            "systemctl daemon-reload",
            f"systemctl enable --now {WORKER}",
            f"systemctl enable --now {SERVICE}",
        ],
    }
    return "#cloud-config\n" + json.dumps(config, indent=2) + "\n"


class Guest:
    def __init__(
        self, image: Path, agent: Path, run_dir: Path, mem_mb: int, force_tcg: bool
    ) -> None:
        self.run_dir = run_dir
        self.key = run_dir / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True
        )
        seed = run_dir / "seed"
        seed.mkdir()
        shutil.copy(agent, seed / SERVICE)
        shutil.copy(UNIT, seed / f"{SERVICE}.service")
        self.seed = SeedServer(seed)
        (seed / "user-data").write_text(
            user_data(self.key.with_suffix(".pub").read_text().strip(), self.seed.port)
        )
        (seed / "meta-data").write_text(f"instance-id: {ROBOT}-{int(time.time())}\n")
        (seed / "vendor-data").write_text("")
        self.disk = run_dir / "disk.qcow2"
        subprocess.run(
            ["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(image)]
            + [str(self.disk), "8G"],
            check=True,
        )
        firmware = next((f for f in FIRMWARE if Path(f).exists()), None)
        if firmware is None:
            raise Failed(f"no aarch64 UEFI firmware found (looked in {FIRMWARE})")
        self.accel_args, self.label = accel(force_tcg)
        self.ssh_port = free_port()
        self.mem_mb = mem_mb
        self.firmware = firmware
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        console = (self.run_dir / "console.log").open("ab")
        self.proc = subprocess.Popen(
            ["qemu-system-aarch64", "-M", "virt", *self.accel_args]
            + ["-smp", "2", "-m", str(self.mem_mb), "-bios", self.firmware]
            + ["-display", "none", "-serial", "stdio", "-monitor", "none"]
            + ["-drive", f"if=virtio,format=qcow2,file={self.disk}"]
            + ["-netdev", f"user,id=n0,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22"]
            # romfile=: no iPXE option ROM (we boot from disk; Debian's qemu needs ipxe-qemu
            # for it, which --no-install-recommends leaves out).
            + ["-device", "virtio-net-pci,netdev=n0,romfile="]
            + ["-device", "virtio-rng-pci"]
            + ["-smbios", f"type=1,serial=ds=nocloud;s=http://10.0.2.2:{self.seed.port}/"],
            stdin=subprocess.DEVNULL,
            stdout=console,
            stderr=subprocess.STDOUT,
        )

    def ssh(self, cmd: str, check: bool = True, timeout: float = 60) -> str:
        r = subprocess.run(
            ["ssh", "-i", str(self.key), "-p", str(self.ssh_port)]
            + ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
            + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR"]
            + [f"{GUEST_USER}@127.0.0.1", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
        return r.stdout.strip()

    def ssh_ok(self) -> bool:
        if not self.alive():  # fail now, not after the whole timeout
            tail = (self.run_dir / "console.log").read_text(errors="replace")[-2000:]
            raise Failed(f"QEMU exited (code {self.proc and self.proc.poll()}):\n{tail}")
        return self.ssh("echo up", check=False, timeout=15) == "up"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def save_journal(self, name: str) -> None:
        try:
            (self.run_dir / name).write_text(
                self.ssh(f"sudo journalctl -b --no-pager -u {SERVICE}", check=False)
            )
        except subprocess.SubprocessError:
            pass

    def kill(self) -> None:
        if self.alive():
            assert self.proc is not None
            self.proc.kill()
            self.proc.wait()
        self.seed.close()


# --- the checks ----------------------------------------------------------------------------


def robot() -> dict:
    return get(f"/robots/{ROBOT}")


def new_run_since(since: datetime, not_id: str = "") -> str:
    """The robot's current run if it started after `since` (and isn't `not_id`)."""
    run_id = robot()["current_run_id"] or ""
    if not run_id or run_id == not_id:
        return ""
    run = get(f"/runs/{run_id}")
    return run_id if run["status"] == "RUNNING" and ts(run["started_at"]) >= since else ""


def node_state(name: str) -> str:
    return next((n["state"] for n in get(f"/robots/{ROBOT}/nodes") if n["node"] == name), "")


def run_status(run_id: str) -> str:
    return get(f"/runs/{run_id}")["status"]


def main_pid(g: Guest) -> int:
    return int(g.ssh(f"systemctl show -p MainPID --value {SERVICE}"))


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    t = report["timings_s"]
    WORK.mkdir(parents=True, exist_ok=True)

    log("backend (compose), recreated so it seeds this robot's demo key")
    subprocess.run(
        [*COMPOSE, "up", "-d", "--build", "--force-recreate", "--wait", "postgres", "backend"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    image = fetch_image()
    log("building the agent for the guest (Debian 13 arm64)")
    agent = build_agent()

    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=WORK))
    g = Guest(image, agent, run_dir, args.mem, args.accel == "tcg")
    report["environment"] = g.label
    slow = 1 if "accelerated" in g.label else args.tcg_factor
    log(f"guest: {g.label}; artifacts in {run_dir}")
    try:
        since = server_now()
        t0 = time.monotonic()
        g.start()

        log("1. first boot: cloud-init installs the agent; systemd starts it")
        wait_for("ssh up", g.ssh_ok, timeout=180 * slow)
        t["boot_to_ssh"] = round(time.monotonic() - t0, 1)
        status = g.ssh("cloud-init status --wait --long", check=False, timeout=600 * slow)
        check("cloud-init finished", "status: done" in status, status.splitlines()[0])
        run_id = wait_for(
            "the service registered and opened a run",
            lambda: new_run_since(since),
            timeout=60 * slow,
        )
        t["boot_to_first_run"] = round(time.monotonic() - t0, 1)
        check(
            "service active and enabled",
            g.ssh(f"systemctl is-active {SERVICE}; systemctl is-enabled {SERVICE}").split()
            == ["active", "enabled"],
        )
        check(
            "the shipped unit is installed unchanged",
            g.ssh(f"sha256sum < /etc/systemd/system/{SERVICE}.service").split()[0]
            == hashlib.sha256(UNIT.read_bytes()).hexdigest(),
        )
        wait_for(
            "CPU and memory reported",
            lambda: (r := robot())["cpu_pct"] is not None and r["mem_pct"] is not None,
            timeout=30 * slow,
        )
        # The guest's own kernel: its /proc/meminfo, not the host's or Docker's.
        guest_mem = float(
            g.ssh("awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print 100*(t-a)/t}' /proc/meminfo")
        )
        reported = robot()["mem_pct"]
        check(
            "memory matches the guest's /proc/meminfo",
            abs(reported - guest_mem) < 10,
            f"reported {reported:.1f} %, guest {guest_mem:.1f} %",
        )
        wait_for(
            "edge_agent node RUNNING", lambda: node_state("edge_agent") == "RUNNING", 30 * slow
        )

        log("2. a root-owned watched process, seen through the hardening")
        wait_for(f"{WORKER} RUNNING", lambda: node_state(WORKER) == "RUNNING", 30 * slow)
        g.ssh(f"sudo systemctl stop {WORKER}")
        wait_for(f"{WORKER} STOPPED", lambda: node_state(WORKER) == "STOPPED", 30 * slow)
        g.ssh(f"sudo systemctl start {WORKER}")
        wait_for(f"{WORKER} RUNNING again", lambda: node_state(WORKER) == "RUNNING", 30 * slow)

        log("3. hardening under a real systemd")
        pid = main_pid(g)
        uid = int(g.ssh(f"stat -c %u /proc/{pid}"))
        check("runs as a DynamicUser, not root", uid in DYNAMIC_UIDS, f"uid {uid}")
        caps = g.ssh(f"awk '/CapEff/{{print $2}}' /proc/{pid}/status")
        check("no effective capabilities", int(caps, 16) == 0, f"CapEff {caps}")
        nnp = g.ssh(f"awk '/NoNewPrivs/{{print $2}}' /proc/{pid}/status")
        check("NoNewPrivileges", nnp == "1")
        mem_max = g.ssh(f"systemctl show -p MemoryMax --value {SERVICE}")
        check("MemoryMax enforced by the cgroup", mem_max == str(64 * 1024 * 1024), mem_max)
        mem_now = int(g.ssh(f"systemctl show -p MemoryCurrent --value {SERVICE}"))
        report["memory_current_bytes"] = mem_now
        check("memory well under the cap", mem_now < 32 * 1024 * 1024, f"{mem_now >> 10} KiB")
        sec = g.ssh(f"systemd-analyze security --no-pager {SERVICE}", check=False)
        (run_dir / "systemd-security.txt").write_text(sec + "\n")
        last = next((line for line in reversed(sec.splitlines()) if "exposure level" in line), "")
        exposure = float(last.split(":")[1].split()[0]) if last else 99.0
        report["systemd_exposure"] = exposure
        check("systemd-analyze security exposure", exposure <= MAX_EXPOSURE, last.strip())

        log("4. crash: SIGKILL -> Restart=on-failure -> a new run")
        since = server_now()
        g.ssh(f"sudo systemctl kill --signal=SIGKILL {SERVICE}")
        t1 = time.monotonic()
        new_id = wait_for(
            "restarted and opened a new run",
            lambda: new_run_since(since, not_id=run_id),
            timeout=60 * slow,
        )
        t["crash_to_new_run"] = round(time.monotonic() - t1, 1)
        check(
            "systemd counted one restart",
            g.ssh(f"systemctl show -p NRestarts --value {SERVICE}") == "1",
        )
        wait_for("the killed run is no longer RUNNING", lambda: run_status(run_id) != "RUNNING", 30)
        report["killed_run_status"] = run_status(run_id)
        run_id = new_id

        log("5. systemctl stop -> run ABORTED")
        t1 = time.monotonic()
        g.ssh(f"sudo systemctl stop {SERVICE}", timeout=60 * slow)
        t["systemctl_stop"] = round(time.monotonic() - t1, 1)
        check(
            "stopped within TimeoutStopSec (20 s)",
            t["systemctl_stop"] < 20,
            f"{t['systemctl_stop']} s",
        )
        wait_for("run ended ABORTED", lambda: run_status(run_id) == "ABORTED", 30)

        log("6. reboot: ABORTED during shutdown, started again on boot")
        since = server_now()
        g.ssh(f"sudo systemctl start {SERVICE}")
        run_id = wait_for("started again", lambda: new_run_since(since), timeout=60 * slow)
        g.save_journal("journal-before-reboot.txt")
        since = server_now()
        t1 = time.monotonic()
        g.ssh("sudo systemctl reboot", check=False, timeout=30)
        wait_for(
            "the pre-reboot run ended ABORTED (the agent stopped before the network)",
            lambda: run_status(run_id) == "ABORTED",
            timeout=60 * slow,
        )
        wait_for("ssh up after reboot", g.ssh_ok, timeout=180 * slow)
        run_id = wait_for(
            "the enabled service opened a run after boot",
            lambda: new_run_since(since),
            timeout=90 * slow,
        )
        t["reboot_to_new_run"] = round(time.monotonic() - t1, 1)

        log("7. poweroff")
        g.save_journal("journal.txt")
        denied = g.ssh(
            f"sudo journalctl -b --no-pager -u {SERVICE} | grep -ci 'not permitted' || true"
        )
        check("nothing hit the syscall filter (no EPERM in the journal)", denied == "0")
        g.ssh("sudo systemctl poweroff", check=False, timeout=30)
        wait_for("QEMU exited", lambda: not g.alive(), timeout=120 * slow)
        wait_for("the last run ended ABORTED", lambda: run_status(run_id) == "ABORTED", 30)
    except Failed:
        if g.alive():
            g.save_journal("journal.txt")
        raise
    finally:
        g.kill()
        if not args.keep_disk:
            g.disk.unlink(missing_ok=True)  # the logs stay; the overlay is a few hundred MB


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--mem", type=int, default=1024, help="guest memory, MiB")
    p.add_argument(
        "--tcg-factor", type=float, default=4, help="timeout multiplier when emulated (TCG)"
    )
    p.add_argument("--report", type=Path, help="write the JSON report here")
    p.add_argument(
        "--accel",
        choices=["auto", "tcg"],
        default="auto",
        help="tcg: emulate even on an arm64 host (what CI's x86_64 runners do)",
    )
    p.add_argument("--keep-disk", action="store_true", help="keep the guest's disk overlay")
    args = p.parse_args()
    report: dict[str, Any] = {
        "suite": "qemu-system",
        "robot": ROBOT,
        "image": IMAGE,
        "environment": "",
        "passed": False,
        "timings_s": {},
    }
    try:
        run(args, report)
        report["passed"] = True
    except (Failed, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None)
        detail = detail.strip()[:500] if isinstance(detail, str) else ""
        report["error"] = f"{exc} {detail}".strip()
        log(f"FAIL {report['error']}")
    log(json.dumps(report, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    if report["passed"]:
        log("PASS: edge agent as a systemd service on Debian arm64 (QEMU)")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
