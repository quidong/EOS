"""Deprecated: ``python -m akkudoktoreos.rl29mcp`` runs ``python -m akkudoktoreos.mcp``."""

import sys

from akkudoktoreos.mcp.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
