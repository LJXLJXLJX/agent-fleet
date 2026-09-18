"""artifact-cache-prepare entry point (dispatches to per-subcommand handlers)."""

from __future__ import annotations

from typing import Sequence

from .commands import (
    analyze_command,
    mirror_command,
    prepare_command,
)
from .parser import parser
from .warm_commands import (
    configure_apt_command,
    configure_downloads_command,
    refresh_downloads_command,
    warm_downloads_command,
    warm_git_command,
    warm_packages_command,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "analyze":
        return analyze_command(args)
    if args.command == "mirror-images":
        return mirror_command(args)
    if args.command == "configure-apt":
        return configure_apt_command(args)
    if args.command == "configure-downloads":
        return configure_downloads_command(args)
    if args.command == "warm-downloads":
        return warm_downloads_command(args)
    if args.command == "refresh-downloads":
        return refresh_downloads_command(args)
    if args.command == "warm-git":
        return warm_git_command(args)
    if args.command == "prepare":
        return prepare_command(args)
    if args.command == "warm-packages":
        return warm_packages_command(args)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
