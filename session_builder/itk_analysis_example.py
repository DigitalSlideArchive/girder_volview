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
1. Downloads image file(s) from a Girder item or folder
2. Thresholds to find the body (non-air region)
3. Extracts the body contour on the middle slice (3D) or entire image (2D)
4. Creates a polygon annotation tracing the body outline
5. Uploads session back to Girder

Supported formats: DICOM series, NIfTI (.nii, .nii.gz), NRRD, MHA/MHD, PNG, JPEG, TIFF, etc.

Usage:
    # For a single file item (NIfTI, PNG, etc.)
    uv run itk_analysis_example.py --api-url URL --api-key KEY --item-id ID

    # For a folder of DICOM files
    uv run itk_analysis_example.py --api-url URL --api-key KEY --folder-id ID
"""

import argparse
import tempfile
from pathlib import Path
import numpy as np

import itk
from girder_client import GirderClient

from session_builder import generate_session, download_folder_files


def download_item_files(gc: GirderClient, item_id: str, dest_dir: Path) -> Path:
    """Download first file from item, return local path."""
    files = list(gc.listFile(item_id))
    if not files:
        raise ValueError(f"No files in item {item_id}")

    file_info = files[0]
    local_path = dest_dir / file_info["name"]
    gc.downloadFile(file_info["_id"], str(local_path))
    return local_path


def read_image_as_3d(image_path: Path):
    """Read image file or DICOM folder as a 3D ITK image.

    Handles:
    - Single 3D files (NIfTI, NRRD, MHA, etc.)
    - Single 2D files (PNG, JPEG, TIFF) - converts to 3D with single slice
    - DICOM directories - reads as 3D series

    Returns ITK image with pixel type SS (signed short).
    """

    if image_path.is_dir():
        # DICOM series
        names_generator = itk.GDCMSeriesFileNames.New()
        names_generator.SetDirectory(str(image_path))
        file_names = names_generator.GetInputFileNames()
        if not file_names:
            raise ValueError(f"No DICOM files found in {image_path}")
        reader = itk.ImageSeriesReader.New(FileNames=file_names)
        reader.Update()
        return itk.cast_image_filter(
            reader.GetOutput(), ttype=[type(reader.GetOutput()), itk.Image[itk.SS, 3]]
        )

    image = itk.imread(str(image_path), pixel_type=itk.SS)
    dimension = image.GetImageDimension()

    if dimension == 3:
        return image

    # Convert 2D to 3D with single slice
    array_2d = itk.array_from_image(image)
    array_3d = array_2d[np.newaxis, :, :]

    image_3d = itk.image_from_array(array_3d.astype(np.int16))

    # Preserve spacing and origin
    spacing_2d = list(image.GetSpacing())
    origin_2d = list(image.GetOrigin())
    image_3d.SetSpacing([spacing_2d[0], spacing_2d[1], 1.0])
    image_3d.SetOrigin([origin_2d[0], origin_2d[1], 0.0])

    return image_3d


def extract_body_contour(image) -> list[dict]:
    """
    Extract body contour as a polygon annotation from a 3D ITK image.

    1. Threshold to separate body from air
    2. Find largest connected component (the body)
    3. Extract contour on middle slice
    4. Return as polygon annotation
    """
    import numpy as np

    size = list(image.GetLargestPossibleRegion().GetSize())

    # Otsu threshold to separate foreground from background
    otsu = itk.OtsuThresholdImageFilter.New(image)
    otsu.SetInsideValue(1)
    otsu.SetOutsideValue(0)
    otsu.Update()
    binary = otsu.GetOutput()

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

    mask_array = itk.array_from_image(body_mask)
    middle_k = size[2] // 2
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

    # Convert contour points to physical coordinates
    vertex_list = longest_contour.GetVertexList()
    points = []

    direction = itk.array_from_matrix(image.GetDirection())
    plane_normal = direction[:, 2].tolist()
    origin_index = [0, 0, middle_k]
    plane_origin = list(image.TransformIndexToPhysicalPoint(origin_index))

    for j in range(0, max_length, step):
        vertex = vertex_list.GetElement(j)
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


def make_session(
    api_url: str, api_key: str, item_id: str | None = None, folder_id: str | None = None
):
    """Download image, run ITK analysis, create annotated session."""
    print("=== ITK Analysis Session Example ===\n")

    gc = GirderClient(apiUrl=api_url)
    gc.authenticate(apiKey=api_key)
    print(f"Authenticated to {api_url}")

    if item_id:
        parent_id = item_id
        parent_type = "item"
        item = gc.getItem(item_id)
        print(f"Found item: {item['name']}")
    else:
        parent_id = folder_id
        parent_type = "folder"
        folder = gc.getFolder(folder_id)
        print(f"Found folder: {folder['name']}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        print("Downloading image...")
        if item_id:
            image_path = download_item_files(gc, item_id, tmppath)
            print(f"Downloaded: {image_path.name}")
        else:
            downloaded = download_folder_files(gc, folder_id, tmppath)
            if len(downloaded) == 1:
                image_path = downloaded[0]
                print(f"Downloaded: {image_path.name}")
            else:
                image_path = downloaded[0].parent  # files_dir for DICOM series
                print(f"Downloaded {len(downloaded)} files to {image_path.name}/")

        print("Reading image...")
        image = read_image_as_3d(image_path)

        print("Extracting body contour...")
        annotations = extract_body_contour(image)

        if annotations:
            print(
                f"Found contour with {annotations[0]['metadata']['num_points']} points"
            )
        else:
            print("No contour found")

    manifest, json_bytes = generate_session(
        gc,
        parent_id=parent_id,
        parent_type=parent_type,
        annotations=annotations,
        upload=True,
    )

    print(f"\nGenerated session with {len(annotations)} polygon annotation")
    print(f"Session has {len(manifest['dataSources'])} data sources")
    if annotations:
        polygon_tool = manifest["tools"]["polygons"]["tools"][0]
        print(f"Polygon imageID: {polygon_tool['imageID']}")
    print(f"Session uploaded to {parent_type}")

    return manifest


def main():
    parser = argparse.ArgumentParser(
        description="ITK analysis with VolView session generation"
    )
    parser.add_argument("--api-url", required=True, help="Girder API URL")
    parser.add_argument("--api-key", required=True, help="Girder API key")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--item-id", help="Item ID with image file")
    group.add_argument("--folder-id", help="Folder ID with DICOM files")
    args = parser.parse_args()

    make_session(args.api_url, args.api_key, args.item_id, args.folder_id)


if __name__ == "__main__":
    main()
