"""Multi-account switcher for Claude Code."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Distribution renamed to ``claude-swap-2`` (local fork): avoids the PyPI
    # ``claude-swap`` update-check false positive and any accidental remote pull.
    __version__ = version("claude-swap-2")
except PackageNotFoundError:  # running from a source tree with no installed dist
    __version__ = "0.0.0"

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
