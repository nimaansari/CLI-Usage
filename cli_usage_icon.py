"""Tray icon renderer shared by both frontends (needs Pillow).

The icon is a ring gauge: the arc shows how much of the tightest limit is left,
the number in the middle is that percentage, and the colour follows the same
green / amber / red thresholds as the menu rows. On macOS it is drawn as a
monochrome template image so the menu bar can tint it for light and dark mode.
"""

import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

SIZE = 64          # drawn at 4x and downsampled for smooth edges
_SCALE = 4

COLORS = {
    "healthy":  (34, 197, 94),
    "warning":  (245, 158, 11),
    "critical": (239, 68, 68),
    "unknown":  (99, 102, 241),
}

_FONT_CANDIDATES = (
    # Linux
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    # Windows (Pillow also searches %WINDIR%\Fonts for bare names)
    "segoeuib.ttf",
    "arialbd.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
_font_path = None


def usage_state(pct):
    if pct is None:
        return "unknown"
    if pct < 10:
        return "critical"
    if pct < 30:
        return "warning"
    return "healthy"


def _find_font_path():
    global _font_path
    if _font_path is not None:
        return _font_path
    for cand in _FONT_CANDIDATES:
        try:
            ImageFont.truetype(cand, 10)
            _font_path = cand
            return cand
        except Exception:
            continue
    if shutil.which("fc-match"):
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
            if out:
                ImageFont.truetype(out, 10)
                _font_path = out
                return out
        except Exception:
            pass
    _font_path = ""
    return ""


def _font(px):
    path = _find_font_path()
    if path:
        return ImageFont.truetype(path, px)
    try:
        return ImageFont.load_default(size=px)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def render_icon(pct=None, template=False, size=SIZE):
    """Return an RGBA PIL image for the given remaining percentage (or None)."""
    s = size * _SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if template:
        accent = track = text = (0, 0, 0)
        track_a, disc_a = 70, 0
    else:
        accent = COLORS[usage_state(pct)]
        track = (255, 255, 255)
        text = (255, 255, 255)
        track_a, disc_a = 70, 255

    pad = s * 0.04
    ring_w = s * 0.13
    box = (pad, pad, s - pad, s - pad)

    # Dark disc behind everything so the icon reads on light and dark panels.
    if disc_a:
        d.ellipse(box, fill=(24, 24, 32, disc_a))
        inset = ring_w * 0.35
        box = (pad + inset, pad + inset, s - pad - inset, s - pad - inset)

    d.ellipse(box, outline=track + (track_a,), width=int(ring_w))
    if pct is not None:
        frac = max(0.0, min(100.0, float(pct))) / 100
        if frac > 0:
            d.arc(box, start=-90, end=-90 + 360 * frac, fill=accent + (255,), width=int(ring_w))
    else:
        d.arc(box, start=-90, end=270, fill=accent + (255,), width=int(ring_w))

    label = "CLI" if pct is None else str(int(round(max(0, min(100, pct)))))
    # Fit the text inside the ring.
    inner = (box[2] - box[0]) - 2.6 * ring_w
    px = int(s * (0.46 if len(label) <= 2 else 0.34))
    font = _font(px)
    while px > 8:
        bbox = d.textbbox((0, 0), label, font=font)
        if bbox[2] - bbox[0] <= inner:
            break
        px -= 4
        font = _font(px)
    bbox = d.textbbox((0, 0), label, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((s - w) / 2 - bbox[0], (s - h) / 2 - bbox[1]), label, font=font, fill=text + (255,))

    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    # Quick preview: python cli_usage_icon.py out.png
    out = sys.argv[1] if len(sys.argv) > 1 else "icon-preview.png"
    strip = Image.new("RGBA", (SIZE * 6, SIZE), (0, 0, 0, 0))
    for i, p in enumerate((None, 100, 64, 22, 7)):
        strip.paste(render_icon(p), (i * SIZE, 0))
    strip.paste(render_icon(64, template=True), (5 * SIZE, 0))
    strip.save(out)
    print(out)
