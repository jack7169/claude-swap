"""Render the claude-swap glyph into an .icns (run inside the build venv).

Uses Cocoa (pyobjc, present in the build venv) to draw the app glyph onto a
1024x1024 image, writes the required iconset sizes, then calls `iconutil`.
Output: packaging/claude-swap.icns. Best-effort: if drawing fails, exit non-zero
and let the build proceed without a custom icon.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from AppKit import (
    NSBitmapImageRep,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSPNGFileType,
    NSString,
)

GLYPH = "⇄"  # the menu-bar glyph
HERE = Path(__file__).resolve().parent
ICONSET = HERE / "claude-swap.iconset"
ICNS = HERE / "claude-swap.icns"
SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _render_png(px: int) -> bytes:
    img = NSImage.alloc().initWithSize_((px, px))
    img.lockFocus()
    NSColor.clearColor().set()
    style = NSMutableParagraphStyle.alloc().init()
    style.setAlignment_(2)  # NSTextAlignmentCenter is 2 on macOS
    attrs = {
        NSFontAttributeName: NSFont.systemFontOfSize_(px * 0.62),
        NSForegroundColorAttributeName: NSColor.labelColor(),
        NSParagraphStyleAttributeName: style,
    }
    s = NSString.stringWithString_(GLYPH)
    size = s.sizeWithAttributes_(attrs)
    rect = NSMakeRect(0, (px - size.height) / 2.0, px, size.height)
    s.drawInRect_withAttributes_(rect, attrs)
    img.unlockFocus()
    tiff = img.TIFFRepresentation()
    rep = NSBitmapImageRep.imageRepWithData_(tiff)
    return bytes(rep.representationUsingType_properties_(NSPNGFileType, {}))


def main() -> int:
    ICONSET.mkdir(parents=True, exist_ok=True)
    for px in SIZES:
        (ICONSET / f"icon_{px}x{px}.png").write_bytes(_render_png(px))
        if px <= 512:  # @2x variants
            (ICONSET / f"icon_{px}x{px}@2x.png").write_bytes(_render_png(px * 2))
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True
    )
    print(f"wrote {ICNS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
