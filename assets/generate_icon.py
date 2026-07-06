"""Regenerates assets/app_icon.ico from the same design as make_app_icon() in app_v2.py."""
from pathlib import Path
from PIL import Image, ImageDraw

SIZES = (16, 24, 32, 48, 64, 128, 256)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size: int) -> Image.Image:
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    top = (255, 92, 129)
    bottom = (163, 32, 74)
    radius = s * .22
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=255)
    grad = Image.new("RGB", (s, s))
    grad_draw = ImageDraw.Draw(grad)
    for y in range(s):
        for_t = y / max(1, s - 1)
        grad_draw.line([(0, y), (s, y)], fill=lerp(top, bottom, for_t))
    img.paste(grad, (0, 0), mask)

    draw.polygon([
        (s * .38, s * .18),
        (s * .68, s * .35),
        (s * .38, s * .52),
    ], fill=(255, 255, 255, 235))

    bar_h = max(1.5 * scale, s * .10)
    draw.rounded_rectangle(
        [s * .16, s * .62, s * .16 + s * .68, s * .62 + bar_h],
        radius=bar_h / 2, fill=(255, 255, 255, 235),
    )
    draw.rounded_rectangle(
        [s * .28, s * .78, s * .28 + s * .44, s * .78 + bar_h],
        radius=bar_h / 2, fill=(255, 255, 255, 235),
    )

    return img.resize((size, size), Image.LANCZOS)


def main():
    base = render(max(SIZES))
    out = Path(__file__).parent / "app_icon.ico"
    base.save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
