#!/usr/bin/env python3
"""Export the published contract artefacts.

The API description is generated from the running application rather than
maintained beside it, so it cannot describe endpoints the service does not have.
The event schemas are hand-authored in contracts/events/ -- they are the source
the code is validated against, not an output -- and are copied unchanged.

  python tools/export_contract.py --out <dir>

`--out` is normally a component's provides/ folder in the Atlas vault, which is
where clients read it: the point of publishing artefacts is that a client repo
never needs this repo's source.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from blocks_service.app import create_app  # noqa: E402
from blocks_service.config import Settings  # noqa: E402

API_VERSION = "0.2"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    # Importing the app is enough to describe it; no database is touched because
    # the lifespan never runs.
    app = create_app(Settings(db_app_password="unused", dev_token_secret="unused-export-only"))
    spec = app.openapi()

    openapi_path = args.out / f"blocks-service-api-openapi-v{API_VERSION.replace('.', '_')}.json"
    openapi_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"  wrote  {openapi_path.name}  ({len(spec['paths'])} paths)")

    for schema in sorted((ROOT / "contracts" / "events").glob("*.schema.json")):
        shutil.copy2(schema, args.out / schema.name)
        print(f"  wrote  {schema.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
