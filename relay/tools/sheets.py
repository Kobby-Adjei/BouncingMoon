"""Contact sheets: the screens of each variant side by side, a few per image, for a quick look at them all."""
import glob
import os
import sys

from PIL import Image

out = sys.argv[1]
for variant, per_row, scale in (("phone-light", 4, 1.0), ("phone-dark", 4, 1.0), ("desktop", 2, 0.5)):
    shots = sorted(glob.glob(os.path.join(out, f"{variant}--*.png")))
    for n in range(0, len(shots), per_row):
        group = [Image.open(p) for p in shots[n:n + per_row]]
        group = [im.resize((int(im.width * scale), int(im.height * scale))) for im in group]
        w, h = sum(im.width for im in group) + 12 * (len(group) - 1), max(im.height for im in group)
        sheet = Image.new("RGB", (w, h), (128, 128, 128))
        x = 0
        for im in group:
            sheet.paste(im, (x, 0))
            x += im.width + 12
        sheet.save(os.path.join(out, f"sheet-{variant}-{n // per_row + 1:02d}.png"))
