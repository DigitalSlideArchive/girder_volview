# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "girder-client",
# ]
# ///
"""
Example: Using volview_session.py Python API to generate VolView sessions.

Usage:
    uv run python_api_example.py --api-url URL --api-key KEY --item-id ID
"""

import argparse
import sys
from pathlib import Path
from girder_client import GirderClient

sys.path.insert(0, str(Path(__file__).parent.parent / "girder_volview"))

from volview_session import (
    generate_session,
)


def make_session(api_url: str, api_key: str, item_id: str):
    """Generate session from Girder item with annotations."""
    print("=== Generate session from Girder item ===\n")

    gc = GirderClient(apiUrl=api_url)
    gc.authenticate(apiKey=api_key)
    print(f"Authenticated to {api_url}")

    item = gc.getItem(item_id)
    print(f"Found item: {item['name']}")

    annotations = [
        {
            "type": "rectangle",
            "imageId": "0",
            "firstPoint": [100, 100, 0],
            "secondPoint": [200, 200, 0],
            "slice": 50,
            "label": "finding",
            "color": "#ff6600",
            "metadata": {"notes": "Suspicious region"},
        },
        {
            "type": "ruler",
            "imageId": "0",
            "firstPoint": [50, 50, 0],
            "secondPoint": [150, 50, 0],
            "slice": 50,
            "label": "diameter",
        },
    ]

    manifest, zip_bytes = generate_session(
        gc,
        parent_id=item_id,
        parent_type="item",
        annotations=annotations,
        upload=True,
    )

    print(f"Generated session with {len(manifest['dataSources'])} data sources")
    print(f"Session uploaded to item ({len(zip_bytes)} bytes)")

    return manifest


def main():
    parser = argparse.ArgumentParser(description="VolView session Python API examples")
    parser.add_argument("--api-url", required=True, help="Girder API URL")
    parser.add_argument("--api-key", required=True, help="Girder API key")
    parser.add_argument(
        "--item-id", required=True, help="Item ID to generate session from"
    )
    args = parser.parse_args()

    make_session(args.api_url, args.api_key, args.item_id)


if __name__ == "__main__":
    main()
