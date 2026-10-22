# SPDX-License-Identifier: Apache-2.0
from .lifecycle import handle
from .protocol import stdio


def main() -> int:
    return stdio(handle)


if __name__ == "__main__":
    raise SystemExit(main())
