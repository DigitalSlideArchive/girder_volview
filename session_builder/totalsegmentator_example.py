# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "girder-client",
#     "TotalSegmentator",
#     "xmltodict",
# ]
# ///
"""
Example: Run TotalSegmentator on a CT image from Girder.

Downloads CT from Girder, runs TotalSegmentator to segment anatomical structures,
generates a VolView session with meaningful segment names (liver, spleen, etc.),
and uploads the session to Girder.

Usage:
    # Fast mode (3mm resolution, works on CPU)
    uv run totalsegmentator_example.py --api-url URL --api-key KEY --item-id ID --fast

    # Specific organs only
    uv run totalsegmentator_example.py --api-url URL --api-key KEY --item-id ID \
        --roi-subset liver spleen kidney_left kidney_right

    # Full resolution (requires GPU with ~4GB VRAM)
    uv run totalsegmentator_example.py --api-url URL --api-key KEY --folder-id ID
"""

import argparse
import tempfile
from pathlib import Path

import numpy as np
from girder_client import GirderClient
from totalsegmentator.python_api import totalsegmentator
from totalsegmentator.nifti_ext_header import load_multilabel_nifti

from session_builder import (
    generate_session,
    upload_labelmap,
    download_folder_files,
    LabelMapInput,
)


def download_item_files(
    gc: GirderClient, item_id: str, dest_dir: Path
) -> tuple[Path, str]:
    """Download first file from item, return local path and original name."""
    files = list(gc.listFile(item_id))
    if not files:
        raise ValueError(f"No files in item {item_id}")

    file_info = files[0]
    local_path = dest_dir / file_info["name"]
    gc.downloadFile(file_info["_id"], str(local_path))
    return local_path, file_info["name"]


def get_base_name(filename: str) -> str:
    """Strip extensions to get base name."""
    name = filename
    for ext in [".nii.gz", ".nii", ".nrrd", ".mha", ".mhd", ".dcm"]:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    return name


def run_segmentation(
    input_path: Path,
    output_path: Path,
    fast: bool = False,
    roi_subset: list[str] | None = None,
) -> Path:
    """Run TotalSegmentator and save multi-label segmentation."""
    import nibabel as nib

    seg_img = totalsegmentator(
        input=input_path,
        output=None,
        fast=fast,
        roi_subset=roi_subset,
        ml=True,
    )
    nib.save(seg_img, output_path)
    return output_path


def extract_label_names(seg_path: Path) -> dict[int, str]:
    """Extract label names from TotalSegmentator NIfTI extended header.

    Only returns labels for values actually present in the image.
    """

    seg_img, label_map = load_multilabel_nifti(str(seg_path))

    data = np.asarray(seg_img.dataobj)
    present_values = set(np.unique(data)) - {0}  # exclude background

    return {v: name for v, name in label_map.items() if v in present_values}


def read_file_bytes(path: Path) -> bytes:
    """Read file as bytes."""
    return path.read_bytes()


def segment_and_upload(
    api_url: str,
    api_key: str,
    item_id: str | None = None,
    folder_id: str | None = None,
    fast: bool = False,
    roi_subset: list[str] | None = None,
):
    """Download image, run TotalSegmentator, generate session with named segments."""
    print("=== TotalSegmentator Session Example ===\n")

    gc = GirderClient(apiUrl=api_url)
    gc.authenticate(apiKey=api_key)
    print(f"Authenticated to {api_url}")

    if item_id:
        item = gc.getItem(item_id)
        print(f"Found item: {item['name']}")
        parent_id = item_id
        parent_type = "item"
    else:
        folder = gc.getFolder(folder_id)
        print(f"Found folder: {folder['name']}")
        parent_id = folder_id
        parent_type = "folder"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        print("Downloading image...")
        if item_id:
            image_path, original_name = download_item_files(gc, item_id, tmppath)
            base_name = get_base_name(original_name)
            print(f"Downloaded: {original_name}")
        else:
            downloaded = download_folder_files(
                gc, folder_id, tmppath, extra_exclude=(".seg.nii.gz",)
            )
            if len(downloaded) == 1:
                image_path = downloaded[0]
                base_name = get_base_name(image_path.name)
                print(f"Downloaded: {image_path.name}")
            else:
                image_path = downloaded[0].parent  # files_dir for DICOM series
                base_name = folder["name"]  # folder fetched earlier
                print(f"Downloaded {len(downloaded)} files (DICOM series)")

        seg_path = tmppath / f"{base_name}-total.seg.nii.gz"

        print(f"\nRunning TotalSegmentator (fast={fast})...")
        if roi_subset:
            print(f"Segmenting: {', '.join(roi_subset)}")
        else:
            print("Segmenting all structures (~104 classes)")

        run_segmentation(image_path, seg_path, fast, roi_subset)
        print(f"Segmentation complete: {seg_path.name}")

        print("\nExtracting segment names from NIfTI header...")
        label_names = extract_label_names(seg_path)
        print(
            f"Found {len(label_names)} segments: {', '.join(list(label_names.values())[:5])}..."
        )

        seg_bytes = read_file_bytes(seg_path)
        print(f"Segmentation size: {len(seg_bytes) / 1024 / 1024:.1f} MB")

        print("\nUploading segmentation file to Girder...")
        seg_url = upload_labelmap(
            gc,
            labelmap_bytes=seg_bytes,
            filename=seg_path.name,
            parent_id=parent_id,
            parent_type=parent_type,
        )
        print(f"Uploaded: {seg_path.name}")

        print("\nGenerating VolView session with segment metadata...")
        labelmap: LabelMapInput = {
            "url": seg_url,
            "name": f"TotalSegmentator ({base_name})",
            "label_names": label_names,
        }

        manifest, json_bytes = generate_session(
            gc,
            parent_id=parent_id,
            parent_type=parent_type,
            labelmaps=[labelmap],
            upload=True,
        )

        print(f"\nSession uploaded ({len(json_bytes)} bytes)")
        print(f"Contains {len(label_names)} named segments")


def main():
    parser = argparse.ArgumentParser(
        description="Run TotalSegmentator on Girder CT images"
    )
    parser.add_argument("--api-url", required=True, help="Girder API URL")
    parser.add_argument("--api-key", required=True, help="Girder API key")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--item-id", help="Item ID with CT file (NIfTI, NRRD, etc.)")
    group.add_argument("--folder-id", help="Folder ID with DICOM series")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use fast mode (3mm resolution, better for CPU)",
    )
    parser.add_argument(
        "--roi-subset",
        nargs="+",
        help="Segment only specific structures (e.g., liver spleen kidney_left)",
    )
    args = parser.parse_args()

    segment_and_upload(
        args.api_url,
        args.api_key,
        args.item_id,
        args.folder_id,
        args.fast,
        args.roi_subset,
    )


if __name__ == "__main__":
    main()
