# Customize File Browsing to Group Images and add Columns

A `.large_image_config.yaml` file can change how images are grouped and add columns displaying metadata.
The YAML file applies to all folders lower in the higherachy.

More information on `.large_image_config.yaml` here:
https://girder.github.io/large_image/girder_config_options.html#yaml-configuration-files

## Columns with Metadata via .large_image_config.yaml

![image](https://github.com/DigitalSlideArchive/girder_volview/assets/16823231/9ab0f04d-9103-431a-ab22-cbf87ee760e2)

To show DICOM tags in a table view, add a `.large_image_config.yaml` file higher in the Girder folder hierarchy. When a DICOM file is imported/uploaded, its DICOM tags are saved on the Item metadata under the top level `dicom` key.

Example `.large_image_config.yaml` file:

```yml
# If present, show a table with column headers in item lists
itemList:
  # Show these columns in order from left to right.  Each column has a
  # "type" and "value".  It optionally has a "title" used for the column
  # header, and a "format" used for searching and filtering.  The "label",
  # if any, is displayed to the left of the column value.  This is more
  # useful in an grid view than in a column view.
  columns:
    - # The "record" type is from the default item record.  The value is
      # one of "name", "size", or "controls".
      type: record
      value: name
    - type: record
      value: size
    - # The "metadata" type is taken from the item's "meta" contents.  It
      # can be a nested key by using dots in its name.
      type: metadata
      value: dicom.Modality
      title: Modality
    - type: metadata
      value: dicom.BodyPartExamined
      title: Body Part Examined
    - type: metadata
      value: dicom.StudyDate
      title: Study Date
    - type: metadata
      value: dicom.StudyDescription
      title: Study Description
    - type: metadata
      value: dicom.SeriesDescription
      title: Series Description
    - type: metadata
      value: dicom.ManufacturerModelName
      title: Manufacturer Model Name
    - type: metadata
      value: dicom.StudyInstanceUID
      title: Study Instance UID
    - type: metadata
      value: dicom.SeriesInstanceUID
      title: Series Instance UID
```

## Open images with VolView at DICOM study level

When your dataset has images that are not co-located in the folder hierarchy, VolView's Open Folder/Checked button won't help much. However, we can use a .large_image_config.yaml file to group files based on their Girder metadata.

Here is a video showing how to set up the YAML config file.
https://youtu.be/uCWmGVkk6TI

Example config YAML file that groups files at 2 levels using their DICOM Tags: PatientID -> StudyInstanceUID.

```yaml
defaultItemList: patientList
namedItemLists:
  patientList:
    layout:
      # flatten: true
      flatten: only
    #   # The default layout is a list.  This can optionally be "grid"
    #   mode: grid
    #   # max-width is only used in grid mode.  It is the maximum width in
    #   # pixels for grid entries.  It defaults to 250.
    #   max-width: 250
    group:
      keys: dicom.PatientID
      counts:
        dicom.StudyInstanceUID: _count.studiescount
        dicom.SeriesInstanceUID: _count.seriescount
    # navigate can be:
    #  - the name of an itemList record to show a sublist
    #    if grouping, a filter of the group is added to the sublist
    #  - "open" to use the first open control to open the list of things that are grouped
    #  - "open:<viewer>" to open with a specific viewer
    navigate:
      type: itemList # or open, or item
      name: studyList # itemList name, or app, or unset for item; for open, no key is default, or its histomicsui or volview or dive
    # Show these columns
    columns:
      - type: image
        value: thumbnail
        title: Thumbnail
        # width: 160
        # height: 30
      - type: metadata
        value: dicom.PatientID
        title: Patient ID
        format: text
      - type: metadata
        value: dicom.PatientAge
        title: Age
      - type: metadata
        value: dicom.PatientSex
        title: Sex
      - type: metadata
        value: _count.studiescount
        title: Number of Studies
        format: count
      - type: metadata
        value: _count.seriescount
        title: Number of Series
        format: count
      - type: record
        value: controls
    defaultSort:
      - type: metadata
        value: dicom.PatientID
        dir: down
      - type: record
        value: name
        dir: down
  studyList:
    layout:
      flatten: only
    group:
      keys:
        - dicom.StudyInstanceUID
      counts:
        dicom.SeriesInstanceUID: _count.seriescount
        _id: _count.slicescount
    navigate:
      type: open
      name: volview
    columns:
      - type: image
        value: thumbnail
        title: Thumbnail
      - type: metadata
        value: dicom.PatientID
        title: Patient ID
        format: text
      - type: metadata
        value: dicom.StudyInstanceUID
        title: Study ID
        format: text
      - type: metadata
        value: dicom.StudyDescription
        title: Study Description
      - type: metadata
        value: dicom.StudyDate
        title: Study Date
      - type: metadata
        value: _count.seriescount
        title: Number of series
        format: count
      - type: metadata
        value: _count.slicescount
        title: Number of Slices
        format: count
      - type: record
        value: controls
    defaultSort:
      - type: metadata
        value: dicom.PatientID
        dir: down
      - type: metadata
        value: dicom.StudyInstanceUID
        dir: down
```
