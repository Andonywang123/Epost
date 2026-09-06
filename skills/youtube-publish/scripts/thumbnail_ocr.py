#!/usr/bin/env python3
"""Read Chinese thumbnail text with the local RapidOCR engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def has_han(text: str) -> bool:
    return any("\u3400" <= character <= "\u4dbf" or "\u4e00" <= character <= "\u9fff"
               for character in text)


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    image_path = Path(args.image).expanduser().resolve()
    if not image_path.is_file():
        fail("thumbnail image does not exist")
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        fail(f"RapidOCR is unavailable: {exc}")
    try:
        from PIL import Image
        with Image.open(image_path) as image:
            width, height = image.size
        raw, _ = RapidOCR()(str(image_path))
    except Exception as exc:
        fail(f"RapidOCR failed: {exc}")
    results = []
    for item in raw or []:
        try:
            box, text, confidence = item
            text = str(text).strip()
            points = [(float(point[0]), float(point[1])) for point in box]
        except (TypeError, ValueError, IndexError):
            continue
        if float(confidence) < 0.55 or not text or not has_han(text):
            continue
        x_values, y_values = zip(*points)
        results.append({
            "text": text,
            "x": max(0, min(1000, round(min(x_values) / width * 1000))),
            "y": max(0, min(1000, round(min(y_values) / height * 1000))),
            "width": max(1, min(1000, round((max(x_values) - min(x_values)) / width * 1000))),
            "height": max(1, min(1000, round((max(y_values) - min(y_values)) / height * 1000))),
        })
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
