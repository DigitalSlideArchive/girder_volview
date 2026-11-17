import importlib.util
from pathlib import Path

_SEED_PATH = Path(__file__).parents[1] / "e2e" / "seed" / "seed.py"
_SPEC = importlib.util.spec_from_file_location("e2e_seed", _SEED_PATH)
seed = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(seed)


def test_small_direct_upload_tier_contains_complete_ct_pet_studies():
    manifest = seed.read_manifest()

    picks = seed.small_tier_picks(manifest)
    modalities = {}
    for pick in picks:
        study = (pick["patient_slot"], pick["study_slot"])
        modalities.setdefault(study, set()).add(pick["modality_slot"])

    assert modalities == {
        ("patient-01", "study-01"): {"CT", "PET"},
        ("patient-02", "study-01"): {"CT", "PET"},
    }
