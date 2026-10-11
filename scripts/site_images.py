"""Build every image of the site from the raw app captures, the app icon, and the brand marks.

    uv run python scripts/site_images.py \
        --screenshots ../ios/.screenshots \
        --icon ../design/icon-origami/AppIcon-1024.png \
        --brand-marks ../ios/RelayKit/Sources/DesignSystem/Resources/BrandMarks.xcassets

    uv run python scripts/site_images.py --list     # each output, its source, and where the site shows it

Each output keeps its file name. To change one image, replace its source capture (or edit its row in
`CAPTURES`) and run the script again. No template changes.
Needs ImageMagick 7 (`magick`) with AVIF support, and the SF Pro fonts for the share image.
"""

import argparse
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

OUTPUT = Path(__file__).resolve().parents[1] / "src" / "mcp_client_server" / "site" / "static" / "img"

PHONE = 660
# The part of an iPhone capture that holds the open Dynamic Island: full width, from the top.
ISLAND = (1206, 760)


@dataclass(frozen=True, slots=True)
class Capture:
    """One image of the site. `source` is a path below the screenshots folder."""

    name: str
    source: str
    used: str
    width: int = PHONE
    crop: tuple[int, int] | None = None


def _live(style: str, label: str) -> tuple[Capture, Capture]:
    return (
        Capture(f"iphone-live-{style}", f"site/live-{style}-lock.png", f"Live Activities, layout {label}"),
        Capture(f"island-{style}", f"site/live-{style}-expanded.png", f"Live Activities, layout {label}", crop=ISLAND),
    )


# The `site/` captures come from a Debug build on a simulator, with the status bar set to 9:41.
# The in-app ones use the `-demo` launch argument. The others use a local server, so that the
# widgets, the Live Activity, and the notification are the real system surfaces.
CAPTURES = (
    Capture("iphone-tasks", "site/iphone-tasks.png", "hero, highlights, Task inbox, share image"),
    Capture("iphone-task-detail", "site/iphone-task-detail.png", "hero"),
    Capture("iphone-keys", "site/iphone-keys.png", "hero, Privacy (the keys are masked)"),
    Capture("iphone-settings", "site/iphone-settings.png", "Privacy"),
    Capture("iphone-question-sheet", "site/iphone-question-sheet.png", "Questions"),
    Capture("iphone-question-lock-screen", "site/question-lock.png", "Questions"),
    Capture("iphone-question-notification", "site/question-actions.png", "highlights, Questions"),
    Capture("iphone-live-style", "site/iphone-live-style.png", "highlights"),
    *_live("ring", "Progress Ring"),
    *_live("bar", "Progress Bar"),
    *_live("note", "Status Note"),
    *_live("minimal", "Minimal"),
    Capture("iphone-widgets", "site/widgets-small-large.png", "highlights, Widgets"),
    Capture("iphone-widgets-medium", "site/widgets-medium.png", "Widgets"),
    Capture("iphone-shortcuts", "site/shortcuts.png", "Siri and Shortcuts"),
    Capture("ipad-task-detail", "phase3/tasks-ipad-landscape-detail.png", "Task inbox, iPad", width=1600),
    Capture("watch-question", "phase2-watch/watch-27-focus-yes-no-question.png", "Questions", width=416),
    Capture("watch-notification", "phase2-watch/watch-notification-06-question-actions.png", "Apple Watch", width=416),
    Capture("watch-task", "phase2-watch/watch-07-focus-in-progress.png", "highlights, Apple Watch", width=416),
)

# Output name and the official file in the asset catalog of the app. The file is copied byte for byte.
BRAND_MARKS = (
    ("brand-claude", "claude.imageset/Claude Spark - Clay.svg"),
    ("brand-openai", "openai.imageset/OAI_OpenAI-Blossom_Black.svg"),
    ("brand-cursor", "cursor.imageset/CUBE_2D_LIGHT.svg"),
)

ICONS = ("app-icon.png", "apple-touch-icon.png", "favicon.ico")
SHARE_IMAGE = "og.jpg"
SHARE_SOURCE = "site/iphone-tasks.png"
HEADLINE = ("See your", "agents work.")
SUBLINE = ("Tasks, progress, and questions from your", "coding agents, on your Apple devices.")
FONT_BOLD = "/Library/Fonts/SF-Pro-Display-Semibold.otf"
FONT_TEXT = "/Library/Fonts/SF-Pro-Display-Regular.otf"


def magick(*arguments: str | Path) -> None:
    command = ["magick", *map(str, arguments)]
    subprocess.run(command, check=True)  # noqa: S603  ImageMagick comes from PATH on the developer's machine


def announce(path: Path) -> Path:
    print(f"write {path.relative_to(OUTPUT.parents[4])}")
    return path


def target(name: str, suffix: str = ".avif") -> Path:
    return announce(OUTPUT / f"{name}{suffix}")


def captures(folder: Path) -> None:
    for capture in CAPTURES:
        path = folder / capture.source
        if not path.is_file():
            print(f"skip  {capture.name}: no {capture.source}")
            continue
        # The simulator tags a landscape capture as rotated, but its pixels are upright.
        steps: list[str] = ["-strip", "-orient", "TopLeft"]
        if capture.crop:
            width, height = capture.crop
            steps += ["-crop", f"{width}x{height}+0+0", "+repage"]
        steps += ["-resize", f"{capture.width}x", "-quality", "55", "-define", "heic:speed=2"]
        magick(path, *steps, target(capture.name))


def brand_marks(catalog: Path) -> None:
    for name, source in BRAND_MARKS:
        shutil.copyfile(catalog / source, target(name, ".svg"))


def icon(source: Path) -> None:
    app_icon, touch_icon, favicon = (OUTPUT / name for name in ICONS)
    magick(source, "-strip", "-resize", "192x192", announce(app_icon))
    magick(source, "-strip", "-resize", "180x180", announce(touch_icon))
    magick(source, "-strip", "-define", "icon:auto-resize=48,32,16", announce(favicon))


def rounded(source: Path, size: int, radius: int, output: Path) -> None:
    mask = ("(", "+clone", "-alpha", "extract", "-fill", "black", "-colorize", "100", "-fill", "white")
    corners = ("-draw", f"roundrectangle 0,0 %[fx:w-1],%[fx:h-1] {radius},{radius}", ")")
    apply = ("-alpha", "off", "-compose", "CopyOpacity", "-composite")
    magick(source, "-strip", "-resize", f"{size}x", *mask, *corners, *apply, output)


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
            *("-strip", "-interlace", "JPEG", "-quality", "86", announce(OUTPUT / SHARE_IMAGE)),
        )


def outputs() -> list[tuple[str, str, str]]:
    """Each file that this script writes: its name, its source, and where the site shows it."""
    rows = [(f"{capture.name}.avif", capture.source, capture.used) for capture in CAPTURES]
    rows += [(f"{name}.svg", source, "Connect, agent tabs") for name, source in BRAND_MARKS]
    rows += [(name, "the --icon file", "navigation, closing, browser icon") for name in ICONS]
    rows.append((SHARE_IMAGE, f"{SHARE_SOURCE} and the --icon file", "share image"))
    return rows


def show_list() -> None:
    for name, source, used in outputs():
        print(f"{name:36} {source:58} {used}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--screenshots", type=Path, help="the .screenshots folder of the app")
    parser.add_argument("--icon", type=Path, help="the app icon, 1024 x 1024")
    parser.add_argument("--brand-marks", type=Path, help="the BrandMarks.xcassets folder of the app")
    parser.add_argument("--list", action="store_true", help="show each output and its source, and build nothing")
    options = parser.parse_args()
    if options.list:
        show_list()
        return
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if options.screenshots:
        captures(options.screenshots)
    if options.brand_marks:
        brand_marks(options.brand_marks)
    if options.icon:
        icon(options.icon)
        if options.screenshots:
            share_image(options.icon, options.screenshots / SHARE_SOURCE)


if __name__ == "__main__":
    main()
