# DICOM Dataset Validation

Lists every entry in a dataset through the EnclaveSDK Data API, reads each one as a DICOM file, and posts a report with the number of folders and the number of files in each folder.

It exists to test datasets stored in Azure Data Lake Storage Gen2 (hierarchical namespace), where each folder is also returned by a listing as a zero-byte entry.

## What it does

1. Lists the dataset with `DataApi.api_v1_data_files_get`, the same way the `covid-validation` example does.
2. Skips folders that the listing returned as files.
3. Skips entries whose extension is something other than `.dcm` (for example `.nrrd`).
4. Skips empty entries.
5. Downloads every other entry and reads its DICOM header with `pydicom`.
6. Groups the files by the folder in their path and posts the report.

## How folder entries are handled

An entry is treated as a folder when its name is the parent path of another listed entry, at any depth. For `folder1/folder2/file1.dcm`, listed entries named `folder1` and `folder1/folder2` are both skipped. This does not depend on the size or content of the entry.

A folder with nothing underneath it cannot be recognised that way. It is caught by the empty check instead: an entry is skipped when the listing reports a size of 0 or when its download returns no content.

Skipped folder and empty entries are counted in the report and summarised in one log message each.

An entry that is none of these and still cannot be read as DICOM fails the run. The `Failed` log names the entry, its listed size and the number of bytes downloaded. No report is posted.

Entries with no extension are read as DICOM because DICOM files are often stored without one.

## First version, without folder handling

`v1-no-folder-handling/` holds the first version of `app.py` and its `schema.json`. It reads every listed entry as DICOM and fails on the first folder entry, which is how the problem was reproduced on an ADLS Gen2 dataset. Its report does not have the `folder_entries_skipped` and `empty_entries_skipped` fields, so it needs its own schema.

The Dockerfile builds the current version. To build the first version, copy those two files over `app.py` and `schema.json`.

## Report

The report is validated against `schema.json`.

| Field | Meaning |
|---|---|
| `entries_listed` | Entries returned by the Data API listing |
| `total_folders` | Folders that contain at least one DICOM file |
| `total_files` | DICOM files read |
| `non_dicom_files_skipped` | Entries skipped because of their extension |
| `folder_entries_skipped` | Folders that the listing returned as files |
| `empty_entries_skipped` | Entries with a listed size of 0 or an empty download |
| `folders` | One item per folder: `folder` and `file_count` |
| `modalities` | One item per DICOM modality: `modality` and `file_count` |

Files at the top level of the container are reported under the folder `(root)`.

## Running in the Sandbox

Set `ENCLAVE_URL` to the Sandbox Enclave API and `SAS_URL` to the SAS URL of the dataset container, then run:

```
pip install -r requirements.txt
python ../escrowai_sandbox_runner.py app.py
```

Do not commit the SAS URL.

## Line endings

`run.sh` must keep LF line endings. It runs inside a Linux container, and CRLF breaks it. The other files can be CRLF.
