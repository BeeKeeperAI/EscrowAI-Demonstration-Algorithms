"""
Report size / count test harness for the EnclaveAPI report endpoint.

This algorithm does no model validation. It builds N JSON reports of a
configurable size and posts each one through the EnclaveSDK Report API
(the same ``api_v1_report_post`` call the original COVID validation used),
always with status "Completed". It is used to verify how many reports, and
of what size, the EnclaveAPI report endpoint accepts.

Environment variables
---------------------
REPORT_SIZE_MB   Size of each report's ``json_data`` payload in megabytes
                 (decimal values allowed, e.g. "0.5" or "100"). Default: 1
REPORT_COUNT     Number of reports to post. Default: 1
REPORT_INTERVAL_SECONDS
                 Seconds to sleep between consecutive report posts. Default: 0.
                 Used to measure how long the VM survives after the first
                 "Completed" report queues the destroy pipeline.
REPORT_INTERMEDIATE_STATUS
                 Status for every report except the last one. Default:
                 "In Progress", which is how a real algorithm should post
                 several validated reports: only the last report is posted as
                 "Completed" and ends the run. Set to "Completed" to reproduce
                 the original all-Completed behaviour, where the first report
                 ends the run and later reports race the VM destroy.
ENCLAVE_URL      EnclaveAPI base URL. Default: https://enclaveapi.escrow.beekeeperai.com/
"""

import json
import logging
import os
import sys
import time
from typing import Dict

import EnclaveSDK
from EnclaveSDK import LogData, Report
from EnclaveSDK.exceptions import ApiException

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("report-size-test")

MB = 1024 * 1024

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Strip a trailing slash so the SDK does not build paths like "//api/v1/report"
ENCLAVE_URL = os.getenv("ENCLAVE_URL", "https://enclaveapi.escrow.beekeeperai.com/").rstrip("/")
REPORT_SIZE_MB = float(os.getenv("REPORT_SIZE_MB", "1"))
REPORT_COUNT = int(os.getenv("REPORT_COUNT", "1"))
REPORT_INTERVAL_SECONDS = float(os.getenv("REPORT_INTERVAL_SECONDS", "0"))
REPORT_INTERMEDIATE_STATUS = os.getenv("REPORT_INTERMEDIATE_STATUS", "In Progress").strip()
VALID_INTERMEDIATE_STATUSES = ("In Progress", "Completed")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.json")

configuration = EnclaveSDK.Configuration(ENCLAVE_URL)
api_client = EnclaveSDK.ApiClient(configuration)


def load_json_schema():
    """
    Load the Cerberus validation schema shipped next to this script, if present.
    The SDK requires ``json_schema`` in the Sandbox and ignores it elsewhere
    (in EscrowAI the platform's validation criteria are used instead).
    """
    if not os.path.exists(SCHEMA_PATH):
        log.info("No schema.json found next to app.py; reports will be posted without json_schema")
        return None
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    log.info("Loaded validation schema from %s", SCHEMA_PATH)
    return schema


JSON_SCHEMA = load_json_schema()


# ---------------------------------------------------------------------------
# EnclaveAPI helpers
# ---------------------------------------------------------------------------
def post_log(message: str, status: str = None) -> None:
    """Post a log line to the Enclave Log API. Never raises."""
    log.info("[enclave-log] %s%s", message, f" (status={status})" if status else "")
    try:
        api_instance = EnclaveSDK.LogApi(api_client)
        payload = {"message": message}
        if status:
            payload["status"] = status
        api_instance.api_v1_log_post(LogData.from_dict(payload))
    except Exception as exc:  # noqa: BLE001 - logging must never abort the test
        log.warning("Failed to post log to EnclaveAPI: %s", exc)


def post_report(final_report: Dict):
    """Post a report via the Report API (same call the validation algorithm used)."""
    api_instance = EnclaveSDK.ReportApi(api_client)
    report = Report.from_dict(final_report)
    return api_instance.api_v1_report_post(report)


# ---------------------------------------------------------------------------
# Report construction
# ---------------------------------------------------------------------------
def serialized_size(obj) -> int:
    """Size in bytes of ``obj`` once JSON-serialized, as the SDK will send it."""
    return len(json.dumps(obj).encode("utf-8"))


def build_json_data(index: int, target_bytes: int) -> Dict:
    """
    Build a ``json_data`` dict whose serialized size is as close as possible
    to ``target_bytes`` (never smaller). The bulk is a ``padding`` string of
    printable characters.
    """
    base = {
        "report": {
            "test": "enclaveapi-report-size-limit",
            "report_index": index,
            "report_count": REPORT_COUNT,
            "target_size_mb": REPORT_SIZE_MB,
            "target_size_bytes": target_bytes,
            "padding": "",
        }
    }
    overhead = serialized_size(base)
    pad_len = max(0, target_bytes - overhead)

    pattern = "abcdefghijklmnopqrstuvwxyz0123456789"
    base["report"]["padding"] = (pattern * (pad_len // len(pattern) + 1))[:pad_len]
    return base


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    if REPORT_COUNT < 1:
        log.error("REPORT_COUNT must be >= 1 (got %s)", REPORT_COUNT)
        post_log(f"Invalid REPORT_COUNT={REPORT_COUNT}", "Failed")
        return 1
    if REPORT_SIZE_MB <= 0:
        log.error("REPORT_SIZE_MB must be > 0 (got %s)", REPORT_SIZE_MB)
        post_log(f"Invalid REPORT_SIZE_MB={REPORT_SIZE_MB}", "Failed")
        return 1
    if REPORT_INTERVAL_SECONDS < 0:
        log.error("REPORT_INTERVAL_SECONDS must be >= 0 (got %s)", REPORT_INTERVAL_SECONDS)
        post_log(f"Invalid REPORT_INTERVAL_SECONDS={REPORT_INTERVAL_SECONDS}", "Failed")
        return 1
    if REPORT_INTERMEDIATE_STATUS not in VALID_INTERMEDIATE_STATUSES:
        log.error(
            "REPORT_INTERMEDIATE_STATUS must be one of %s (got %r)",
            VALID_INTERMEDIATE_STATUSES, REPORT_INTERMEDIATE_STATUS,
        )
        post_log(f"Invalid REPORT_INTERMEDIATE_STATUS={REPORT_INTERMEDIATE_STATUS!r}", "Failed")
        return 1

    target_bytes = int(REPORT_SIZE_MB * MB)
    total_planned_mb = REPORT_SIZE_MB * REPORT_COUNT

    log.info("ENCLAVE_URL    = %s", ENCLAVE_URL)
    log.info("REPORT_SIZE_MB = %s (%d bytes per report)", REPORT_SIZE_MB, target_bytes)
    log.info("REPORT_COUNT   = %d", REPORT_COUNT)
    log.info("REPORT_INTERVAL_SECONDS = %s", REPORT_INTERVAL_SECONDS)
    log.info("REPORT_INTERMEDIATE_STATUS = %s (last report is always Completed)", REPORT_INTERMEDIATE_STATUS)
    log.info("Planned total  = %.2f MB across %d report(s)", total_planned_mb, REPORT_COUNT)
    post_log(
        f"Starting report size test: {REPORT_COUNT} report(s) x {REPORT_SIZE_MB} MB "
        f"(planned total {total_planned_mb:.2f} MB, {REPORT_INTERVAL_SECONDS}s between reports, "
        f"intermediate status {REPORT_INTERMEDIATE_STATUS!r})",
        "In Progress",
    )

    successes = 0
    failures = 0
    bytes_sent_ok = 0
    results = []

    for i in range(1, REPORT_COUNT + 1):
        if i > 1 and REPORT_INTERVAL_SECONDS > 0:
            # The run is already marked ended by the first Completed report, so every
            # log below is stored with status Completed and hidden by the UI download.
            # The stdout lines still reach the run log via the EnclaveAPI container output.
            log.info(
                "Sleeping %.0fs before report %d/%d (VM destroy pipeline is running since the "
                "first Completed report)", REPORT_INTERVAL_SECONDS, i, REPORT_COUNT,
            )
            post_log(f"Sleeping {REPORT_INTERVAL_SECONDS:.0f}s before report {i}/{REPORT_COUNT}", "In Progress")
            time.sleep(REPORT_INTERVAL_SECONDS)
        name = f"Report Size Test {i}/{REPORT_COUNT} ({REPORT_SIZE_MB} MB)"
        # Only the last report ends the run. Earlier ones use the configured
        # intermediate status so they are stored without triggering the VM destroy.
        report_status = "Completed" if i == REPORT_COUNT else REPORT_INTERMEDIATE_STATUS
        log.info("--- Report %d/%d: building payload ---", i, REPORT_COUNT)
        t_build = time.perf_counter()
        json_data = build_json_data(i, target_bytes)
        final_report = {
            "json_data": json_data,
            "name": name,
            "status": report_status,
        }
        if JSON_SCHEMA is not None:
            final_report["json_schema"] = JSON_SCHEMA
        body_bytes = serialized_size(final_report)
        json_data_bytes = serialized_size(json_data)
        # Inside an enclave, EnclaveAPI base64-encodes json_data before forwarding the
        # report to EscrowAI, so the upstream request is ~4/3 the size of json_data.
        forwarded_estimate = (json_data_bytes + 2) // 3 * 4
        log.info(
            "Report %d/%d built in %.2fs: json_data=%d bytes (%.2f MB), full body=%d bytes (%.2f MB), "
            "est. base64 size forwarded to EscrowAI=%d bytes (%.2f MB)",
            i, REPORT_COUNT, time.perf_counter() - t_build,
            json_data_bytes, json_data_bytes / MB,
            body_bytes, body_bytes / MB,
            forwarded_estimate, forwarded_estimate / MB,
        )

        log.info("Report %d/%d: posting '%s' with status=%s", i, REPORT_COUNT, name, report_status)
        t_post = time.perf_counter()
        try:
            response = post_report(final_report)
            elapsed = time.perf_counter() - t_post
            successes += 1
            bytes_sent_ok += body_bytes
            log.info(
                "Report %d/%d: ACCEPTED in %.2fs. Response: %s",
                i, REPORT_COUNT, elapsed, response,
            )
            post_log(
                f"Report {i}/{REPORT_COUNT} accepted ({body_bytes / MB:.2f} MB, "
                f"{elapsed:.2f}s). Cumulative accepted: {bytes_sent_ok / MB:.2f} MB",
                "In Progress",
            )
            results.append({"index": i, "bytes": body_bytes, "result": "accepted", "seconds": round(elapsed, 2)})
        except ApiException as exc:
            elapsed = time.perf_counter() - t_post
            failures += 1
            body_preview = (exc.body or "")[:500] if isinstance(exc.body, str) else str(exc.body)[:500]
            log.error(
                "Report %d/%d: REJECTED in %.2fs. HTTP %s %s. Body: %s",
                i, REPORT_COUNT, elapsed, exc.status, exc.reason, body_preview,
            )
            post_log(
                f"Report {i}/{REPORT_COUNT} rejected ({body_bytes / MB:.2f} MB): "
                f"HTTP {exc.status} {exc.reason}. Cumulative accepted before this: {bytes_sent_ok / MB:.2f} MB",
                "In Progress",
            )
            results.append({"index": i, "bytes": body_bytes, "result": f"rejected HTTP {exc.status}", "seconds": round(elapsed, 2)})
        except Exception as exc:  # noqa: BLE001 - connection errors, timeouts, etc.
            elapsed = time.perf_counter() - t_post
            failures += 1
            log.exception("Report %d/%d: ERROR after %.2fs: %s", i, REPORT_COUNT, elapsed, exc)
            post_log(
                f"Report {i}/{REPORT_COUNT} error ({body_bytes / MB:.2f} MB): {type(exc).__name__}: {exc}",
                "In Progress",
            )
            results.append({"index": i, "bytes": body_bytes, "result": f"error {type(exc).__name__}", "seconds": round(elapsed, 2)})

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    log.info("=== Summary ===")
    for r in results:
        log.info("  report %d: %.2f MB -> %s (%.2fs)", r["index"], r["bytes"] / MB, r["result"], r["seconds"])
    log.info(
        "Accepted %d/%d report(s); rejected/errored %d; accepted total %.2f MB of planned %.2f MB",
        successes, REPORT_COUNT, failures, bytes_sent_ok / MB, total_planned_mb,
    )

    final_status = "Completed" if failures == 0 else "Failed"
    post_log(
        f"Report size test finished: accepted {successes}/{REPORT_COUNT} x {REPORT_SIZE_MB} MB "
        f"(accepted total {bytes_sent_ok / MB:.2f} MB, planned {total_planned_mb:.2f} MB)",
        final_status,
    )
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
