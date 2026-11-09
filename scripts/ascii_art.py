"""Generate the ASCII portrait used in the profile card.

Run once (or whenever the avatar changes) and commit the result:

    python scripts/ascii_art.py path/to/avatar.png > assets/ascii.txt

The defaults are tuned for the current GitHub avatar (400x400). The mask keeps
only the head and shoulders, so the busy background does not turn into noise.
Requires Pillow (pip install pillow).
"""

import argparse
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageOps

RAMP = " .:-=+*#%@"


def build_mask(size):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((158, 8, 272, 182), fill=255)  # head
    draw.polygon([(185, 150), (245, 150), (250, 195), (180, 195)], fill=255)  # neck
    draw.polygon(  # shoulders
        [(160, 175), (270, 170), (340, 215), (372, 300), (380, 330),
         (110, 330), (120, 240), (135, 195)],
        fill=255,
    )
    return mask.filter(ImageFilter.GaussianBlur(2))


def to_ascii(path, box, cols, rows):
    src = Image.open(path).convert("L").resize((400, 400))
    mask = build_mask(src.size).crop(box)
    img = src.crop(box).filter(ImageFilter.UnsharpMask(3, 160, 2))
    img = ImageOps.equalize(img, mask=mask)
    # Dark pixels become dense characters, so hair and jacket keep their shape.
    img = ImageOps.invert(img)
    img = img.resize((cols, rows), Image.LANCZOS)
    mask = mask.resize((cols, rows), Image.LANCZOS)

    lines = []
    for y in range(rows):
        row = ""
        for x in range(cols):
            if mask.getpixel((x, y)) < 128:
                row += " "
            else:
                v = img.getpixel((x, y))
                row += RAMP[min(len(RAMP) - 1, v * len(RAMP) // 256)]
        lines.append(row.rstrip())
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image")
    parser.add_argument("--box", default="140,0,320,250", help="crop box x0,y0,x1,y1")
    parser.add_argument("--cols", type=int, default=40)
    parser.add_argument("--rows", type=int, default=26)
    args = parser.parse_args()
    box = tuple(int(v) for v in args.box.split(","))
    sys.stdout.write(to_ascii(args.image, box, args.cols, args.rows) + "\n")


if __name__ == "__main__":
    main()
