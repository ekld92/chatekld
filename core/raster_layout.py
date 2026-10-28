"""Conservative ruled-scan segmentation, without recognition or pixel repair.

All page pixels belong to one rectangle. Rules suggest regions, never prove
cell semantics. Unruled pages remain a single full-page OCR call.
"""
from __future__ import annotations

import io


def _runs(values):
    import numpy as np
    edges = np.diff(np.r_[False, values, False].astype('int8'))
    return list(zip(np.where(edges == 1)[0], np.where(edges == -1)[0], strict=True))


def regions(png: bytes) -> list[dict]:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(png)) as original:
        width, height = original.size
        if width * height > 20_000_000:
            raise ValueError('Raster exceeds page pixel bound')
        small = original.convert('L')
        small.thumbnail((900, 1400))
        dark = np.asarray(small) < 210
        sh, sw = dark.shape
        dilated = dark.copy()
        for offset in range(1, 5):
            dilated[offset:] |= dark[:-offset]
            dilated[:-offset] |= dark[offset:]
        rows = np.array([any(end - start > sw * .3 for start, end in _runs(row))
                         for row in dilated])
        ys = [int((start + end - 1) / 2) for start, end in _runs(rows)]
        if len(ys) < 4:
            return []
        # Exact boundaries are shared by neighbours; no crop is discarded.
        bounds = [0] + [round(y * height / sh) for y in ys] + [height]
        result = []
        for index, (top, bottom) in enumerate(zip(bounds, bounds[1:])):
            xs = [0, width]
            if 0 < index < len(bounds) - 2:
                first, last = ys[index - 1] + 5, ys[index] - 5
                if last - first > 10:
                    band = dark[first:last].copy()
                    for offset in range(1, 5):
                        band[:, offset:] |= dark[first:last, :-offset]
                        band[:, :-offset] |= dark[first:last, offset:]
                    columns = np.array([any(end - start > (last - first) * .7
                                            for start, end in _runs(column))
                                        for column in band.T])
                    xs = [0] + [round((start + end - 1) / 2 * width / sw)
                                for start, end in _runs(columns)] + [width]
            xs = sorted(set(xs))
            for left, right in zip(xs, xs[1:]):
                if right <= left or bottom <= top:
                    continue
                result.append({'bbox_pixels': [left, top, right, bottom],
                               'band': index, 'psm': 3 if index in (0, len(bounds) - 2) else 6})
        if len(result) > 48:
            return []  # bounded fallback: full page, still explicitly unqualified
        return result


def crop(png: bytes, box: list[int]) -> bytes:
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(png)) as image:
        # White padding aids isolated labels; original pixels stay unchanged.
        region = ImageOps.expand(image.crop((box[0], box[1], box[2], box[3])), border=12, fill='white')
        output = io.BytesIO()
        region.save(output, format='PNG')
        return output.getvalue()
