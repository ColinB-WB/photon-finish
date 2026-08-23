# tools/make_fixture.py
"""Create a small committed test fixture from a real Seestar stacked FITS
(spec 2026-07-13 §6): a center crop with the ORIGINAL header kept verbatim,
except NAXIS1/2 (astropy syncs) and CRPIX1/2 (shifted by the crop offset so
the WCS stays self-consistent). 256x256 keeps each file ~384 KB, under the
pre-commit 500 KB cap.

Usage: python tools/make_fixture.py <src.fit> <dest.fit> [--size 256]
"""

from __future__ import annotations

import argparse

from astropy.io import fits


def make_fixture(src: str, dest: str, size: int = 256) -> None:
    with fits.open(src) as hdul:
        data = hdul[0].data  # (3, NAXIS2, NAXIS1) uint16 (astropy applies BZERO)
        header = hdul[0].header.copy()
    _, h, w = data.shape
    y0, x0 = (h - size) // 2, (w - size) // 2
    crop = data[:, y0 : y0 + size, x0 : x0 + size].copy()
    if "CRPIX1" in header:
        header["CRPIX1"] = header["CRPIX1"] - x0  # CRPIX1 is the NAXIS1 (x) axis
    if "CRPIX2" in header:
        header["CRPIX2"] = header["CRPIX2"] - y0
    fits.PrimaryHDU(data=crop, header=header).writeto(dest, overwrite=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src")
    ap.add_argument("dest")
    ap.add_argument("--size", type=int, default=256)
    a = ap.parse_args()
    make_fixture(a.src, a.dest, a.size)

    # Verify: identity check + measurable, printed for the operator.
    from seestar.inspect import is_seestar_stack, measure, read_header

    hdr = read_header(a.dest)
    ok, reason = is_seestar_stack(hdr)
    stats = measure(a.dest)
    print(f"{a.dest}: is_seestar_stack={ok} ({reason}), CREATOR={hdr.get('CREATOR')}")
    print(f"  background={stats['background']}, bgnoise={stats['bgnoise']}, max={stats['max']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
