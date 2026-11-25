"""
Image converter: any image format -> WebP or JPEG, in place.

Put files or whole folder trees inside the "img" folder next to this script,
set the options below, then run the script (F5 / Run button in VS Code, or
`python convert.py`). Every image is replaced by its converted version in the
same folder, so the directory structure stays exactly as it was.

Requires:  pip install pillow
Optional:  pip install pillow-heif   (for .heic / .heif photos from phones)
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from PIL import Image, ImageOps, ImageSequence
except ImportError:
    sys.exit("Pillow is not installed. Run:  pip install pillow")

try:  # optional HEIC/HEIF support
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

# ----------------------------- OPTIONS ---------------------------------
OUTPUT_FORMAT = "webp"   # "webp" or "jpeg"
QUALITY = 80             # 1-100 (higher = better quality, bigger file)
SCALE = 1.0              # 1.0 = same size, 0.5 = half width/height, 2.0 = double
INPUT_FOLDER = "img"     # folder next to this script

# Files that are ALREADY in the output format are skipped by default, so
# running the script twice never shrinks/recompresses the same image twice.
# Set to True to re-encode them too (e.g. to shrink a folder of .jpg to .jpg).
REPROCESS_SAME_FORMAT = False

JPEG_BACKGROUND = (255, 255, 255)  # colour put behind transparent areas for JPEG
WEBP_LOSSLESS = False              # True = lossless WebP (QUALITY then means effort)
# -----------------------------------------------------------------------

FORMATS = {
    "webp": (".webp", "WEBP", {".webp"}),
    "jpeg": (".jpg", "JPEG", {".jpg", ".jpeg", ".jpe", ".jfif"}),
    "jpg": (".jpg", "JPEG", {".jpg", ".jpeg", ".jpe", ".jfif"}),
}


def is_image(path: Path, known_exts: set) -> bool:
    return path.suffix.lower() in known_exts


def free_target(src: Path, ext: str) -> Path:
    """Target path beside src; avoids overwriting a different existing file
    (e.g. photo.png and photo.gif both wanting photo.webp)."""
    target = src.with_suffix(ext)
    n = 1
    while target.exists() and target != src:
        target = src.with_name(f"{src.stem}_{n}{ext}")
        n += 1
    return target


def resize(im: Image.Image, scale: float) -> Image.Image:
    if scale == 1.0:
        return im
    w = max(1, round(im.width * scale))
    h = max(1, round(im.height * scale))
    return im.resize((w, h), Image.LANCZOS)


def to_rgb(im: Image.Image, background) -> Image.Image:
    """Flatten any mode (incl. transparency) to RGB for JPEG."""
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        im = im.convert("RGBA")
        canvas = Image.new("RGB", im.size, background)
        canvas.paste(im, mask=im.getchannel("A"))
        return canvas
    return im.convert("RGB")


def to_webp_mode(im: Image.Image) -> Image.Image:
    if im.mode in ("RGB", "RGBA"):
        return im
    has_alpha = im.mode in ("LA", "PA") or "transparency" in im.info
    return im.convert("RGBA" if has_alpha else "RGB")


def convert_one(src: Path, fmt: str, quality: int, scale: float, lossless: bool) -> Path:
    ext, pil_format, _ = FORMATS[fmt]
    target = free_target(src, ext)
    tmp = target.with_name(target.name + ".tmp")

    try:
        with Image.open(src) as im:
            animated = getattr(im, "is_animated", False) and pil_format == "WEBP"
            icc = im.info.get("icc_profile")
            extra = {"icc_profile": icc} if icc else {}

            if animated:
                frames, durations = [], []
                for frame in ImageSequence.Iterator(im):
                    durations.append(frame.info.get("duration", 100))
                    frames.append(resize(frame.convert("RGBA"), scale))
                frames[0].save(
                    tmp, pil_format, save_all=True, append_images=frames[1:],
                    duration=durations, loop=im.info.get("loop", 0),
                    quality=quality, lossless=lossless, **extra,
                )
            else:
                im = ImageOps.exif_transpose(im)  # respect camera rotation
                if pil_format == "JPEG":
                    out = resize(to_rgb(im, JPEG_BACKGROUND), scale)
                    out.save(tmp, pil_format, quality=quality, optimize=True,
                             progressive=True, **extra)
                else:
                    out = resize(to_webp_mode(im), scale)
                    out.save(tmp, pil_format, quality=quality, lossless=lossless,
                             method=6, **extra)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise

    # Only now, after a successful save, touch the original.
    os.replace(tmp, target)
    if src != target:
        src.unlink()
    return target


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def main() -> None:
    p = argparse.ArgumentParser(description="Convert images in place to WebP/JPEG.")
    p.add_argument("-f", "--format", default=OUTPUT_FORMAT, choices=sorted(FORMATS))
    p.add_argument("-q", "--quality", type=int, default=QUALITY)
    p.add_argument("-s", "--scale", type=float, default=SCALE)
    p.add_argument("-i", "--input", default=INPUT_FOLDER)
    p.add_argument("--reprocess", action="store_true", default=REPROCESS_SAME_FORMAT,
                   help="also re-encode files already in the output format")
    args = p.parse_args()

    if not 1 <= args.quality <= 100:
        sys.exit("QUALITY must be between 1 and 100.")
    if args.scale <= 0:
        sys.exit("SCALE must be greater than 0.")

    root = Path(args.input)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent / root
    if not root.is_dir():
        root.mkdir(parents=True)
        sys.exit(f'Created "{root}". Put your images/folders in it and run again.')

    known_exts = set(Image.registered_extensions())
    same_exts = FORMATS[args.format][2]
    files = sorted(f for f in root.rglob("*") if f.is_file() and is_image(f, known_exts))
    if not files:
        sys.exit(f'No images found in "{root}".')

    print(f"{len(files)} image(s) found -> {args.format.upper()}, "
          f"quality {args.quality}, scale {args.scale}\n")

    done = skipped = failed = 0
    before = after = 0
    for f in files:
        rel = f.relative_to(root)
        if f.suffix.lower() in same_exts and not args.reprocess:
            skipped += 1
            continue
        size = f.stat().st_size
        try:
            out = convert_one(f, args.format, args.quality, args.scale, WEBP_LOSSLESS)
        except Exception as e:  # unreadable/corrupt file: leave it untouched
            failed += 1
            print(f"  FAILED  {rel}  ({e})")
            continue
        new_size = out.stat().st_size
        before += size
        after += new_size
        done += 1
        print(f"  OK      {rel} -> {out.name}  ({human(size)} -> {human(new_size)})")

    print(f"\nConverted {done}, skipped {skipped} (already {args.format}), failed {failed}.")
    if done:
        change = 100 * (after / before - 1) if before else 0
        word = "smaller" if change <= 0 else "larger"
        print(f"Total: {human(before)} -> {human(after)}  ({abs(change):.0f}% {word})")
    if skipped:
        print("Set REPROCESS_SAME_FORMAT = True to re-encode the skipped files as well.")


if __name__ == "__main__":
    main()
