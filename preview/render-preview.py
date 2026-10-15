#!/usr/bin/env python3
"""
render-preview.py — render a mock-up of the macos-dark theme.

This is NOT a screenshot. It is a synthetic scene assembled from the theme's own
files so that it cannot drift out of sync with them:

  * every colour comes from themes/macos-dark/labwc/themerc
  * geometry (corner radius, blur tuning, font sizes/weights, button order)
    comes from rc.xml.snippet
  * the traffic lights are rasterised from the theme's real SVG buttons

The only thing it invents is the wallpaper behind everything. The blur radius is
interpreted at *output* resolution rather than native, so the effect actually
reads at README size.

Usage:
    python3 preview/render-preview.py                  # preview/theme-mockup.png, 800px
    python3 preview/render-preview.py --width 1600     # bigger render
    python3 preview/render-preview.py --out /tmp/x.png

Needs: pillow, rsvg-convert (librsvg), and the Inter variable font.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("error: pillow is required (pip install pillow)")

REPO = Path(__file__).resolve().parent.parent
DEFAULT_THEME = REPO / "themes" / "macos-dark" / "labwc"
DEFAULT_SNIPPET = REPO / "rc.xml.snippet"
DEFAULT_OUT = REPO / "preview" / "theme-mockup.png"

FONT_CANDIDATES = [
    Path("/usr/share/fonts/inter/InterVariable.ttf"),
    Path("/usr/local/share/fonts/InterVariable.ttf"),
    Path.home() / ".local/share/fonts/InterVariable.ttf",
]

# themerc weight name -> Inter variable font instance name
VARIATIONS = {
    "thin": "Thin",
    "extralight": "ExtraLight",
    "light": "Light",
    "regular": "Regular",
    "medium": "Medium",
    "semibold": "SemiBold",
    "bold": "Bold",
    "extrabold": "ExtraBold",
    "black": "Black",
}

SS = 2  # supersampling factor: draw at SS x, downscale with LANCZOS at the end

MENU_ITEMS = ["Open", "Open With…", "Copy", None, "Get Info", None, "Move to Trash", "Quit"]
MENU_HOVERED = "Copy"
OSD_TITLES = ["notes.md", "labwc-rc.xml", "mountain.jpg"]
OSD_SELECTED = 1


# --------------------------------------------------------------------- themerc
def parse_themerc(path: Path) -> dict[str, str]:
    """Parse labwc's `key: value` themerc format into a plain dict.

    Note the `#` trap: in this format `#` starts both a comment *and* every
    colour value. Stripping comments from the whole line would silently turn
    `window.active.title.bg.color: #2e2e32` into an empty value, so comments
    are only removed from the key, and from a value that does not itself start
    with `#`.
    """
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.split("#", 1)[0].strip()
        value = value.strip()
        if not value.startswith("#"):
            value = value.split("#", 1)[0].strip()
        if key and value:
            out[key] = value
    return out


def parse_color(value: str, default=(0, 0, 0, 255)) -> tuple[int, int, int, int]:
    """`#rgb` / `#rrggbb` / `#rrggbbaa` -> RGBA tuple."""
    value = value.strip().lower()
    if not value.startswith("#"):
        return default
    h = value[1:]
    try:
        if len(h) == 3:
            return tuple(int(c * 2, 16) for c in h) + (255,)  # type: ignore[return-value]
        if len(h) == 6:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (255,)  # type: ignore[return-value]
        if len(h) == 8:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4, 6))  # type: ignore[return-value]
    except ValueError:
        return default
    return default


def color(themerc: dict[str, str], key: str, default=(0, 0, 0, 255)):
    raw = themerc.get(key)
    return parse_color(raw, default) if raw else default


def number(themerc: dict[str, str], key: str, default: float) -> float:
    try:
        return float(themerc.get(key, default))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- rc.xml.snippet
def parse_snippet(path: Path) -> dict:
    """Pull cornerRadius, the <blur> tuning, the font table and the button order."""
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
    root = ET.fromstring(text)

    def num(parent, tag, default):
        node = parent.find(tag) if parent is not None else None
        if node is None or not (node.text or "").strip():
            return default
        try:
            return float(node.text.strip())
        except ValueError:
            return default

    # values here are already multipliers (brightness 0.9), not percentages
    blur_node = root.find("blur")
    blur = {
        "passes": num(blur_node, "passes", 3),
        "radius": num(blur_node, "radius", 5),
        "brightness": num(blur_node, "brightness", 1.0),
        "contrast": num(blur_node, "contrast", 1.0),
        "saturation": num(blur_node, "saturation", 1.0),
    }

    theme_node = root.find("theme")
    corner_radius = int(num(theme_node, "cornerRadius", 8))

    fonts = {"__base__": {"size": 10.0, "weight": "regular"}}
    if theme_node is not None:
        base = theme_node.find("font")
        if base is not None and base.get("size"):
            fonts["__base__"] = {
                "size": float(base.get("size", 10)),
                "weight": base.get("weight", "regular"),
            }
        for place in theme_node.findall("font[@place]"):
            fonts[place.get("place", "")] = {
                "size": num(place, "size", 10),
                "weight": (place.findtext("weight") or "regular").strip(),
            }

    titlebar = theme_node.find("titlebar") if theme_node is not None else None
    return {
        "blur": blur,
        "corner_radius": corner_radius,
        "fonts": fonts,
        "layout": (titlebar.findtext("layout") or "").strip() if titlebar is not None else "",
        "show_title": (titlebar.findtext("showTitle") == "yes") if titlebar is not None else True,
    }


# ---------------------------------------------------------------------- assets
def find_font() -> Path:
    override = os.environ.get("FONT_PATH")
    if override and Path(override).is_file():
        return Path(override)
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "error: Inter not found. Install it (e.g. `sudo pacman -S ttf-inter`) "
        "or point FONT_PATH at InterVariable.ttf."
    )


def load_font(path: Path, size: float, weight: str = "regular") -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(path), max(1, round(size * SS)))
    instance = VARIATIONS.get(weight.strip().lower())
    if instance:
        try:
            font.set_variation_by_name(instance.encode())
        except OSError:
            pass  # static font, or FreeType built without variable-font support
    return font


def render_svg(path: Path, size: int = 20) -> Image.Image:
    """Rasterise a button SVG at SS scale with librsvg; returns RGBA."""
    if not shutil.which("rsvg-convert"):
        raise SystemExit("error: rsvg-convert is required (Arch/CachyOS: librsvg)")
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        subprocess.run(
            ["rsvg-convert", "-w", str(round(size * SS)), "-h", str(round(size * SS)),
             str(path), "-o", str(out)],
            check=True,
            capture_output=True,
        )
        return Image.open(out).convert("RGBA")
    finally:
        out.unlink(missing_ok=True)


# ------------------------------------------------------------------- wallpaper
def make_wallpaper(w: int, h: int) -> Image.Image:
    """A deterministic abstract wallpaper — the only invented part of the scene."""
    base = Image.new("RGB", (w, h), (12, 14, 22))
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / max(1, h - 1)
        draw.line([(0, y), (w, y)], fill=(int(18 + 26 * t), int(22 + 14 * t), int(44 + 30 * t)))

    blobs = Image.new("RGB", (w, h), (0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    bd.ellipse([w * 0.52, h * 0.30, w * 1.28, h * 1.42], fill=(214, 108, 38))
    bd.ellipse([w * 0.68, h * 0.58, w * 1.10, h * 1.20], fill=(236, 156, 58))
    bd.ellipse([-w * 0.18, -h * 0.42, w * 0.52, h * 0.46], fill=(46, 82, 168))
    bd.ellipse([w * 0.14, h * 0.54, w * 0.62, h * 1.30], fill=(96, 46, 120))
    blobs = blobs.filter(ImageFilter.GaussianBlur(max(w, h) * 0.085))

    base = Image.blend(base, blobs, 0.62)
    base = ImageEnhance.Color(base).enhance(1.15)
    return base.filter(ImageFilter.GaussianBlur(max(w, h) * 0.012))


def make_backdrop(wallpaper: Image.Image, blur_cfg: dict) -> Image.Image:
    """What a client asking for backdrop blur actually sees.

    The radius is read at output resolution so the effect reads at README size;
    `passes` is compounded into extra kernel iterations for the frosted look.
    """
    radius = max(1.0, blur_cfg["radius"] * SS)
    for _ in range(max(0, int(blur_cfg["passes"]) // 2 - 1)):
        wallpaper = wallpaper.filter(ImageFilter.GaussianBlur(radius * 0.55))
    out = wallpaper.filter(ImageFilter.GaussianBlur(radius))
    out = ImageEnhance.Brightness(out).enhance(blur_cfg["brightness"])
    out = ImageEnhance.Contrast(out).enhance(blur_cfg["contrast"])
    return ImageEnhance.Color(out).enhance(blur_cfg["saturation"])


# -------------------------------------------------------------------- painting
def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius, fill=255)
    return mask


def drop_shadow(canvas: Image.Image, box, radius: int, size: float, rgba,
                offset: float = 3.0) -> None:
    """labwc's soft window shadow. `size` is the themerc shadow size (a fade distance)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pad = max(2, int(size * 1.4))
    layer = Image.new("L", (w + pad * 2, h + pad * 2), 0)
    ImageDraw.Draw(layer).rounded_rectangle([pad, pad, pad + w - 1, pad + h - 1], radius, fill=255)
    layer = layer.filter(ImageFilter.GaussianBlur(max(1.0, size * 0.45)))
    shadow = Image.new("RGBA", layer.size, rgba[:3] + (255,))
    shadow.putalpha(layer.point(lambda a: a * rgba[3] // 255))
    canvas.alpha_composite(shadow, (x0 - pad, y0 - pad + round(offset * SS)))


def text_at(draw: ImageDraw.ImageDraw, xy, s: str, font, fill, halign="left", valign="center"):
    """Draw text with real bbox-based alignment (PIL's default 'la' anchor drifts)."""
    bbox = draw.textbbox((0, 0), s, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = xy[0] - bbox[0] if halign == "left" else xy[0] - tw / 2 - bbox[0]
    if valign == "center":
        y = xy[1] - th / 2 - bbox[1]
    else:
        y = xy[1] - bbox[1]
    draw.text((x, y), s, font=font, fill=fill)


def draw_window(canvas: Image.Image, *, box, title: str, active: bool, themerc: dict,
                snippet: dict, backdrop: Image.Image, svgs: dict, font_path: Path) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    radius = snippet["corner_radius"]
    state = "active" if active else "inactive"

    title_bg = color(themerc, f"window.{state}.title.bg.color")
    border = color(themerc, f"window.{state}.border.color")
    label = color(themerc, f"window.{state}.label.text.color")
    shadow_size = number(themerc, f"window.{state}.shadow.size", 40)
    shadow_color = color(themerc, f"window.{state}.shadow.color", (0, 0, 0, 90))

    pad_w = number(themerc, "window.titlebar.padding.width", 8)
    pad_h = number(themerc, "window.titlebar.padding.height", 2)
    btn = number(themerc, "window.button.width", 20)
    spacing = number(themerc, "window.button.spacing", 0)
    hover_bg = color(themerc, "window.button.hover.bg.color", (255, 255, 255, 0))
    hover_radius = int(number(themerc, "window.button.hover.bg.corner-radius", btn / 2))

    # 20px traffic lights, not the text, are what set the titlebar height
    titlebar_h = round((btn + pad_h * 2) * SS)

    drop_shadow(canvas, box, radius * SS, shadow_size * SS, shadow_color, offset=shadow_size * 0.08)

    # body: blurred wallpaper clipped to the rounded corners
    body_top = titlebar_h
    body = backdrop.crop((x0, y0 + body_top, x1, y1)).convert("RGBA")
    body.putalpha(rounded_mask((w, h - body_top), radius * SS))
    canvas.alpha_composite(body, (x0, y0 + body_top))

    # frame, then the titlebar over the top of it
    ImageDraw.Draw(canvas).rounded_rectangle(
        [x0, y0, x1 - 1, y1 - 1], radius * SS, outline=border, width=SS
    )
    bar = Image.new("RGBA", (w, titlebar_h), title_bg)
    bar.putalpha(rounded_mask((w, titlebar_h), radius * SS))
    canvas.alpha_composite(bar, (x0, y0))
    # a rounded rect rounds the bottom too; the titlebar's bottom edge must stay square
    ImageDraw.Draw(canvas).rectangle(
        [x0, y0 + titlebar_h - radius * SS, x1, y0 + titlebar_h], fill=title_bg
    )

    draw = ImageDraw.Draw(canvas)

    if snippet["show_title"] and title:
        spec = snippet["fonts"].get("ActiveWindow" if active else "InactiveWindow",
                                    snippet["fonts"]["__base__"])
        text_at(
            draw, ((x0 + x1) / 2, y0 + titlebar_h / 2), title,
            load_font(font_path, spec["size"], spec["weight"]), label, halign="center",
        )

    # traffic lights, right-aligned, in the rc.xml layout order.
    # labwc's <layout> is `left:centre:right` with buttons inside a group
    # separated by commas, so `:iconify,max,close` means an empty left section
    # and the three buttons on the right.
    known = {"iconify", "max", "close"}
    order = [p for p in re.split(r"[,:]", snippet["layout"]) if p in known]
    cy = y0 + titlebar_h / 2
    cursor = x1 - pad_w * SS
    for name in reversed(order):
        cx = cursor - btn / 2 * SS
        # the showcase hovers the close button on the active window
        hovered = active and name == "close"
        # hover assets are named close_hover-active (underscore), others hyphen
        stems = ([f"{name}_hover-{state}"] if hovered else []) + [f"{name}-{state}"]
        svg = next((svgs[s] for s in stems if s in svgs), None)
        if svg is None:
            raise SystemExit(f"error: no SVG for button {name!r} ({state}) in theme dir")
        if hovered and hover_bg[3]:
            # macOS draws a circular halo behind the hovered light
            d = round(hover_radius * 2 * SS)
            halo = Image.new("RGBA", (d, d), (0, 0, 0, 0))
            ImageDraw.Draw(halo).ellipse([0, 0, d - 1, d - 1], fill=hover_bg)
            canvas.alpha_composite(
                halo, (round(cx - d / 2), round(cy - d / 2))
            )
        canvas.alpha_composite(svg, (round(cx - btn / 2 * SS), round(cy - btn / 2 * SS)))
        cursor -= (btn + spacing) * SS


def draw_menu(canvas: Image.Image, *, at, themerc: dict, snippet: dict, font_path: Path):
    """Context menu. Returns the box it actually drew, so callers can position it."""
    x0, y0 = at
    bg = color(themerc, "menu.items.bg.color")
    border = color(themerc, "menu.border.color", (0, 0, 0, 255))
    text = color(themerc, "menu.items.text.color")
    hot_bg = color(themerc, "menu.items.active.bg.color")
    hot_text = color(themerc, "menu.items.active.text.color")
    sep = color(themerc, "menu.separator.color")

    px = number(themerc, "menu.items.padding.x", 10)
    py = number(themerc, "menu.items.padding.y", 5)

    hspec = snippet["fonts"].get("MenuHeader", snippet["fonts"]["__base__"])
    ispec = snippet["fonts"].get("MenuItem", snippet["fonts"]["__base__"])
    header_font = load_font(font_path, hspec["size"], hspec["weight"])
    item_font = load_font(font_path, ispec["size"], ispec["weight"])

    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    header_h = hspec["size"] + py * 2
    row_h = ispec["size"] + py * 2
    sep_h = py + 1

    widest = 0
    for label in MENU_ITEMS:
        if label is None:
            continue
        b = probe.textbbox((0, 0), label, font=item_font)
        widest = max(widest, b[2] - b[0])
    b = probe.textbbox((0, 0), "File", font=header_font)
    widest = max(widest, b[2] - b[0])

    min_w = number(themerc, "menu.width.min", 180)
    w = round(max(min_w, widest + px * 2 + 10) * SS)
    h = round((py + header_h + sep_h + sum(row_h if i else sep_h for i in MENU_ITEMS) + py) * SS)
    x1, y1 = x0 + w, y0 + h

    drop_shadow(canvas, (x0, y0, x1, y1), 8 * SS, 26 * SS, (0, 0, 0, 120), offset=4)

    layer = Image.new("RGBA", (w, h), bg)
    layer.putalpha(rounded_mask((w, h), 8 * SS))
    draw = ImageDraw.Draw(layer)

    y = py * SS
    text_at(draw, (px * SS, y + header_h / 2), "File", header_font, text)
    y += header_h * SS
    draw.rectangle([px * SS, y, w - px * SS, y + SS], fill=sep)
    y += SS

    for label in MENU_ITEMS:
        if label is None:
            draw.rectangle([px * SS, y + sep_h / 2 * SS / SS, w - px * SS,
                            y + sep_h / 2 * SS / SS + SS], fill=sep)
            y += sep_h * SS
            continue
        is_hot = label == MENU_HOVERED
        if is_hot:
            draw.rounded_rectangle([px * SS / 2, y, w - px * SS / 2, y + row_h * SS],
                                   5 * SS, fill=hot_bg)
        text_at(draw, (px * SS, y + row_h * SS / 2), label, item_font,
                hot_text if is_hot else text)
        y += row_h * SS

    canvas.alpha_composite(layer, (x0, y0))
    return x0, y0, x1, y1


def draw_osd(canvas: Image.Image, *, at, themerc: dict, snippet: dict, backdrop: Image.Image,
             font_path: Path):
    """Window-switcher OSD: a row of window thumbnails, one selected."""
    x0, y0 = at
    bg = color(themerc, "osd.bg.color")
    border = color(themerc, "osd.border.color", (0, 0, 0, 255))
    text = color(themerc, "osd.label.text.color")
    sel = color(themerc, "osd.window-switcher.preview.border.color")

    spec = snippet["fonts"].get("OnScreenDisplay", snippet["fonts"]["__base__"])
    font = load_font(font_path, spec["size"], spec["weight"])

    gap = 8
    pad = 10
    card_w = 150
    card_h = 82
    label_h = spec["size"] + 6
    w = round((card_w * len(OSD_TITLES) + gap * (len(OSD_TITLES) + 1) + pad) * SS)
    h = round((card_h + label_h + pad) * SS)
    x1, y1 = x0 + w, y0 + h

    drop_shadow(canvas, (x0, y0, x1, y1), 10 * SS, 34 * SS, (0, 0, 0, 140), offset=5)

    layer = Image.new("RGBA", (w, h), bg)
    layer.putalpha(rounded_mask((w, h), 10 * SS))
    ImageDraw.Draw(layer).rounded_rectangle(
        [0, 0, w - 1, h - 1], 10 * SS, outline=border, width=SS
    )
    canvas.alpha_composite(layer, (x0, y0))

    for i, title in enumerate(OSD_TITLES):
        cx = x0 + round((pad + i * (card_w + gap)) * SS)
        cy = y0 + pad * SS
        cw, ch = round(card_w * SS), round(card_h * SS)

        thumb = backdrop.crop((cx, cy, cx + cw, cy + ch)).convert("RGBA")
        thumb.putalpha(rounded_mask((cw, ch), 5 * SS))
        canvas.alpha_composite(thumb, (cx, cy))

        selected = i == OSD_SELECTED
        if selected:
            ImageDraw.Draw(canvas).rounded_rectangle(
                [cx, cy, cx + cw - 1, cy + ch - 1], 5 * SS, outline=sel, width=2 * SS
            )
        text_at(ImageDraw.Draw(canvas), (cx + cw / 2, cy + ch + (label_h * SS) / 2),
                title, font, sel if selected else text, halign="center")

    return x0, y0, x1, y1


# ------------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description="Render a mock-up of the macos-dark theme.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output PNG path")
    ap.add_argument("--width", type=int, default=800, help="output width in px")
    ap.add_argument("--theme-dir", type=Path, default=DEFAULT_THEME)
    ap.add_argument("--snippet", type=Path, default=DEFAULT_SNIPPET)
    args = ap.parse_args()

    themerc = parse_themerc(args.theme_dir / "themerc")
    snippet = parse_snippet(args.snippet)
    font_path = find_font()

    W, H = args.width * SS, round(args.width * 1056 / 1826) * SS
    print(f"themerc      {len(themerc)} keys  <- {args.theme_dir / 'themerc'}")
    print(f"snippet      cornerRadius={snippet['corner_radius']} "
          f"blur.radius={snippet['blur']['radius']} layout={snippet['layout']!r}")
    print(f"font         {font_path}")
    print(f"output       {args.out} ({args.width}x{round(args.width * 1056 / 1826)})")

    svgs = {p.stem: render_svg(p) for p in sorted(args.theme_dir.glob("*.svg"))}
    print(f"svg buttons  {len(svgs)} rasterised at {20 * SS}px via rsvg-convert")

    wallpaper = make_wallpaper(W, H)
    backdrop = make_backdrop(wallpaper, snippet["blur"])
    canvas = wallpaper.convert("RGBA")

    def s(*v):
        return tuple(round(x * SS) for x in v)

    # inactive window first, so the active one overlaps it
    draw_window(canvas, box=s(344, 132, 756, 376), title="~/Pictures/mountain.jpg",
                active=False, themerc=themerc, snippet=snippet, backdrop=backdrop,
                svgs=svgs, font_path=font_path)
    draw_window(canvas, box=s(44, 40, 452, 296), title="~/Documents/notes.md — Editor",
                active=True, themerc=themerc, snippet=snippet, backdrop=backdrop,
                svgs=svgs, font_path=font_path)
    draw_menu(canvas, at=s(120, 110), themerc=themerc, snippet=snippet, font_path=font_path)
    draw_osd(canvas, at=s(154, 344), themerc=themerc, snippet=snippet, backdrop=backdrop,
             font_path=font_path)

    out = canvas.convert("RGB").resize(
        (args.width, round(args.width * 1056 / 1826)), Image.LANCZOS
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.save(args.out, "PNG", optimize=True)
    print(f"wrote        {args.out} ({out.width}x{out.height}, "
          f"{args.out.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())