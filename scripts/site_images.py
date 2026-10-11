"""Build the images of the site from the app screenshots and the app icon.

    uv run python scripts/site_images.py --screenshots ../ios/.screenshots \
        --icon ../design/icon-origami/AppIcon-1024.png

Each output keeps its file name, so a new screenshot or a new icon needs no code change.
Needs ImageMagick 7 (`magick`) with AVIF support, and the SF Pro fonts for the share image.
"""

import argparse
import subprocess
import tempfile
from pathlib import Path

OUTPUT = Path(__file__).resolve().parents[1] / "src" / "mcp_client_server" / "site" / "static" / "img"

# Output name, source below the screenshots folder, width in pixels. The site shows each at half this width or less.
SCREENS = (
    ("iphone-tasks", "phase1/iphone-01-tasks.png", 660),
    ("iphone-task-detail", "phase1/iphone-05-task-detail.png", 660),
    ("iphone-keys", "phase1/iphone-09-keys.png", 660),
    ("iphone-settings", "phase1/iphone-11-settings.png", 660),
    ("ipad-task-detail", "phase1/ipad-13-landscape-detail.png", 1600),
    # The site draws these in CSS until a screenshot with this name exists.
    ("iphone-question", "phase3/iphone-question.png", 660),
    ("iphone-live-activity", "phase3/iphone-live-activity.png", 660),
    ("iphone-widgets", "phase2-widgets/iphone-widgets.png", 660),
    ("watch-question", "phase2-watch/watch-question.png", 416),
)
HEADLINE = ("See your", "agents work.")
SUBLINE = ("Tasks, progress, and questions from your", "coding agents, on your Apple devices.")
FONT_BOLD = "/Library/Fonts/SF-Pro-Display-Semibold.otf"
FONT_TEXT = "/Library/Fonts/SF-Pro-Display-Regular.otf"


def magick(*arguments: str | Path) -> None:
    command = ["magick", *map(str, arguments)]
    subprocess.run(command, check=True)  # noqa: S603  ImageMagick comes from PATH on the developer's machine


def screens(folder: Path) -> None:
    for name, source, width in SCREENS:
        path = folder / source
        if not path.is_file():
            print(f"skip  {name}: no {source}")
            continue
        # The simulator tags a landscape capture as rotated, but its pixels are upright.
        upright = ("-strip", "-orient", "TopLeft")
        magick(path, *upright, "-resize", f"{width}x", "-quality", "55", "-define", "heic:speed=2", target(name))


def target(name: str, suffix: str = ".avif") -> Path:
    path = OUTPUT / f"{name}{suffix}"
    print(f"write {path.relative_to(OUTPUT.parents[4])}")
    return path


def icon(source: Path) -> None:
    magick(source, "-strip", "-resize", "192x192", target("app-icon", ".png"))
    magick(source, "-strip", "-resize", "180x180", target("apple-touch-icon", ".png"))
    magick(source, "-strip", "-define", "icon:auto-resize=48,32,16", target("favicon", ".ico"))


def rounded(source: Path, size: int, radius: int, output: Path) -> None:
    mask = ("(", "+clone", "-alpha", "extract", "-fill", "black", "-colorize", "100", "-fill", "white")
    corners = ("-draw", f"roundrectangle 0,0 %[fx:w-1],%[fx:h-1] {radius},{radius}", ")")
    magick(
        source,
        "-strip",
        "-resize",
        f"{size}x",
        *mask,
        *corners,
        "-alpha",
        "off",
        "-compose",
        "CopyOpacity",
        "-composite",
        output,
    )


def share_image(icon_source: Path, phone: Path) -> None:
    """The Open Graph image: 1200 x 630, with no product name in it, so that it fits each deployment."""
    with tempfile.TemporaryDirectory() as folder:
        screen, device, mark = (Path(folder) / name for name in ("screen.png", "device.png", "mark.png"))
        rounded(phone, 330, 50, screen)
        rounded(icon_source, 84, 19, mark)
        frame = ("-size", "346x733", "xc:none", "-fill", "#2a2a30", "-draw", "roundrectangle 0,0 345,732 58,58")
        magick(*frame, screen, "-geometry", "+8+8", "-composite", device)
        glow = ("(", "-size", "1200x630", "xc:black", "-fill", "#5b3bf5", "-draw", "ellipse 950,760 470,330 0,360")
        magick(
            *("-size", "1200x630", "xc:black"),
            *(*glow, "-blur", "0x110", ")", "-compose", "Screen", "-composite"),
            *(device, "-gravity", "NorthWest", "-geometry", "+776+70", "-compose", "Over", "-composite"),
            *(mark, "-geometry", "+80+80", "-composite"),
            *("-font", FONT_BOLD, "-pointsize", "84", "-kerning", "-1.6", "-fill", "#f5f5f7"),
            *("-annotate", "+76+304", HEADLINE[0], "-annotate", "+76+396", HEADLINE[1]),
            *("-font", FONT_TEXT, "-pointsize", "29", "-kerning", "0", "-fill", "#a1a1a6"),
            *("-annotate", "+80+488", SUBLINE[0], "-annotate", "+80+528", SUBLINE[1]),
            *("-strip", "-interlace", "JPEG", "-quality", "86", target("og", ".jpg")),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--screenshots", type=Path, help="the .screenshots folder of the app")
    parser.add_argument("--icon", type=Path, help="the app icon, 1024 x 1024")
    options = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if options.screenshots:
        screens(options.screenshots)
    if options.icon:
        icon(options.icon)
        if options.screenshots:
            share_image(options.icon, options.screenshots / SCREENS[0][1])


if __name__ == "__main__":
    main()
