"""
Report size / count test harness for the EnclaveAPI report endpoint.

This algorithm does no model validation. It builds N JSON reports of a
configurable size and posts each one through the EnclaveSDK Report API
(the same ``api_v1_report_post`` call the original COVID validation used),
with configurable statuses. It is used to verify how many reports, and of
what size, the EnclaveAPI report endpoint accepts.

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
REPORT_FINAL_STATUS
                 Status of the last report. Default: "Completed", which ends
                 the run. Set to "In Progress" to post every report as
                 "In Progress"; the harness's closing log line is then also
                 posted as "In Progress" (a "Completed" log line ends the run
                 too), so the run only ends when the VM runner posts its own
                 "Completed" report after the algorithm container exits.
REPORT_FAIL_AT   Comma-separated 1-based indexes of reports to post deliberately
                 invalid, for example "49" or "49,50". Default: none. Used to
                 observe what the platform does when one report in a sequence
                 is rejected. The harness still records such a rejection as
                 "as intended", but a rejected report is a hard failure for
                 the platform, so the run is over and the harness exits 2.
REPORT_FAIL_MODE How an injected report is made invalid. "schema" (default)
                 replaces the "report" object with a string, which fails any
                 schema that types "report" as a dict, so EnclaveAPI answers
                 400 and does not forward it. "oversize" pads json_data past
                 the 100 MiB per-report limit, so an EnclaveAPI build with the
                 size pre-check answers 413; an older build forwards ~134 MB
                 of base64 to the backend, which Dev and QA cannot decode.
REPORT_CONTINUE_ON_REJECT
                 Default: false. By default the harness stops at the first
                 report that is not accepted, whether injected or not, and
                 exits 2: that is what a well-behaved algorithm does, because
                 EnclaveAPI has already failed the run. Set to "true" to keep
                 posting the remaining reports anyway, to show what the
                 platform does with an algorithm that ignores the failure. An
                 EnclaveAPI build that refuses work after the run has ended
                 answers 409 "run ended" to each of those; the harness counts
                 them separately as "refused after run ended".
ENCLAVE_URL      EnclaveAPI base URL. Default: https://enclaveapi.escrow.beekeeperai.com/

Exit codes: 0 when every report was accepted, 1 for a configuration error, 2
when any report was rejected, refused or errored, or an injected report was
accepted.

Status values are matched ignoring case, spaces, underscores and hyphens, so
"InProgress", "in_progress" and "in-progress" all mean "In Progress". Use one
of those spellings in an EscrowAI run configuration: the platform exports run
parameters through an unquoted shell ``export KEY=value``, which splits
"In Progress" at the space and delivers only "In".
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
VALID_REPORT_STATUSES = ("In Progress", "Completed")


def normalize_status(raw: str) -> str:
    """
    Map any spelling of a report status to the exact value the API expects.

    EscrowAI run parameters reach the container through an unquoted shell
    ``export KEY=value``, which splits "In Progress" at the space and delivers
    just "In". So the run configuration has to use a space-free spelling such
    as ``InProgress`` or ``in_progress``; this accepts those (case, spaces,
    underscores and hyphens are ignored) and returns "In Progress". Anything
    unrecognised is returned stripped so the validation below reports it.
    """
    key = "".join(ch for ch in raw.lower() if ch.isalnum())
    return {"inprogress": "In Progress", "completed": "Completed"}.get(key, raw.strip())


REPORT_INTERMEDIATE_STATUS = normalize_status(os.getenv("REPORT_INTERMEDIATE_STATUS", "In Progress"))
REPORT_FINAL_STATUS = normalize_status(os.getenv("REPORT_FINAL_STATUS", "Completed"))
REPORT_FAIL_AT_RAW = os.getenv("REPORT_FAIL_AT", "")
REPORT_FAIL_MODE = os.getenv("REPORT_FAIL_MODE", "schema").strip().lower()
VALID_FAIL_MODES = ("schema", "oversize")
REPORT_CONTINUE_ON_REJECT = os.getenv("REPORT_CONTINUE_ON_REJECT", "false").strip().lower() in ("1", "true", "yes", "on")
# EnclaveAPI builds that refuse work once the run has ended answer with this.
HTTP_RUN_ENDED = 409
# One MiB past the backend's 100 MiB per-report limit on the decoded JSON.
OVERSIZE_TARGET_BYTES = 101 * MB
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.json")


def parse_fail_at(raw: str):
    """Parse "49,50" into {49, 50}. Raises ValueError on anything non-numeric."""
    indexes = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            indexes.add(int(part))
    return indexes

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
    if REPORT_INTERMEDIATE_STATUS not in VALID_REPORT_STATUSES:
        log.error(
            "REPORT_INTERMEDIATE_STATUS must be one of %s (got %r)",
            VALID_REPORT_STATUSES, REPORT_INTERMEDIATE_STATUS,
        )
        post_log(f"Invalid REPORT_INTERMEDIATE_STATUS={REPORT_INTERMEDIATE_STATUS!r}", "Failed")
        return 1
    if REPORT_FINAL_STATUS not in VALID_REPORT_STATUSES:
        log.error(
            "REPORT_FINAL_STATUS must be one of %s (got %r)",
            VALID_REPORT_STATUSES, REPORT_FINAL_STATUS,
        )
        post_log(f"Invalid REPORT_FINAL_STATUS={REPORT_FINAL_STATUS!r}", "Failed")
        return 1
    try:
        fail_at = parse_fail_at(REPORT_FAIL_AT_RAW)
    except ValueError:
        log.error("REPORT_FAIL_AT must be a comma-separated list of report indexes (got %r)", REPORT_FAIL_AT_RAW)
        post_log(f"Invalid REPORT_FAIL_AT={REPORT_FAIL_AT_RAW!r}", "Failed")
        return 1
    out_of_range = sorted(i for i in fail_at if i < 1 or i > REPORT_COUNT)
    if out_of_range:
        log.error(
            "REPORT_FAIL_AT indexes must be between 1 and REPORT_COUNT=%d (got %s)",
            REPORT_COUNT, out_of_range,
        )
        post_log(
            f"Invalid REPORT_FAIL_AT={REPORT_FAIL_AT_RAW!r}: {out_of_range} out of range for "
            f"REPORT_COUNT={REPORT_COUNT}",
            "Failed",
        )
        return 1
    if REPORT_FAIL_MODE not in VALID_FAIL_MODES:
        log.error("REPORT_FAIL_MODE must be one of %s (got %r)", VALID_FAIL_MODES, REPORT_FAIL_MODE)
        post_log(f"Invalid REPORT_FAIL_MODE={REPORT_FAIL_MODE!r}", "Failed")
        return 1

    target_bytes = int(REPORT_SIZE_MB * MB)
    total_planned_mb = REPORT_SIZE_MB * REPORT_COUNT

    log.info("ENCLAVE_URL    = %s", ENCLAVE_URL)
    log.info("REPORT_SIZE_MB = %s (%d bytes per report)", REPORT_SIZE_MB, target_bytes)
    log.info("REPORT_COUNT   = %d", REPORT_COUNT)
    log.info("REPORT_INTERVAL_SECONDS = %s", REPORT_INTERVAL_SECONDS)
    log.info("REPORT_INTERMEDIATE_STATUS = %s", REPORT_INTERMEDIATE_STATUS)
    log.info("REPORT_FINAL_STATUS = %s", REPORT_FINAL_STATUS)
    if REPORT_FINAL_STATUS != "Completed":
        log.info(
            "No report will be posted as Completed; the run ends when the VM runner posts its "
            "own Completed report after this container exits"
        )
    log.info("Planned total  = %.2f MB across %d report(s)", total_planned_mb, REPORT_COUNT)
    if fail_at:
        log.info("REPORT_FAIL_AT = %s (mode %s): these reports are posted deliberately invalid", sorted(fail_at), REPORT_FAIL_MODE)
    log.info(
        "REPORT_CONTINUE_ON_REJECT = %s (%s)", REPORT_CONTINUE_ON_REJECT,
        "keep posting after a rejection" if REPORT_CONTINUE_ON_REJECT else "stop at the first rejection",
    )
    post_log(
        f"Starting report size test: {REPORT_COUNT} report(s) x {REPORT_SIZE_MB} MB "
        f"(planned total {total_planned_mb:.2f} MB, {REPORT_INTERVAL_SECONDS}s between reports, "
        f"intermediate status {REPORT_INTERMEDIATE_STATUS!r}, final status {REPORT_FINAL_STATUS!r}"
        + (f", deliberately invalid report(s) {sorted(fail_at)} via {REPORT_FAIL_MODE}" if fail_at else "")
        + (", continuing after rejections" if REPORT_CONTINUE_ON_REJECT else ", stopping at the first rejection")
        + ")",
        "In Progress",
    )

    successes = 0
    failures = 0            # normal reports rejected or errored
    expected_rejections = 0  # injected reports rejected, as intended
    unexpected_accepts = 0   # injected reports accepted, which should not happen
    refused_after_end = 0    # 409 "run ended" answers from EnclaveAPI
    stopped_at = None        # report index at which the harness stopped early
    bytes_sent_ok = 0
    results = []

    def stop_here(index: int, why: str) -> bool:
        """
        Decide whether to stop after a report that was not accepted. Logs the
        decision either way. Returns True when the loop should break.
        """
        if REPORT_CONTINUE_ON_REJECT:
            log.info("Report %d/%d: %s. REPORT_CONTINUE_ON_REJECT is set, continuing with the next report.", index, REPORT_COUNT, why)
            return False
        log.error(
            "Report %d/%d: %s. Stopping here: EnclaveAPI has failed the run and nothing posted after this "
            "point is accepted. Set REPORT_CONTINUE_ON_REJECT=true to keep posting anyway.",
            index, REPORT_COUNT, why,
        )
        post_log(
            f"Report {index}/{REPORT_COUNT}: {why}. Stopping: the run is over once a report is rejected.",
            "In Progress",
        )
        return True

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
        # By default only the last report is Completed and ends the run. Earlier
        # ones use the intermediate status so they are stored without triggering
        # the VM destroy. REPORT_FINAL_STATUS="In Progress" makes every report
        # In Progress and leaves ending the run to the VM runner.
        report_status = REPORT_FINAL_STATUS if i == REPORT_COUNT else REPORT_INTERMEDIATE_STATUS
        inject = i in fail_at
        log.info(
            "--- Report %d/%d: building payload%s ---", i, REPORT_COUNT,
            f" (DELIBERATELY INVALID, mode={REPORT_FAIL_MODE})" if inject else "",
        )
        t_build = time.perf_counter()
        if inject and REPORT_FAIL_MODE == "schema":
            # Any schema that types "report" as a dict rejects a string here, so
            # this fails validation whatever schema the project was created with.
            json_data = {
                "report": (
                    f"deliberately invalid report {i}/{REPORT_COUNT}: 'report' is a string "
                    "where the schema requires an object"
                )
            }
        else:
            json_data = build_json_data(i, OVERSIZE_TARGET_BYTES if inject else target_bytes)
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
            if inject:
                unexpected_accepts += 1
                log.error(
                    "Report %d/%d: ACCEPTED in %.2fs although it was deliberately invalid (mode=%s). Response: %s",
                    i, REPORT_COUNT, elapsed, REPORT_FAIL_MODE, response,
                )
                post_log(
                    f"Report {i}/{REPORT_COUNT} was deliberately invalid ({REPORT_FAIL_MODE}) but EnclaveAPI "
                    f"ACCEPTED it ({body_bytes / MB:.2f} MB, {elapsed:.2f}s)",
                    "In Progress",
                )
                results.append({"index": i, "bytes": body_bytes, "result": "accepted (UNEXPECTED, injected failure)", "seconds": round(elapsed, 2)})
            else:
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
            body_preview = (exc.body or "")[:500] if isinstance(exc.body, str) else str(exc.body)[:500]
            if exc.status == HTTP_RUN_ENDED:
                # EnclaveAPI has already failed the run and refuses further work.
                # Nothing was validated or forwarded; the platform never sees this report.
                refused_after_end += 1
                log.warning(
                    "Report %d/%d: REFUSED in %.2fs, the run has already ended. HTTP %s %s. Body: %s",
                    i, REPORT_COUNT, elapsed, exc.status, exc.reason, body_preview,
                )
                results.append({"index": i, "bytes": body_bytes, "result": "refused HTTP 409 (run ended)", "seconds": round(elapsed, 2)})
                if stop_here(i, "refused because the run has ended"):
                    stopped_at = i
                    break
            elif inject:
                expected_rejections += 1
                log.info(
                    "Report %d/%d: rejected as intended in %.2fs. HTTP %s %s. Body: %s",
                    i, REPORT_COUNT, elapsed, exc.status, exc.reason, body_preview,
                )
                post_log(
                    f"Report {i}/{REPORT_COUNT} was deliberately invalid ({REPORT_FAIL_MODE}) and was rejected "
                    f"as intended: HTTP {exc.status} {exc.reason}.",
                    "In Progress",
                )
                results.append({"index": i, "bytes": body_bytes, "result": f"rejected HTTP {exc.status} (expected)", "seconds": round(elapsed, 2)})
                if stop_here(i, "deliberately invalid report rejected"):
                    stopped_at = i
                    break
            else:
                failures += 1
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
                if stop_here(i, f"rejected with HTTP {exc.status}"):
                    stopped_at = i
                    break
        except Exception as exc:  # noqa: BLE001 - connection errors, timeouts, etc.
            elapsed = time.perf_counter() - t_post
            if inject:
                # A dropped connection on a deliberately oversized report is still a rejection.
                expected_rejections += 1
                log.warning("Report %d/%d: error after %.2fs on a deliberately invalid report: %s: %s", i, REPORT_COUNT, elapsed, type(exc).__name__, exc)
                post_log(
                    f"Report {i}/{REPORT_COUNT} was deliberately invalid ({REPORT_FAIL_MODE}) and failed with "
                    f"{type(exc).__name__}: {exc}.",
                    "In Progress",
                )
                results.append({"index": i, "bytes": body_bytes, "result": f"error {type(exc).__name__} (injected failure)", "seconds": round(elapsed, 2)})
                if stop_here(i, f"deliberately invalid report failed with {type(exc).__name__}"):
                    stopped_at = i
                    break
            else:
                failures += 1
                log.exception("Report %d/%d: ERROR after %.2fs: %s", i, REPORT_COUNT, elapsed, exc)
                post_log(
                    f"Report {i}/{REPORT_COUNT} error ({body_bytes / MB:.2f} MB): {type(exc).__name__}: {exc}",
                    "In Progress",
                )
                results.append({"index": i, "bytes": body_bytes, "result": f"error {type(exc).__name__}", "seconds": round(elapsed, 2)})
                if stop_here(i, f"failed with {type(exc).__name__}"):
                    stopped_at = i
                    break

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    log.info("=== Summary ===")
    for r in results:
        log.info("  report %d: %.2f MB -> %s (%.2fs)", r["index"], r["bytes"] / MB, r["result"], r["seconds"])
    not_sent = REPORT_COUNT - len(results)
    log.info(
        "Accepted %d/%d report(s); rejected/errored %d; deliberately invalid and rejected %d; "
        "deliberately invalid but accepted %d; refused after run ended %d; not sent %d; "
        "accepted total %.2f MB of planned %.2f MB",
        successes, REPORT_COUNT, failures, expected_rejections, unexpected_accepts,
        refused_after_end, not_sent, bytes_sent_ok / MB, total_planned_mb,
    )
    if stopped_at is not None:
        log.info("Stopped after report %d/%d; %d report(s) were never sent", stopped_at, REPORT_COUNT, not_sent)

    # The run is clean only if every report was accepted. Any report that was
    # rejected, refused or errored is a hard failure from the platform's point
    # of view, including a deliberately invalid one: the point of injecting it
    # is to see the platform end the run, and the harness's exit code agrees
    # with that. If EnclaveAPI has already ended the run, this closing log line
    # is refused too and only reaches the run log via the container's stdout.
    all_accepted = successes == REPORT_COUNT
    # A "Completed" log line ends the run just like a "Completed" report does, so
    # when no report is meant to end the run the closing log stays "In Progress".
    final_status = REPORT_FINAL_STATUS if all_accepted else "Failed"
    post_log(
        f"Report size test finished: accepted {successes}/{REPORT_COUNT} x {REPORT_SIZE_MB} MB "
        f"(accepted total {bytes_sent_ok / MB:.2f} MB, planned {total_planned_mb:.2f} MB)"
        + (f"; {expected_rejections} deliberately invalid report(s) rejected as intended" if fail_at else "")
        + (f"; {unexpected_accepts} deliberately invalid report(s) ACCEPTED" if unexpected_accepts else "")
        + (f"; {failures} normal report(s) rejected or errored" if failures else "")
        + (f"; {refused_after_end} refused after the run ended" if refused_after_end else "")
        + (f"; stopped after report {stopped_at}, {not_sent} never sent" if stopped_at is not None else ""),
        final_status,
    )
    return 0 if all_accepted else 2


if __name__ == "__main__":
    sys.exit(main())
