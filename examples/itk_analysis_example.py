# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "girder-client",
#     "itk",
# ]
# ///
"""
Example: Download image from Girder, run ITK analysis, create session with annotations.

This example:
1. Downloads a NIfTI file from a Girder item
2. Thresholds to find the body (non-air region)
3. Extracts the body contour on the middle slice
4. Creates a polygon annotation tracing the body outline
5. Uploads session back to Girder

Usage:
    uv run itk_analysis_example.py --api-url URL --api-key KEY --item-id ID
"""

import argparse
import sys
import tempfile
from pathlib import Path

import itk
from girder_client import GirderClient

sys.path.insert(0, str(Path(__file__).parent.parent / "girder_volview"))

from volview_session import generate_session


def download_first_file(gc: GirderClient, item_id: str, dest_dir: Path) -> Path:
    """Download first file from item, return local path."""
    files = list(gc.listFile(item_id))
    if not files:
        raise ValueError(f"No files in item {item_id}")

    file_info = files[0]
    local_path = dest_dir / file_info["name"]
    gc.downloadFile(file_info["_id"], str(local_path))
    return local_path


def extract_body_contour(image_path: Path) -> list[dict]:
    """
    Extract body contour as a polygon annotation.

    1. Threshold to separate body from air
    2. Find largest connected component (the body)
    3. Extract contour on middle slice
    4. Return as polygon annotation
    """
    image = itk.imread(str(image_path), pixel_type=itk.SS)

    # Threshold to get body (above -500 HU for CT)
    threshold = itk.BinaryThresholdImageFilter.New(image)
    threshold.SetLowerThreshold(-500)
    threshold.SetInsideValue(1)
    threshold.SetOutsideValue(0)
    threshold.Update()
    binary = threshold.GetOutput()

    # Find connected components and keep the largest (the body)
    connected = itk.ConnectedComponentImageFilter.New(binary)
    connected.Update()
    labeled = connected.GetOutput()

    # Relabel by size, largest first
    relabel = itk.RelabelComponentImageFilter.New(labeled)
    relabel.Update()
    relabeled = relabel.GetOutput()

    # Keep only the largest component (label 1)
    body_threshold = itk.BinaryThresholdImageFilter.New(relabeled)
    body_threshold.SetLowerThreshold(1)
    body_threshold.SetUpperThreshold(1)
    body_threshold.SetInsideValue(1)
    body_threshold.SetOutsideValue(0)
    body_threshold.Update()
    body_mask = body_threshold.GetOutput()

    # Get middle slice
    size = list(image.GetLargestPossibleRegion().GetSize())
    middle_k = size[2] // 2

    # Get slice as numpy array and create 2D ITK image
    import numpy as np
    mask_array = itk.array_from_image(body_mask)
    slice_array = mask_array[middle_k, :, :].astype(np.int16)

    slice_2d = itk.image_from_array(slice_array)

    # Find contour using contour extractor
    contour_filter = itk.ContourExtractor2DImageFilter.New(slice_2d)
    contour_filter.SetContourValue(0.5)
    contour_filter.Update()

    if contour_filter.GetNumberOfOutputs() == 0:
        return []

    # Get the longest contour (should be the outer body boundary)
    longest_contour = None
    max_length = 0
    for i in range(contour_filter.GetNumberOfOutputs()):
        contour = contour_filter.GetOutput(i)
        vertex_list = contour.GetVertexList()
        num_points = vertex_list.Size()
        if num_points > max_length:
            max_length = num_points
            longest_contour = contour

    if longest_contour is None or max_length < 10:
        return []

    # Subsample to avoid too many points (take every Nth point)
    step = max(1, max_length // 100)

    direction = itk.array_from_matrix(image.GetDirection())
    plane_normal = direction[:, 2].tolist()

    # Get plane origin at middle slice
    origin_index = [0, 0, middle_k]
    plane_origin = list(image.TransformIndexToPhysicalPoint(origin_index))

    # Convert contour points to physical coordinates
    vertex_list = longest_contour.GetVertexList()
    points = []
    for j in range(0, max_length, step):
        vertex = vertex_list.GetElement(j)
        # vertex is continuous index (col, row), ITK index is (i, j, k)
        index_3d = [int(vertex[0]), int(vertex[1]), middle_k]
        physical_pt = list(image.TransformIndexToPhysicalPoint(index_3d))
        points.append(physical_pt)

    return [
        {
            "type": "polygon",
            "points": points,
            "slice": middle_k,
            "planeNormal": plane_normal,
            "planeOrigin": plane_origin,
            "label": "body_contour",
            "color": "#00ff00",
            "metadata": {
                "source": "itk_contour_extraction",
                "num_points": str(len(points)),
            },
        }
    ]


def make_session(api_url: str, api_key: str, item_id: str):
    """Download image, run ITK analysis, create annotated session."""
    print("=== ITK Analysis Session Example ===\n")

    gc = GirderClient(apiUrl=api_url)
    gc.authenticate(apiKey=api_key)
    print(f"Authenticated to {api_url}")

    item = gc.getItem(item_id)
    print(f"Found item: {item['name']}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        print("Downloading image...")
        image_path = download_first_file(gc, item_id, tmppath)
        print(f"Downloaded: {image_path.name}")

        print("Extracting body contour...")
        annotations = extract_body_contour(image_path)

        if annotations:
            print(f"Found contour with {annotations[0]['metadata']['num_points']} points")
        else:
            print("No contour found")

    manifest, zip_bytes = generate_session(
        gc,
        parent_id=item_id,
        parent_type="item",
        annotations=annotations,
        upload=True,
    )

    print(f"\nGenerated session with {len(annotations)} polygon annotation")
    print(f"Session uploaded to item ({len(zip_bytes)} bytes)")

    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="ITK analysis with VolView session generation"
    )
    parser.add_argument("--api-url", required=True, help="Girder API URL")
    parser.add_argument("--api-key", required=True, help="Girder API key")
    parser.add_argument("--item-id", required=True, help="Item ID with image file")
    args = parser.parse_args()

    make_session(args.api_url, args.api_key, args.item_id)


if __name__ == "__main__":
    main()
