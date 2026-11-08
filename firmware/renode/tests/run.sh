#!/usr/bin/env bash
# Runs the Robot Framework firmware suite with Renode's own runner (renode-test).
# Needs the firmware built (firmware/build/openfleet-fw.elf) and Robot Framework installed
# (the dev image has it; see edge/docker/Dockerfile.dev).
#
#   scripts/edge-dev.sh bash -c 'cd /src && firmware/renode/tests/run.sh [results-dir]'
#
# Results (report.html, log.html, robot_output.xml, firmware-metrics.json) go to the given
# directory, default firmware/build/robot.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/../../build/robot}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"
renode_test="$(command -v renode-test || echo /opt/renode/renode-test)"
cd "$out"  # renode-test writes some of its files to the current directory
"$renode_test" -r "$out" --variable "OUTPUT_DIR:$out" "$here/firmware.robot"
