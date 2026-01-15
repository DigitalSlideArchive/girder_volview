# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "girder-client",
# ]
# ///
"""
Example: Generate a VolView session from a Girder folder with a rectangle annotation.
Rectangle is created in the CT_Electrodes sample CT scan:
https://raw.githubusercontent.com/neurolabusc/niivue-images/main/CT_Electrodes.nii.gz

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

    # Coordinates for CT_Electrodes sample CT scan:
    # https://raw.githubusercontent.com/neurolabusc/niivue-images/main/CT_Electrodes.nii.gz
    annotations = [
        {
            "type": "rectangle",
            "firstPoint": [281.8206054852409, -42.94960034417328, 477.2959518432617],
            "secondPoint": [334.10922362127144, -1.9831074603214844, 477.2959518432617],
            "slice": 80,
            "planeNormal": [0, 0, 1],
            "planeOrigin": [388.260009765625, 81.11995697021484, 477.2959518432617],
            "label": "lesion",
            "color": "#ff6b6b",
            "metadata": {
                "model": "LesionDetector-v2",
                "confidence": "0.87",
                "detection_threshold": "0.5",
                "inference_time_ms": "234",
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
