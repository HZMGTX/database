"""Entry point: ``python3 -m database``."""

import sys


def _main() -> int:
    # Imported lazily so that ``python3 -m database`` on an unsupported
    # interpreter fails in cli.main()'s version guard with a readable message
    # rather than on some import deep in the package.
    from db.cli import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(_main())
