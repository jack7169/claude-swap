"""Entry point for the bundled ``.app`` — launches the menu bar directly.

py2app runs this module as the app's main script. Unlike ``cswap --menubar`` it
does not go through the CLI argument parser (a bundle receives launchd/Finder
argv, and the CLI's required action group would reject an empty argv). All
account logic still lives in ``ClaudeAccountSwitcher``.
"""

from __future__ import annotations


def main() -> int:
    from claude_swap import menubar
    from claude_swap.switcher import ClaudeAccountSwitcher

    return menubar.run(ClaudeAccountSwitcher())


if __name__ == "__main__":
    import sys

    sys.exit(main())
