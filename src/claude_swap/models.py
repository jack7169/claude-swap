"""Data models for Claude Swap."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


def _is_wsl_kernel() -> bool:
    """Return True if the running Linux kernel identifies as WSL.

    Under WSL, the kernel reports a "microsoft"/"WSL" marker in both
    /proc/sys/kernel/osrelease and /proc/version. This is the standard robust
    WSL signal and works even when WSL_DISTRO_NAME is unset. Any read error
    (missing or unreadable /proc on a non-WSL or unusual system) is treated as
    "not WSL" so detection degrades to plain Linux rather than crashing.

    Callers must only invoke this on the Linux branch; it performs Linux-only
    /proc reads.
    """
    for proc_path in ("/proc/sys/kernel/osrelease", "/proc/version"):
        try:
            with open(proc_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        lowered = content.lower()
        if "microsoft" in lowered or "wsl" in lowered:
            return True
    return False


class Platform(Enum):
    """Supported platforms."""

    MACOS = auto()
    LINUX = auto()
    WSL = auto()
    WINDOWS = auto()
    UNKNOWN = auto()

    @classmethod
    def detect(cls) -> Platform:
        """Detect current platform.

        Uses sys.platform rather than platform.system() because the latter
        calls platform.uname() on Windows, which runs a WMI query that can
        hang indefinitely when the WMI service is slow or unresponsive.
        """
        if sys.platform == "darwin":
            return cls.MACOS
        elif sys.platform == "win32":
            return cls.WINDOWS
        elif sys.platform.startswith("linux"):
            # WSL_DISTRO_NAME is the cheapest signal, but it is frequently
            # unset (non-login shells, services, some terminals), so fall back
            # to the kernel-identity probe before concluding plain Linux.
            if os.environ.get("WSL_DISTRO_NAME") or _is_wsl_kernel():
                return cls.WSL
            return cls.LINUX
        return cls.UNKNOWN


@dataclass
class AccountInfo:
    """Information about a managed account."""

    email: str
    uuid: str
    organization_uuid: str
    organization_name: str
    added: str
    number: int

    @property
    def is_organization(self) -> bool:
        """Whether this is an organization account."""
        return bool(self.organization_uuid)

    @property
    def display_label(self) -> str:
        """Display label: 'email [OrgName]' or 'email [personal]'."""
        tag = self.organization_name if self.organization_name else "personal"
        return f"{self.email} [{tag}]"

    @classmethod
    def from_dict(cls, number: int, data: dict) -> AccountInfo:
        """Create AccountInfo from dictionary."""
        return cls(
            email=data.get("email", ""),
            uuid=data.get("uuid", ""),
            organization_uuid=data.get("organizationUuid", "") or "",
            organization_name=data.get("organizationName", "") or "",
            added=data.get("added", ""),
            number=number,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "email": self.email,
            "uuid": self.uuid,
            "organizationUuid": self.organization_uuid,
            "organizationName": self.organization_name,
            "added": self.added,
        }


@dataclass
class SwitchTransaction:
    """Represents a switch operation that can be rolled back."""

    original_credentials: str
    original_config: str
    original_account_num: str
    original_email: str
    config_path: Path
    completed_steps: list[str] = field(default_factory=list)
    # Prior backup snapshot for the current account, captured BEFORE Step 1
    # overwrites it with the freshly-read live state. ``had_prior_backup`` is
    # False when no backup existed yet, in which case the ``backup_written``
    # rollback removes the newly-written one instead of restoring an old copy.
    prior_backup_credentials: str = ""
    prior_backup_config: str = ""
    had_prior_backup: bool = False

    def record_step(self, step: str) -> None:
        """Record a completed step."""
        self.completed_steps.append(step)

    def rollback(self, switcher: ClaudeAccountSwitcher) -> bool:
        """Rollback all completed steps in reverse order.

        Returns:
            True if rollback successful, False if any step failed.
        """
        success = True
        for step in reversed(self.completed_steps):
            try:
                if step == "credentials_written":
                    switcher._write_credentials(self.original_credentials)
                elif step == "config_written":
                    switcher._atomic_write_text(
                        self.config_path, self.original_config
                    )
                elif step == "sequence_updated":
                    data = switcher._get_sequence_data()
                    if data:
                        data["activeAccountNumber"] = int(self.original_account_num)
                        data["lastUpdated"] = get_timestamp()
                        switcher._write_json(switcher.sequence_file, data)
                elif step == "backup_written":
                    # Step 1 overwrote the current account's backup with the
                    # freshly-read live state. Restore the prior backup if one
                    # existed; otherwise remove the just-written backup so a
                    # possibly-wrong live snapshot can't masquerade as the
                    # current account's good backup.
                    if self.had_prior_backup:
                        switcher._write_account_credentials(
                            self.original_account_num,
                            self.original_email,
                            self.prior_backup_credentials,
                        )
                        switcher._write_account_config(
                            self.original_account_num,
                            self.original_email,
                            self.prior_backup_config,
                        )
                    else:
                        switcher._delete_account_credentials(
                            self.original_account_num, self.original_email
                        )
                        switcher._delete_account_config(
                            self.original_account_num, self.original_email
                        )
                switcher._logger.info(f"Rolled back step: {step}")
            except Exception as e:
                switcher._logger.error(f"Failed to rollback step {step}: {e}")
                success = False
        return success


def get_timestamp() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
