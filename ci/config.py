#!/usr/bin/env python3
"""Write the CI benchmark config: targets from `configure`, load from a named profile.

Usage: ci/config.py PROFILE LABEL OUTPUT
Prints the workloads for each mode as KEY=value lines for $GITHUB_OUTPUT.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent

PROFILES = {
    # A smoke-sized comparison for checking a change; minutes, not a publishable result.
    "quick": {
        "load": {"rates": [100], "concurrency": [1, 8, 32], "duration_seconds": 15,
                 "warmup_seconds": 5, "cooldown_seconds": 2, "repetitions": 2},
        "concurrency_workloads": ["reads", "subscriptions", "writes"],
        "rate_workloads": ["reads", "subscriptions"],
    },
    # The set published on the documentation site.
    "full": {
        "load": {"rates": [100, 500], "concurrency": [1, 8, 32, 128], "duration_seconds": 20,
                 "warmup_seconds": 5, "cooldown_seconds": 3, "repetitions": 3},
        "concurrency_workloads": ["reads", "subscriptions", "writes", "mixed", "stats", "user-list", "user-detail"],
        "rate_workloads": ["reads", "subscriptions"],
    },
}


def main(profile, label, output):
    selected = PROFILES[profile]
    config = json.loads((ROOT / "config/local.json").read_text())
    # Every target gets a CHECKPOINT and VACUUM before each measured run, so one
    # run's dirty pages and dead rows are not flushed during the next.
    config["load"].update(selected["load"], settle_databases=True)
    config["experiment_label"] = label
    Path(output).write_text(json.dumps(config, indent=2) + "\n")
    print("concurrency_workloads=" + " ".join(selected["concurrency_workloads"]))
    print("rate_workloads=" + " ".join(selected["rate_workloads"]))


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in PROFILES:
        sys.exit(__doc__)
    main(*sys.argv[1:])
