import datetime
import sys

import pydicom
import pydicom.valuerep
import pydicom.multival
import pydicom.sequence

from girder import events
from girder.models.item import Item
from girder.models.file import File
from girder.exceptions import GirderException

MAX_TAG_SIZE = 1024 * 128  # bytes


def setupEventHandlers():
    events.bind("model.file.save.after", "girder_volview", handleFileSave)


def handleFileSave(event):
    return addDicomTagsToItem(event.info)


# Code modified from https://github.com/girder/girder/blob/master/plugins/dicom_viewer/girder_dicom_viewer/__init__.py
def addDicomTagsToItem(file):
    """
    Add DICOM tags to Item metadata.
    """
    itemId = file["itemId"]
    if not itemId:
        return
    dicomMetadata = _parseFile(file)
    if dicomMetadata is None:
        return  # not a dicom file
    itemMeta = {"dicom": dicomMetadata}
    item = Item().load(itemId, force=True)
    Item().setMetadata(item, itemMeta)


def _coerceValue(value):
    # Handle lists (MultiValue) recursively
    if isinstance(value, pydicom.multival.MultiValue):
        if isinstance(value, pydicom.sequence.Sequence):
            # A pydicom Sequence is a nested list of Datasets, which is too complicated to flatten
            # now
            raise ValueError("Cannot coerce a Sequence")
        return list(map(_coerceValue, value))

    # pydicom does not treat the PersonName type as a subclass of a text type
    if isinstance(value, pydicom.valuerep.PersonName):
        return str(value)

    # Many pydicom value types are subclasses of base types; to ensure the value can be serialized
    # to MongoDB, cast the value back to its base type
    for knownBaseType in {
        datetime.datetime,
        datetime.date,
        datetime.time,
        int,
        float,
        str,
    }:
        if isinstance(value, knownBaseType):
            return knownBaseType(value)

    if isinstance(value, bytes):
        if b"\x00" in value:
            raise ValueError("Binary data with null")
        # For binary data, see if it can be coerced further into utf8 data.  If
        # not, mongo won't store it, so don't accept it here.
        try:
            value.decode()
            return value
        except UnicodeDecodeError:
            raise ValueError("Binary data that cannot be stored as utf-8")

    raise ValueError("Unknown type", type(value))


def _coerceMetadata(dataset):
    metadata = {}

    # Use simple iteration instead of "dataset.iterall", to prevent recursing into Sequences, which
    # are too complicated to flatten now
    # The dataset iterator is
    #   for tag in sorted(dataset.keys()):
    #       yield dataset[tag]
    # but we want to ignore certain exceptions of delayed data loading, so
    # we iterate through the dataset ourselves.
    for tag in dataset.keys():
        try:
            dataElement = dataset[tag]
        except OSError:
            continue
        if dataElement.tag.element == 0:
            # Skip Group Length tags, which are always element 0x0000
            continue

        # Use "keyword" instead of "name", as the keyword is a simpler and more uniform string
        # See: http://dicom.nema.org/medical/dicom/current/output/html/part06.html#table_6-1
        # For unknown / private tags, allow pydicom to create a string representation like
        # "(0013, 1010)"
        tagKey = (
            dataElement.keyword
            if dataElement.keyword and not dataElement.tag.is_private
            else str(dataElement.tag)
        )

        try:
            tagValue = _coerceValue(dataElement.value)
        except ValueError:
            # Omit tags where the value cannot be coerced to JSON-encodable types
            continue

        if tagValue == "" or tagValue == b"":
            continue
        if sys.getsizeof(tagValue) > MAX_TAG_SIZE:
            continue

        metadata[tagKey] = tagValue

    return metadata


def _parseFile(f):
    try:
        # download file and try to parse dicom
        with File().open(f) as fp:
            dataset = pydicom.dcmread(
                fp,
                # don't read huge fields, esp. if this isn't even really dicom
                defer_size=1024,
                # don't read image data, just metadata
                stop_before_pixels=True,
            )
            return _coerceMetadata(dataset)
    except (pydicom.errors.InvalidDicomError, GirderException):
        # If pydicom.errors.InvalidDicomError occurs, probably not a dicom file.
        # If GirderException, the file may have been deleted between scanning for import and handling the event
        return None
