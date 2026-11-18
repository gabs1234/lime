"""Apply child-only resource ceilings, then replace this process with Typst.

A separate interpreter avoids running Python preexec_fn code after a fork in
an embedding application that may already have threads.
"""

import os
import resource
import sys


def main() -> None:
    # Snap's Go launcher otherwise sizes its thread pool to all host CPUs before
    # Typst's own --jobs limit can take effect, exhausting a bounded address space.
    os.environ["GOMAXPROCS"] = "2"
    for kind, maximum in (
        (resource.RLIMIT_CPU, 11),
        (resource.RLIMIT_FSIZE, 32 * 1024 * 1024),
        (resource.RLIMIT_AS, 2 * 1024 * 1024 * 1024),
    ):
        try:
            _, hard = resource.getrlimit(kind)
            cap = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
            resource.setrlimit(kind, (cap, cap))
        except (OSError, ValueError):
            # macOS does not enforce every resource class supported on Linux.
            pass
    try:
        os.execv(sys.argv[1], sys.argv[1:])
    except OSError as error:
        print(f"error: cannot run Typst: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
