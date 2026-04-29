"""
Entrypoint: `python -m minomon`. Optional flag to use the stub sampler for
UI development without touching real system state.
"""

import argparse
import sys

from .app import MinoMonitorApp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minomon", description="Mino Monitor")
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Use fake oscillating data (UI dev mode).",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=30,
        help="Number of top processes to display (default: 30).",
    )
    args = parser.parse_args(argv)

    if args.stub:
        from .data.stub_sampler import StubSampler
        sampler = StubSampler(top_n=args.top_n)
    else:
        from .data.sampler import Sampler
        sampler = Sampler(top_n=args.top_n)

    app = MinoMonitorApp(sampler=sampler)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
