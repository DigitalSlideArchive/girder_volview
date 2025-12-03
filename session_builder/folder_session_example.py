# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "girder-client",
# ]
# ///
"""
Example: Generate a VolView session from a Girder folder with a rectangle annotation.
Rectangle is created in the MRI-PROSTATEx sample MRI scan:
https://data.kitware.com/api/v1/item/63527c7311dab8142820a338/download

Usage:
    uv run folder_session_example.py --api-url URL --api-key KEY --folder-id ID
"""

import argparse
from girder_client import GirderClient

from session_builder import generate_session


def make_session(api_url: str, api_key: str, folder_id: str):
    """Generate session from Girder folder with a rectangle annotation."""
    print("=== Generate session from Girder folder ===\n")

    gc = GirderClient(apiUrl=api_url)
    gc.authenticate(apiKey=api_key)
    print(f"Authenticated to {api_url}")

    folder = gc.getFolder(folder_id)
    print(f"Found folder: {folder['name']}")

    # Coordinates for MRI-PROSTATEx sample MRI scan:
    # https://data.kitware.com/api/v1/item/63527c7311dab8142820a338/download
    annotations = [
        {
            "type": "rectangle",
            "firstPoint": [-65.36087045590452, -15.919061788109012, 37.31865385684797],
            "secondPoint": [22.78165684736155, 47.65944636974224, 21.46675198654735],
            "slice": 9,
            "planeNormal": [
                1.4080733262381892e-17,
                0.24192188680171967,
                0.9702957272529602,
            ],
            "planeOrigin": [-117.91325380387, -75.35208187384475, 52.136969503946816],
            "label": "lesion",
            "color": "#ffff00",
            "metadata": {
                "source": "expert annotation",
                "confidence": "0.9",
            },
        },
    ]

    manifest, json_bytes = generate_session(
        gc,
        parent_id=folder_id,
        parent_type="folder",
        annotations=annotations,
        upload=True,
    )

    print(f"Generated session with {len(manifest['dataSources'])} data sources")
    print("Session uploaded to folder")

    return manifest


def main():
    parser = argparse.ArgumentParser(description="VolView session from folder example")
    parser.add_argument("--api-url", required=True, help="Girder API URL")
    parser.add_argument("--api-key", required=True, help="Girder API key")
    parser.add_argument(
        "--folder-id", required=True, help="Folder ID to generate session from"
    )
    args = parser.parse_args()

    make_session(args.api_url, args.api_key, args.folder_id)


if __name__ == "__main__":
    main()
