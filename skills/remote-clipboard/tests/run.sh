#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bash -n scripts/clip.sh
python3 -B -m unittest discover -s tests -v
