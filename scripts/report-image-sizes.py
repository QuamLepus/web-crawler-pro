#!/usr/bin/env python3
"""Report local Docker image virtual sizes for the supplied image references.

Docker's ``Size`` is the sum of uncompressed image-layer sizes. It is useful
for repeatable comparisons, but is not the compressed registry download size
and shared layers may be counted for each image.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence


def format_image_size(image_ref: str, size_bytes: int) -> str:
    """Format one Docker image's reported virtual size as bytes and MiB."""
    if size_bytes < 0:
        raise ValueError("image size must not be negative")
    mib = size_bytes / (1024 * 1024)
    return f"{image_ref}\t{size_bytes}\t{mib:.2f} MiB"


def inspect_image(image_ref: str) -> int:
    """Return Docker's virtual size for an image reference."""
    result = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        check=True,
        capture_output=True,
        text=True,
    )
    images = json.loads(result.stdout)
    if not images or "Size" not in images[0]:
        raise ValueError(f"Docker inspect returned no image size for {image_ref}")
    return int(images[0]["Size"])


def main(image_refs: Sequence[str]) -> int:
    if not image_refs:
        print(f"Usage: {sys.argv[0]} IMAGE_REF [IMAGE_REF ...]", file=sys.stderr)
        return 2
    print("image\tvirtual_size_bytes\tvirtual_size")
    try:
        for image_ref in image_refs:
            print(format_image_size(image_ref, inspect_image(image_ref)))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as exc:
        print(f"image size report failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
