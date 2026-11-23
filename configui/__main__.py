"""Run the config UI: python -m configui [--config-dir config] [--port 8787]"""
import argparse
import os

import uvicorn

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Harness config UI (local only)")
    ap.add_argument("--config-dir", default=os.environ.get("HARNESS_CONFIG_DIR", "config"))
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    # Host is fixed to loopback on purpose: this UI edits security policy.
    uvicorn.run(create_app(args.config_dir), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
