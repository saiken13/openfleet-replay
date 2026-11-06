"""openfleet-collector: run on the robot (inside its ROS 2 environment).

    source /opt/ros/jazzy/setup.bash
    openfleet-collector --config collector.yaml

On SIGINT/SIGTERM it flushes buffered batches (bounded) and ends its run as SUCCESS.
"""

import argparse
import logging
import signal
import sys
import threading

from openfleet_collector.config import load_config
from openfleet_collector.uplink import Uplink


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="openfleet-collector", description=__doc__.split("\n")[0])
    p.add_argument("--config", "-c", required=True)
    p.add_argument("--api", help="override api_url")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.api:
        cfg.api_url = args.api

    try:
        from openfleet_collector.node import spin
    except ImportError as exc:
        print(
            f"error: ROS 2 Python packages not found ({exc}). "
            "Source your ROS 2 setup first, e.g. `source /opt/ros/jazzy/setup.bash`.",
            file=sys.stderr,
        )
        return 2

    uplink = Uplink(
        cfg.api_url,
        cfg.robot_id,
        robot_name=cfg.robot_name,
        robot_model=cfg.robot_model,
        software_version=cfg.software_version,
        buffer_max=cfg.buffer_max_batches,
        timeout_s=cfg.request_timeout_s,
    )
    uplink.start()  # blocks until the API is reachable and the run is open
    sender = threading.Thread(target=uplink.run_forever, name="openfleet-uplink", daemon=True)
    sender.start()

    # rclpy installs its own SIGINT handler; make SIGTERM (docker stop, systemd) behave the same.
    signal.signal(signal.SIGTERM, lambda *_: signal.raise_signal(signal.SIGINT))
    try:
        spin(cfg, uplink)
    finally:
        uplink.stop()
        sender.join(timeout=2)
        uplink.end("SUCCESS")
        uplink.close()
        logging.info("collector stopped (sent=%d dropped=%d)", uplink.sent, uplink.dropped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
