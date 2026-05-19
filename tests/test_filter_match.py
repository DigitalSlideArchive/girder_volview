from girder_volview.utils import filterMatchesSession


def test_exact_match():
    assert filterMatchesSession(
        {"meta.dicom.StudyInstanceUID": "X"},
        {"meta.dicom.StudyInstanceUID": "X"},
    )


def test_row_with_extra_keys_does_not_match_coarser_session():
    # Row carries Patient+Study; session has only Study. Different dicts.
    assert not filterMatchesSession(
        {"meta.dicom.PatientID": "P", "meta.dicom.StudyInstanceUID": "X"},
        {"meta.dicom.StudyInstanceUID": "X"},
    )


def test_coarser_row_does_not_match_session_with_extra_keys():
    # Row has Study only; session has Patient+Study. Strict equality => no match.
    assert not filterMatchesSession(
        {"meta.dicom.StudyInstanceUID": "X"},
        {"meta.dicom.PatientID": "P", "meta.dicom.StudyInstanceUID": "X"},
    )


def test_session_missing_key():
    assert not filterMatchesSession(
        {"meta.dicom.StudyInstanceUID": "X"},
        {"meta.dicom.PatientID": "P"},
    )


def test_value_mismatch():
    assert not filterMatchesSession(
        {"meta.dicom.StudyInstanceUID": "X"},
        {"meta.dicom.StudyInstanceUID": "Y"},
    )


def test_empty_row_filter_does_not_match_non_empty_session():
    assert not filterMatchesSession({}, {"meta.dicom.StudyInstanceUID": "X"})


def test_empty_row_filter_matches_empty_session():
    assert filterMatchesSession({}, {})
    assert filterMatchesSession([], [])


def test_empty_list_row_does_not_match_non_empty_session():
    assert not filterMatchesSession([], {"k": "v"})
    assert not filterMatchesSession([], [{"k": "v"}])


def test_non_dict_inputs_do_not_match():
    assert not filterMatchesSession(None, {"k": "v"})
    assert not filterMatchesSession({"k": "v"}, None)
    assert not filterMatchesSession("k=v", {"k": "v"})


def test_list_row_matches_list_session_when_each_element_covered():
    assert filterMatchesSession(
        [{"meta.dicom.StudyInstanceUID": "A"}, {"meta.dicom.StudyInstanceUID": "B"}],
        [{"meta.dicom.StudyInstanceUID": "A"}, {"meta.dicom.StudyInstanceUID": "B"}],
    )


def test_list_row_no_match_when_session_missing_element():
    assert not filterMatchesSession(
        [{"meta.dicom.StudyInstanceUID": "A"}, {"meta.dicom.StudyInstanceUID": "B"}],
        [{"meta.dicom.StudyInstanceUID": "A"}],
    )


def test_list_row_session_with_extra_element_does_not_match():
    # Strict equality: extra elements on either side break the match.
    assert not filterMatchesSession(
        [{"meta.dicom.StudyInstanceUID": "A"}, {"meta.dicom.StudyInstanceUID": "B"}],
        [
            {"meta.dicom.StudyInstanceUID": "A"},
            {"meta.dicom.StudyInstanceUID": "B"},
            {"meta.dicom.StudyInstanceUID": "C"},
        ],
    )


def test_dict_row_does_not_match_multi_element_session():
    # Single-row open does not pull in a multi-filter session.
    assert not filterMatchesSession(
        {"meta.dicom.StudyInstanceUID": "A"},
        [{"meta.dicom.StudyInstanceUID": "A"}, {"meta.dicom.StudyInstanceUID": "B"}],
    )


def test_list_row_matches_dict_session_legacy():
    # Legacy single-filter session against a single-element row list.
    assert filterMatchesSession(
        [{"meta.dicom.StudyInstanceUID": "A"}],
        {"meta.dicom.StudyInstanceUID": "A"},
    )


def test_list_with_non_dict_element_rejected():
    assert not filterMatchesSession(
        [{"k": "v"}, "not a dict"],
        [{"k": "v"}],
    )


def test_order_independent():
    assert filterMatchesSession(
        [{"meta.dicom.StudyInstanceUID": "A"}, {"meta.dicom.StudyInstanceUID": "B"}],
        [{"meta.dicom.StudyInstanceUID": "B"}, {"meta.dicom.StudyInstanceUID": "A"}],
    )
