from girder_volview.utils import sessionNameFromFilter


ZIP = ".volview.zip"


def test_empty_filter_uses_bare_session_name():
    assert sessionNameFromFilter(None, ZIP) == "session.volview.zip"
    assert sessionNameFromFilter({}, ZIP) == "session.volview.zip"


def test_single_key_filter():
    name = sessionNameFromFilter({"meta.dicom.PatientID": "P"}, ZIP)
    assert name == "session.P.volview.zip"


def test_multi_key_filter_is_order_independent():
    patient_first = sessionNameFromFilter(
        {
            "meta.dicom.PatientID": "P",
            "meta.dicom.StudyInstanceUID": "S",
        },
        ZIP,
    )
    study_first = sessionNameFromFilter(
        {
            "meta.dicom.StudyInstanceUID": "S",
            "meta.dicom.PatientID": "P",
        },
        ZIP,
    )
    assert patient_first == study_first == "session.P.S.volview.zip"


def test_series_uid_canonical_position():
    name = sessionNameFromFilter(
        {
            "meta.dicom.SeriesInstanceUID": "R",
            "meta.dicom.PatientID": "P",
            "meta.dicom.StudyInstanceUID": "S",
        },
        ZIP,
    )
    assert name == "session.P.S.R.volview.zip"


def test_unknown_key_falls_after_preferred_and_sorts():
    name = sessionNameFromFilter(
        {
            "meta.dicom.StudyInstanceUID": "S",
            "foo": "F",
        },
        ZIP,
    )
    assert name == "session.S.F.volview.zip"


def test_values_are_sanitized():
    name = sessionNameFromFilter(
        {"meta.dicom.PatientID": "1.2/3 4"},
        ZIP,
    )
    assert name == "session.1.2_3_4.volview.zip"


def test_list_filter_two_studies():
    name = sessionNameFromFilter(
        [
            {"meta.dicom.StudyInstanceUID": "A"},
            {"meta.dicom.StudyInstanceUID": "B"},
        ],
        ZIP,
    )
    assert name == "session.A.B.volview.zip"


def test_list_filter_dedupes_repeated_values():
    name = sessionNameFromFilter(
        [
            {"meta.dicom.PatientID": "P", "meta.dicom.StudyInstanceUID": "A"},
            {"meta.dicom.PatientID": "P", "meta.dicom.StudyInstanceUID": "B"},
        ],
        ZIP,
    )
    assert name == "session.P.A.B.volview.zip"


def test_empty_list_filter_uses_bare_session_name():
    assert sessionNameFromFilter([], ZIP) == "session.volview.zip"


def test_single_element_list_matches_dict_form():
    list_form = sessionNameFromFilter(
        [{"meta.dicom.StudyInstanceUID": "S"}],
        ZIP,
    )
    dict_form = sessionNameFromFilter(
        {"meta.dicom.StudyInstanceUID": "S"},
        ZIP,
    )
    assert list_form == dict_form == "session.S.volview.zip"
