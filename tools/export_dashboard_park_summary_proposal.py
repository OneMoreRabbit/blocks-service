#!/usr/bin/env python3
"""Export the unactivated Dashboard Park-summary candidate contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from blocks_service.app import create_app
from blocks_service.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    spec = create_app(
        Settings(
            db_app_password="unused-export-only", dev_token_secret="unused-export-only"
        )
    ).openapi()
    spec["x-blocks-proposal"] = {
        "name": "dashboard-latest-park-summary",
        "version": "0.1",
        "status": "proposed",
        "active_wire": "0.8",
        "note": "The include projection is an unactivated candidate addition.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} ({len(spec['paths'])} paths; proposed summary 0.1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
