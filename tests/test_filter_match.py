from girder_volview.utils import filterMatchesSession


def test_exact_match():
    assert filterMatchesSession(
        {"meta.dicom.StudyInstanceUID": "X"},
        {"meta.dicom.StudyInstanceUID": "X"},
    )


def test_row_with_extra_keys_does_not_match_coarser_session():
    assert not filterMatchesSession(
        {"meta.dicom.PatientID": "P", "meta.dicom.StudyInstanceUID": "X"},
        {"meta.dicom.StudyInstanceUID": "X"},
    )


def test_coarser_row_does_not_match_session_with_extra_keys():
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
    assert not filterMatchesSession(
        [{"meta.dicom.StudyInstanceUID": "A"}, {"meta.dicom.StudyInstanceUID": "B"}],
        [
            {"meta.dicom.StudyInstanceUID": "A"},
            {"meta.dicom.StudyInstanceUID": "B"},
            {"meta.dicom.StudyInstanceUID": "C"},
        ],
    )


def test_dict_row_does_not_match_multi_element_session():
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


def test_mixed_type_values_compare_without_error():
    # dicom.py coerces numeric DICOM values to int, so an int/str mix under one
    # key is real; canonical ordering must not raise TypeError (raw tuple
    # sorting would compare 3 < "3A").
    assert filterMatchesSession(
        [{"meta.dicom.SeriesNumber": 3}, {"meta.dicom.SeriesNumber": "3A"}],
        [{"meta.dicom.SeriesNumber": "3A"}, {"meta.dicom.SeriesNumber": 3}],
    )
    assert not filterMatchesSession(
        [{"meta.dicom.SeriesNumber": 3}, {"meta.dicom.SeriesNumber": "3A"}],
        [{"meta.dicom.SeriesNumber": "3A"}],
    )


def test_operator_dict_values_compare_without_error():
    # Mongo-operator (nested dict) values are unorderable as raw tuples too.
    assert filterMatchesSession(
        [{"a": {"$in": [1, 2]}}, {"a": 1}],
        [{"a": 1}, {"a": {"$in": [1, 2]}}],
    )
