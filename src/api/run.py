"""
API entry point — wraps uvicorn with a --verbosity CLI argument.
================================================================
Usage::

    python -m src.api.run               # INFO logging (default)
    python -m src.api.run --verbosity 4  # DEBUG
    python -m src.api.run --verbosity 1  # ERROR only

Verbosity levels
----------------
  1 → ERROR
  2 → WARNING
  3 → INFO   (default)
  4 → DEBUG
"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from src.logging_config import setup_logging


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="GMR Stock Analysis API")
    parser.add_argument(
        "--verbosity",
        type=int,
        default=3,
        choices=[1, 2, 3, 4],
        metavar="{1..4}",
        help="Log verbosity: 1=ERROR  2=WARNING  3=INFO (default)  4=DEBUG",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root-path", default="/api")
    return parser.parse_args(argv)


def main(argv=None) -> None:  # pylint: disable=missing-function-docstring
    args = _parse_args(argv)
    setup_logging(args.verbosity)

    uvicorn.run(
        "src.api.app:app",
        host=args.host,
        port=args.port,
        workers=1,
        root_path=args.root_path,
        access_log=False,   # replaced by our HTTP middleware
    )


if __name__ == "__main__":
    main(sys.argv[1:])
