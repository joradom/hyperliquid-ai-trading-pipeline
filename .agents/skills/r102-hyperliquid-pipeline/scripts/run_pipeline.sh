#!/usr/bin/env bash
# Compatibility entry point for the current read-only research CLI.
# No separate scanner pass, batching, pauses, wallet calls or position management.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
cd -- "$PROJECT_ROOT/python"
exec python3 -B -m src.research_pipeline "$@"
