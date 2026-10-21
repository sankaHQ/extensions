# SPDX-License-Identifier: Apache-2.0
from sanka_php_migration.protocol import stdio

from .adapter import handle


def main() -> int:
    return stdio(handle)


if __name__ == "__main__":
    raise SystemExit(main())
