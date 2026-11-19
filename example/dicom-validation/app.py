import os
import json
import base64
import traceback
from io import BytesIO
from typing import List, Dict, ByteString

import pydicom
import EnclaveSDK
from EnclaveSDK import File, Report, LogData

# Use the ENCLAVE_URL environment variable to create an SDK configuration for the Sandbox
configuration = EnclaveSDK.Configuration(os.getenv("ENCLAVE_URL", "https://enclaveapi.escrow.beekeeperai.com/"))
# Use the SAS_URL environment variables to use the Data API in the Sandbox, otherwise default to None
sas_url = os.getenv("SAS_URL", None)
if sas_url:
    sas_url = base64.b64encode(sas_url.encode()).decode()

# Finalize the creation of your API client
api_client = EnclaveSDK.ApiClient(configuration)

ROOT_FOLDER = "(root)"
ENCRYPTED_SUFFIX = ".bkenc"
DICOM_EXTENSION = ".dcm"
PROGRESS_EVERY = 250


# Use the Data API class to get a list of files in the Blob container
def get_file_list(sas_url=None) -> List[File]:
    api_instance = EnclaveSDK.DataApi(api_client)
    api_response = api_instance.api_v1_data_files_get(sas_url=sas_url)

    return api_response.files


# Use the Data API class to securely decrypt and download a file give the `.name` attribute of the files list
def download_file(file_name: str, sas_url=None) -> ByteString:
    api_instance = EnclaveSDK.DataApi(api_client)
    content = api_instance.api_v1_data_file_get(file_name, sas_url=sas_url)

    return content


# Use the Log API class to post a log message
def post_log(log: Dict) -> Dict:
    api_instance = EnclaveSDK.LogApi(api_client)

    log = LogData.from_dict(log)
    api_response = api_instance.api_v1_log_post(log)

    return api_response


# Use the Report API to post a report
def post_report(finalReport: Dict) -> Dict:
    api_instance = EnclaveSDK.ReportApi(api_client)

    # Check if schema.json is available and read it into json_schema
    if os.path.exists("schema.json"):
        with open("schema.json", "r") as schema:
            finalReport['json_schema'] = EnclaveSDK.ReportJsonSchema.from_dict(json.load(schema))

    # The posted report will be validated against the DS-provided validation schema
    report = Report.from_dict(finalReport)
    api_response = api_instance.api_v1_report_post(report)
    return api_response


# Name of the entry without the encryption suffix
def plain_name(name: str) -> str:
    if name.endswith(ENCRYPTED_SUFFIX):
        return name[:-len(ENCRYPTED_SUFFIX)]
    return name


# Folder the entry sits in, taken from its path
def folder_of(name: str) -> str:
    parent = os.path.dirname(plain_name(name).strip("/"))
    return parent if parent else ROOT_FOLDER


# Every ancestor path of every listed entry. A listed entry whose own name is
# in this set is a folder, not a file, at any depth of nesting.
def folder_paths(names) -> set:
    folders = set()
    for name in names:
        parts = plain_name(name).strip("/").split("/")
        for i in range(1, len(parts)):
            folders.add("/".join(parts[:i]))
    return folders


# One log line for a group of skipped entries, with a few names as examples
def log_skipped(kind: str, names: List[str]) -> None:
    if names:
        examples = ", ".join(names[:5])
        post_log({"message": f"Skipped {len(names)} {kind} (for example: {examples})", "status": "In Progress"})


# Entries with an extension other than .dcm are not DICOM (for example .nrrd).
# Entries with no extension are read as DICOM, because DICOM files are often
# stored without one.
def is_other_file_type(name: str) -> bool:
    extension = os.path.splitext(os.path.basename(plain_name(name)))[1].lower()
    return extension != "" and extension != DICOM_EXTENSION


# Read the DICOM header of a downloaded entry
def read_dicom(file_content: ByteString):
    return pydicom.dcmread(BytesIO(file_content), stop_before_pixels=True)


def generateReport(folder_counts: Dict[str, int], modalities: Dict[str, int], entries_listed: int, skipped: int,
                   folder_entries_skipped: int, empty_entries_skipped: int) -> Dict:
    folders = [{"folder": folder, "file_count": count} for folder, count in sorted(folder_counts.items())]

    return {
        "entries_listed": entries_listed,
        "total_folders": len(folders),
        "total_files": sum(folder_counts.values()),
        "non_dicom_files_skipped": skipped,
        "folder_entries_skipped": folder_entries_skipped,
        "empty_entries_skipped": empty_entries_skipped,
        "folders": folders,
        "modalities": [{"modality": modality, "file_count": count} for modality, count in sorted(modalities.items())],
    }


def main():
    files = get_file_list(sas_url=sas_url)
    if not files:
        post_log({"message": "No files were listed in the dataset", "status": "Failed"})
        exit(1)

    post_log({"message": f"{len(files)} entries listed in the dataset", "status": "In Progress"})

    folders_listed = folder_paths(file.name for file in files)

    folder_counts = {}
    modalities = {}
    skipped = 0
    folder_entries = []
    empty_entries = []

    for file in files:
        # A folder that the listing returned as if it were a file
        if plain_name(file.name).strip("/") in folders_listed:
            folder_entries.append(file.name)
            continue

        if is_other_file_type(file.name):
            skipped += 1
            post_log({"message": f"Skipping non-DICOM file ({file.name})", "status": "In Progress"})
            continue

        # Empty according to the listing, so there is nothing to download.
        # This also covers a folder with nothing underneath it.
        size = getattr(file, "size", None)
        if size == 0:
            empty_entries.append(file.name)
            continue

        file_content = download_file(file.name, sas_url=sas_url)
        if not file_content:
            empty_entries.append(file.name)
            continue

        try:
            dataset = read_dicom(file_content)
        except Exception as e:
            post_log({"message": f"Failed to read listed entry ({file.name}) as a DICOM file (listed size: {size}, downloaded bytes: {len(file_content)}): {type(e).__name__}: {str(e)}", "status": "Failed"})
            raise

        folder = folder_of(file.name)
        folder_counts[folder] = folder_counts.get(folder, 0) + 1

        modality = str(dataset.get("Modality", "UNKNOWN"))
        modalities[modality] = modalities.get(modality, 0) + 1

        files_read = sum(folder_counts.values())
        if files_read % PROGRESS_EVERY == 0:
            post_log({"message": f"{files_read} DICOM files read so far", "status": "In Progress"})

    log_skipped("folder entries listed as files", folder_entries)
    log_skipped("empty entries", empty_entries)

    rawReport = {}
    rawReport['report'] = generateReport(folder_counts, modalities, len(files), skipped, len(folder_entries), len(empty_entries))

    print(json.dumps(rawReport, indent=4))
    finalReport = {
        "json_data": rawReport,
        "name": "DICOM Dataset Validation Report",
        "status": "Completed",
    }
    post_report(finalReport)


if __name__ == "__main__":
    post_log({"message": "Starting the DICOM Dataset Validation Report"})
    try:
        main()
    except Exception:
        traceback.print_exc()
        exit(1)
