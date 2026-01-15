# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "girder-client",
# ]
# ///
"""
Example: Build a VolView session using the composable API.

This example demonstrates building a manifest step-by-step instead of using
the high-level generate_session() function. Each function returns a new
manifest copy, enabling a functional programming style.

Usage:
    uv run composable_example.py --api-url URL --api-key KEY --item-id ID
"""

import argparse
from girder_client import GirderClient

from session_builder import (
    create_manifest,
    add_dataset,
    add_annotation,
    get_item_files,
    serialize_manifest,
    upload_session,
)


def make_session(api_url: str, api_key: str, item_id: str):
    """Build session using the composable API."""
    print("=== Build session using composable API ===\n")

    gc = GirderClient(apiUrl=api_url)
    gc.authenticate(apiKey=api_key)
    print(f"Authenticated to {api_url}")

    item = gc.getItem(item_id)
    print(f"Found item: {item['name']}")

    # Get data sources from item
    data_sources = get_item_files(gc, item_id)
    print(f"Found {len(data_sources)} files")

    # Build manifest step by step
    dataset_id = "volume"
    manifest = create_manifest()
    manifest = add_dataset(manifest, data_sources, dataset_id)

    # Add annotation (coordinates for CT_Electrodes sample CT scan)
    manifest = add_annotation(
        manifest,
        {
            "type": "rectangle",
            "firstPoint": [281.8206054852409, -42.94960034417328, 477.2959518432617],
            "secondPoint": [334.10922362127144, -1.9831074603214844, 477.2959518432617],
            "slice": 80,
            "planeNormal": [0, 0, 1],
            "planeOrigin": [388.260009765625, 81.11995697021484, 477.2959518432617],
            "label": "Label 1",
            "color": "red",
        },
        dataset_id=dataset_id,
    )

    # Serialize and upload
    json_bytes = serialize_manifest(manifest)
    upload_session(gc, item_id, "item", json_bytes)

    print(f"Generated session with {len(manifest['dataSources'])} data sources")
    print("Session uploaded to item")

    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="VolView session composable API example"
    )
    parser.add_argument("--api-url", required=True, help="Girder API URL")
    parser.add_argument("--api-key", required=True, help="Girder API key")
    parser.add_argument(
        "--item-id", required=True, help="Item ID to generate session from"
    )
    args = parser.parse_args()

    make_session(args.api_url, args.api_key, args.item_id)


if __name__ == "__main__":
    main()
