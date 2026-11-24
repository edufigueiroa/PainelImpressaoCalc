#!/usr/bin/python3
"""Gera os ícones e empacota a extensão em dist/PainelImpressaoCalc.oxt."""
import pathlib
import zipfile

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parent
SOURCE = ROOT / "extension"
OUTPUT = ROOT / "dist" / "PainelImpressaoCalc.oxt"
INK = (37, 99, 235, 255)
PAPER = (255, 255, 255, 255)
LINE = (70, 78, 92, 255)


def draw_icon(kind, size):
    scale = 8
    side = size * scale
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    u = side / 16
    draw.rectangle((3 * u, 1 * u, 13 * u, 15 * u), fill=PAPER, outline=LINE, width=max(1, round(u * .8)))
    if kind == "largura":
        y = 8 * u
        draw.line((1 * u, y, 15 * u, y), fill=INK, width=round(u * 1.3))
        draw.polygon(((0, y), (3.2 * u, y - 2.4 * u), (3.2 * u, y + 2.4 * u)), fill=INK)
        draw.polygon(((16 * u, y), (12.8 * u, y - 2.4 * u), (12.8 * u, y + 2.4 * u)), fill=INK)
    else:
        for row in (4, 7, 10):
            draw.line((5 * u, row * u, 11 * u, row * u), fill=LINE, width=round(u * .7))
        draw.ellipse((7 * u, 7 * u, 14 * u, 14 * u), outline=INK, width=round(u * 1.2))
        draw.line((12.8 * u, 12.8 * u, 15.6 * u, 15.6 * u), fill=INK, width=round(u * 1.4))
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main():
    icons = SOURCE / "icons"
    icons.mkdir(exist_ok=True)
    for kind in ("painel", "largura"):
        for size in (16, 26):
            draw_icon(kind, size).save(icons / f"{kind}_{size}.png")
    bundled = SOURCE / "bin"
    bundled.mkdir(exist_ok=True)
    (bundled / "panel.py").write_bytes((ROOT / "panel.py").read_bytes())
    OUTPUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(SOURCE).as_posix())
    print(OUTPUT)


if __name__ == "__main__":
    main()
