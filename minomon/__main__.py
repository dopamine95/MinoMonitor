"""
Entrypoint: `python -m minomon`.

Modes:
  (default)     interactive Textual TUI dashboard
  --stub        TUI with fake oscillating data (UI dev / preview)
  --snapshot    one-shot text summary, no TUI; prints and exits
  --vibe        start the TUI with vibe view enabled
"""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minomon", description="Mino Monitor")
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Use fake oscillating data (UI dev mode).",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Print a one-shot text summary and exit (no TUI).",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="With --snapshot: emit plain ASCII output (no ANSI styling).",
    )
    parser.add_argument(
        "--vibe",
        action="store_true",
        help="Start the TUI with vibe view enabled.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=30,
        help="Number of top processes to display (default: 30; "
             "snapshot uses 12 unless overridden).",
    )
    args = parser.parse_args(argv)

    if args.snapshot:
        from .snapshot import run_snapshot
        # Snapshot defaults to a tighter top-N for paste-friendliness;
        # respect the user's --top-n if they passed something explicit.
        explicit = "--top-n" in (argv if argv is not None else sys.argv)
        top_n = args.top_n if explicit else 12
        return run_snapshot(top_n=top_n, use_color=not args.no_color)

    from .app import MinoMonitorApp
    if args.stub:
        from .data.stub_sampler import StubSampler
        sampler = StubSampler(top_n=args.top_n)
    else:
        from .data.sampler import Sampler
        sampler = Sampler(top_n=args.top_n)

    app = MinoMonitorApp(sampler=sampler, vibe_mode=args.vibe)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
