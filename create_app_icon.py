#!/usr/bin/env python3
"""
create_app_icon.py — Generate ChatEKLD app icon from a source image.

Produces all required PNG sizes for macOS iconutil to convert into
an .icns file. Uses Pillow for processing.

Usage (called automatically by build_macos_app.sh):
    python3 create_app_icon.py <output_iconset_dir>
"""

import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Pillow is not installed. Install it with: pip install Pillow")
    sys.exit(1)

# macOS icon sizes: (filename_suffix, pixel_size)
ICON_SIZES = [
    ("icon_16x16",          16),
    ("icon_16x16@2x",       32),
    ("icon_32x32",          32),
    ("icon_32x32@2x",       64),
    ("icon_128x128",       128),
    ("icon_128x128@2x",    256),
    ("icon_256x256",       256),
    ("icon_256x256@2x",    512),
    ("icon_512x512",       512),
    ("icon_512x512@2x",   1024),
]

SOURCE_IMAGE_PATH = Path(__file__).parent / "static" / "img" / "daphne.png"

def process_icon(source_img: Image.Image, size: int) -> Image.Image:
    """Resize and square the source image for macOS icon requirements.

    Args:
        source_img (PIL.Image.Image): The source high-resolution image.
        size (int): Target pixel dimension.

    Returns:
        PIL.Image.Image: The processed RGBA icon at the requested size.
    """
    # Create a square version of the source image by adding transparency padding
    # rather than cropping, to ensure the whole character is visible.
    width, height = source_img.size
    max_dim = max(width, height)
    
    # Create transparent canvas
    square_img = Image.new("RGBA", (max_dim, max_dim), (0, 0, 0, 0))
    # Paste source image centered
    offset = ((max_dim - width) // 2, (max_dim - height) // 2)
    square_img.paste(source_img, offset)
    
    # Resize to target size with high-quality resampling
    return square_img.resize((size, size), Image.Resampling.LANCZOS)

def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <output_iconset_dir>")
        sys.exit(1)

    if not SOURCE_IMAGE_PATH.exists():
        print(f"Error: Source image not found at {SOURCE_IMAGE_PATH}")
        sys.exit(1)

    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        source_img = Image.open(SOURCE_IMAGE_PATH).convert("RGBA")
    except Exception as e:
        print(f"Error opening source image: {e}")
        sys.exit(1)

    for name, px in ICON_SIZES:
        icon = process_icon(source_img, px)
        dest = out_dir / f"{name}.png"
        icon.save(str(dest), "PNG")

    print(f"Generated {len(ICON_SIZES)} icon sizes in {out_dir}")

if __name__ == "__main__":
    main()
