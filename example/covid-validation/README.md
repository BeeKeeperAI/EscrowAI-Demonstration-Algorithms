# EnclaveAPI Report Size Limit Test - EscrowAI Example

This example is a test harness for the EnclaveAPI report endpoint. It posts a configurable
number of reports of a configurable size, with configurable statuses, using the same
`api_v1_report_post` call that the original COVID-19 validation algorithm used. It is used to
verify the file size limit enforced by the report endpoint and whether the 100 MB limit applies
to each individual report or to the combined total of all reports posted by a run.

No model, data download, or validation logic is included.

## Environment Variables

| Variable         | Required | Default                                     | Description                                                         |
|------------------|----------|---------------------------------------------|---------------------------------------------------------------------|
| `REPORT_SIZE_MB` | No       | `1`                                         | Size of each report's `json_data` payload in MB. Decimals allowed.  |
| `REPORT_COUNT`   | No       | `1`                                         | Number of reports to post in the run.                               |
| `REPORT_INTERVAL_SECONDS` | No | `0`                                       | Seconds to sleep between consecutive report posts. Use it to measure how long the VM survives after the first `Completed` report. |
| `REPORT_INTERMEDIATE_STATUS` | No | `In Progress`                          | Status of every report except the last. `In Progress` is how a real algorithm posts several validated reports. Set to `Completed` to reproduce the original all-Completed runs. |
| `REPORT_FINAL_STATUS` | No  | `Completed`                                 | Status of the last report. Set to `InProgress` to post every report as `In Progress`; the closing log line is then also `In Progress` and the run ends only when the VM runner posts its own `Completed` report after the container exits. |
| `REPORT_FAIL_AT` | No       | none                                        | Comma-separated 1-based indexes of reports to post deliberately invalid, e.g. `49` or `49,50`. A rejection of one of these is the expected outcome and does not fail the harness. |
| `REPORT_FAIL_MODE` | No     | `schema`                                    | `schema` replaces the `report` object with a string so schema validation rejects it (EnclaveAPI 400, not forwarded). `oversize` pads the report past 100 MiB (EnclaveAPI 413 on builds with the size pre-check). Avoid `oversize` on Dev/QA with older EnclaveAPI builds. |
| `ENCLAVE_URL`    | No       | `https://enclaveapi.escrow.beekeeperai.com/` | EnclaveAPI base URL.                                                |

Status values are matched ignoring case, spaces, underscores and hyphens, so `InProgress`,
`in_progress` and `in-progress` all mean `In Progress`. **In an EscrowAI run configuration use
a space-free spelling.** The platform turns run parameters into environment variables with an
unquoted shell `export KEY=value` (`entrypoint.sh` in the algorithm build pipeline, and the
runtime downloader), which splits `In Progress` at the space and delivers only `In`. The
harness then exits with code 1 and logs `REPORT_FINAL_STATUS must be one of ... (got 'In')`.

Typing the value with double quotes in the run configuration form, `"In Progress"`, also works
when the algorithm image was built by the `algo-runtime-containerbuild` pipeline: its entrypoint
sources the `training_parameters_env` file the downloader writes, so the shell removes the
quotes and the variable is exactly `In Progress`. That is how to run an image built before
status normalisation was added. It does not work with the older entrypoint in
`bkservices-template/azure-pipelines/entrypoint.sh`, which does `export $line` per parameter
and never re-parses quotes. A space-free spelling works on both paths, so prefer it.

## What the Algorithm Does

1. Reads `REPORT_SIZE_MB` and `REPORT_COUNT` and logs the planned run (per-report size, count, planned total).
2. For each report, builds a `json_data` dict padded with printable characters so that its
   serialized JSON size equals the requested size, then posts it with `REPORT_INTERMEDIATE_STATUS`
   (default `In Progress`) or, for the last report, `REPORT_FINAL_STATUS` (default `Completed`).
3. Logs the outcome of every post: accepted (with response and elapsed time) or rejected
   (HTTP status, reason, and the first 500 characters of the response body).
4. Continues to the next report after a rejection, so a run shows exactly which report in the
   sequence was the first to fail.
5. Prints a summary table and posts a final log line with status `REPORT_FINAL_STATUS` (all
   accepted) or `Failed` (at least one rejection). A `Completed` log line ends the run just like
   a `Completed` report, which is why the closing log follows the final report status. Exit code
   is `0` on full success, `2` if any report was rejected, `1` for invalid configuration.

All milestones are also posted to the Enclave Log API so they appear in the EscrowAI run log.

## Files

1. `Dockerfile` - Container definition.
2. `run.sh` - Entry point that runs `app.py`.
3. `app.py` - The test harness.
4. `requirements.txt` - Only `EnclaveSDK`.
5. `schema.json` - Cerberus validation schema matching the test report shape. In the sandbox it
   is attached to every report as `json_schema`. In EscrowAI production, upload this same file
   as the validation criteria. The old COVID metrics schema (accuracy / specificity /
   sensitivity) will **reject** these reports with "unknown field" errors, because Cerberus
   does not allow keys that the schema does not list.

Each report's `json_data` has this shape:

```json
{
  "report": {
    "test": "enclaveapi-report-size-limit",
    "report_index": 1,
    "report_count": 3,
    "target_size_mb": 100.0,
    "target_size_bytes": 104857600,
    "padding": "abcdefghijklmnopqrstuvwxyz0123456789abc..."
  }
}
```

## How to Run

### Local / Sandbox

```bash
cd example/covid-validation
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

export ENCLAVE_URL="https://enclaveapi.escrow.beekeeperai.com/"
export REPORT_SIZE_MB=100
export REPORT_COUNT=3
python app.py
```

### Docker

```bash
cd example/covid-validation
docker build -t report-size-test .
docker run --rm --network host \
  -e ENCLAVE_URL="https://enclaveapi.escrow.beekeeperai.com/" \
  -e REPORT_SIZE_MB=100 \
  -e REPORT_COUNT=3 \
  report-size-test
```

### EscrowAI Enclave

Package the directory with the Encryption Tool, upload it, and set `REPORT_SIZE_MB` and
`REPORT_COUNT` in the run configuration. No SAS URL or dataset is required. Upload
`schema.json` from this directory as the validation criteria; do not reuse the old COVID
metrics schema, or every report will be rejected for schema reasons rather than size.

## Verification Steps

Goal: determine whether the 100 MB limit on the report endpoint is enforced per report or on
the combined total of all reports in a run.

Run each scenario below as a separate run. For every run, record from the log output:

- The `Planned total` line (per-report size x count).
- For each report: `ACCEPTED` or `REJECTED`, the HTTP status on rejection, and the
  `Cumulative accepted` value at that point.
- The final `Accepted N/M report(s)` summary line.

### Scenario 1 - Multiple reports, each exactly 100 MB

```
REPORT_SIZE_MB=100
REPORT_COUNT=3
```

Planned total: 300 MB.

- If every report is accepted: the limit is not a hard 100 MB per report, and it is not a 100 MB total.
- If the first report is rejected: the limit is below or at 100 MB per report (the request body
  is slightly larger than 100 MB because of the `name` and `status` fields). Re-run with
  `REPORT_SIZE_MB=99` to confirm.
- If report 1 is accepted but report 2 is rejected: the limit is enforced on the cumulative total.

### Scenario 2 - Multiple reports, each within 100 MB, combined total above 100 MB

```
REPORT_SIZE_MB=50
REPORT_COUNT=3
```

Planned total: 150 MB.

- All three accepted: the limit is per report, not total.
- Reports 1 and 2 accepted (100 MB cumulative), report 3 rejected: the limit is on the total.

### Scenario 3 - Multiple small reports, combined total below 100 MB

```
REPORT_SIZE_MB=10
REPORT_COUNT=5
```

Planned total: 50 MB. This is the control case. All five reports are expected to be accepted
under either interpretation of the limit. If any are rejected, the failure is not caused by the
100 MB limit; check the HTTP status and body in the log.

### Scenario 4 - Multiple small reports, combined total above 100 MB

```
REPORT_SIZE_MB=30
REPORT_COUNT=5
```

Planned total: 150 MB.

- All five accepted: the limit is per report.
- Reports 1 to 3 accepted (90 MB cumulative) and report 4 rejected (would reach 120 MB): the
  limit is on the total.

### Interpreting the results

| Scenario | Per-report limit expected | Total limit expected                     |
|----------|---------------------------|------------------------------------------|
| 1        | 3/3 or 0/3 accepted       | 1/3 accepted                             |
| 2        | 3/3 accepted              | 2/3 accepted                             |
| 3        | 5/5 accepted              | 5/5 accepted                             |
| 4        | 5/5 accepted              | 3/5 accepted                             |

Any result where the first rejection lines up with the cumulative total crossing 100 MB
indicates a total-payload limit. Any result where rejections depend only on `REPORT_SIZE_MB`,
regardless of count, indicates a per-report limit.

### What EnclaveAPI actually enforces (from the EnclaveAPI source)

The `POST /api/v1/report` handler in EnclaveAPI behaves differently depending on where it runs:

| Where                           | Size check in EnclaveAPI                                   | What happens to the report                                                                 |
|---------------------------------|------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| Hosted sandbox (Auth0 enabled)  | `Content-Length > 100 MB` returns HTTP 413, per request    | Validated against the attached `json_schema`, then discarded. Nothing is forwarded.         |
| Enclave / TEE (no Auth0)        | None. Above 10 MB it only posts a warning to the run log   | `json_data` is base64-encoded (size x 4/3) and forwarded to the EscrowAI backend           |

So in the sandbox the 100 MB limit is per report, measured on the raw request body, with no
cumulative total. In an enclave EnclaveAPI imposes no limit itself; whatever rejects a large
report is the EscrowAI backend or a proxy in front of it. EnclaveAPI surfaces that as HTTP 500
to the algorithm, and the run log shows the upstream status in an `AO:` line, for example
`EscrowAI rejected the report with status code 502`. An empty 502 body after roughly 60 seconds
points to an upstream gateway or worker timeout rather than an explicit size check.

Observed in the dev enclave on 2026-10-20: single reports of 95 MB and 50 MB were both
rejected with an upstream 502. Root cause, confirmed from the Application Gateway access log
and the frontoffice App Service console log:

- The gateway forwarded every report to the backend with no WAF action
  (`serverRouted` populated, `ERRORINFO_NO_ERROR`), and the 502 came from the backend.
- At the same timestamps the frontoffice gunicorn log shows
  `Worker (pid:N) was sent SIGKILL! Perhaps out of memory?` followed by a new worker booting.
- The report view decodes and re-serializes the whole payload several times in memory, so
  peak memory is a multiple of the report size and exceeds the App Service instance's headroom.

Observed in the staging enclave on 2026-11-02: a single 100 MB report was rejected with
HTTP 400 from the backend: `Report JSON is invalid: JSON string exceeds max size of
104857600 bytes`. This is the frontoffice's `secure_json_parse` check, which refuses any
report whose decoded `json_data` JSON exceeds 100 MiB. EnclaveAPI relays it to the algorithm
as HTTP 500 with the real message in the run log. Note that EnclaveAPI re-serializes
`json_data` in Go and, for a Completed report in a validation run, adds a `type` field, so
the backend's byte count differs from this script's by a few tens of bytes. Keep test sizes a
few kilobytes under 100 MiB (for example `REPORT_SIZE_MB=99.9`) rather than exactly at it.

Also on 2026-11-02 in staging: a single 99.8 MB report was accepted, and a run with
`REPORT_SIZE_MB=99.8` and `REPORT_COUNT=2` had both reports accepted and stored (the run's
Report Details shows two reports of 99.80 MB each, 199.6 MB combined). Together with the
100 MB rejection this brackets the limit at 100 MiB of decoded report JSON, per report, with no
cumulative limit.

Two platform behaviours to be aware of when reading multi-report runs:

- The first report posted with status `Completed` marks the run as ended and queues the VM
  destroy. The destroy is an Azure DevOps pipeline run, so the VM survives for several more
  minutes while that pipeline queues and executes. Later reports are still accepted during that
  window, because the backend saves reports against an ended run and skips the destroy. This is
  why posting several `Completed` reports back to back appears to work (2 and 4 reports of
  99.8 MB were all stored in staging). It depends entirely on pipeline latency: a run that pauses
  between reports will lose the VM mid-run and the remaining reports will never be sent. A real
  algorithm should post intermediate reports as `In Progress` and only the final one as
  `Completed`.

  The destroy pipeline for a CVM run has two stages (`terraform_validate`, then
  `terraform_destroy`), each a separate Azure DevOps job that must acquire an agent, check out
  the repo, install terraform, run `terraform init` against remote state, and then destroy the
  VM, NIC, disk and public IP. On staging that looked like 5 to 10 minutes end to end, but on
  Dev on 2026-11-23 the algorithm was dead 60 to 90 s after the run ended (scenario 6), so the
  window varies by environment and agent-pool availability. To measure it
  on a given environment, run with `REPORT_INTERVAL_SECONDS` set (for example `REPORT_COUNT=3`
  and `REPORT_INTERVAL_SECONDS=240`) and compare the timestamp of the first report against the
  `VM Destroyed` line that the pipeline posts to the run log. Reports whose sleep ends after
  that line are lost, not rejected: the process dies with the VM and nothing is sent.

  Observed in staging on 2026-11-03: a run with `REPORT_COUNT=10` posted back to back ended
  with 5 reports stored and no rejection lines in the run log. That is consistent with the VM
  being destroyed while report 6 was in flight, roughly 5 to 8 minutes after report 1. The
  cutoff is the VM lifetime after the first `Completed` report, not a report count or a
  cumulative size. Any algorithm that keeps posting after its first `Completed` report is
  subject to it.
- **Report Details shows at most 15 reports.** The web UI's Report Details modal requests the
  first page of reports with a page size of 15 and has no pager for that list
  (`beekeeper-web-frontend`, `src/lib/hooks/run-configurations/use-run-configurations.ts`,
  `PAGE_SIZE = 15`). The backend report list is unpaginated unless the UI passes `page_size`,
  so every report is stored and reachable; the UI just does not show rows beyond 15. A run on
  Dev on 2026-11-16 with `REPORT_SIZE_MB=1`, `REPORT_COUNT=100` and the default `In Progress`
  intermediates had all 100 posts accepted in the run log and "15 reports available" in Report
  Details. The staging run on 2026-11-03 with `REPORT_SIZE_MB=5` and `REPORT_COUNT=100`, which
  also showed 15, was most likely the same display cap rather than the destroy race. To count
  stored reports, query the report endpoint for the run with `page=2&page_size=15`, or without
  a page size, instead of counting rows in the modal.
- Every log posted after the run has ended has its status overwritten to `Completed` by the
  backend, and the web UI's run log download drops all logs with that status. So the script's
  "accepted" and summary lines for a multi-report run are stored but do not appear in the
  downloaded log. Use the run's Report Details view, or query the log endpoint for the run, to
  see the outcome.

Conclusion:

- The 100 MB check inside EnclaveAPI applies only to the hosted sandbox, per request.
- In an enclave the limit is the backend's 100 MiB check on the decoded report JSON, enforced
  per report. There is no cumulative limit on any hop.
- Dev can fail earlier than that, with a 502 and no message, because its App Service worker
  runs out of memory while decoding large reports before the explicit check is reached.

### Scenario 5 - Many small reports with intermediate `In Progress` status (Dev / QA safe)

This is the scenario for environments whose App Service has little memory headroom, such as
Dev and QA, where a single large report already causes a worker OOM. It tests report *count*
rather than report *size*, and it exercises the intended contract: intermediate reports are
posted as `In Progress`, only the last one as `Completed`.

```
REPORT_SIZE_MB=1
REPORT_COUNT=100
REPORT_INTERMEDIATE_STATUS=InProgress   # the default; do not set it with a space in a run configuration
```

Planned total: 100 MB, but the backend only ever holds one 1 MB report in memory at a time.

Expected with an EnclaveAPI build that validates and tags `In Progress` reports (branch
`kurian/disk-mode-decryption`, commit `00c4739` or later): all 100 reports stored, the run ends
only after report 100, and no VM-destroy race because nothing ends the run early. Reports 1 to
99 are stored with status `In Progress` and report 100 with `Completed`. In the run log every
intermediate report is preceded by the EnclaveAPI line "Intermediate report received and
validation is starting"; the last one by "Report has been generated and validation is
starting". Report Details lists only the first 15 (see the display cap above).

Confirmed on Dev on 2026-11-16: 99 `In Progress` reports and 1 `Completed` report, all
accepted, 99 posts in about 65 seconds, run ended on report 100.

With an older EnclaveAPI build the same run still stores all 100 reports, but Report Details
lists only report 100, because only `Completed` reports were tagged `validated_report`. Query
the report endpoint for the run to confirm the other 99 arrived.

#### Variant: every report `In Progress`

```
REPORT_SIZE_MB=1
REPORT_COUNT=100
REPORT_FINAL_STATUS=InProgress
```

No report, and no harness log line, carries status `Completed`, so nothing the algorithm sends
ends the run. The run still ends: when the container exits with code 0 the VM runner drains
EnclaveAPI and posts its own `Completed` report ("The run has completed"), which sets the run's
end date and queues the destroy. Expected: all 100 reports stored as `In Progress`, the harness
summary lines visible in the run log (they are posted before the run ends, so their status is
not overwritten), and the run marked completed by the VM runner's report shortly after the
container exits. This is the shape of an algorithm that never posts a final report itself.

If Dev or QA still fails partway through, the failure is a per-request cost on the backend
(one report row, one base64 decode, one schema validation, and one Report Details query per
post), not a size limit. Bisect with `REPORT_COUNT` to find the count at which it breaks and
record the backend worker log at that time.

### Scenario 6 - One report in the middle of the sequence is rejected

Branch `develop/covid-report-failure-midrun`. The question is what the platform does when, say,
report 49 of 100 is rejected while the algorithm keeps going.

```
REPORT_SIZE_MB=1
REPORT_COUNT=100
REPORT_FAIL_AT=49            # or 49,50
REPORT_FAIL_MODE=schema      # the default
REPORT_FINAL_STATUS=Completed  # the default; use "In Progress" (quoted) for the all-In-Progress variant
```

Report 49 is posted with `"report"` set to a string instead of an object, which fails any schema
that types `report` as a dict. The harness treats the rejection as the expected outcome, logs it,
and continues with report 50. Its exit code and closing log status stay as for a clean run, so
everything that happens to the run afterwards is the platform's doing, not the harness's.

Observed on Dev on 2026-11-23 (EnclaveAPI 2.1.6, `Standard_DC4as_v5`, run configuration 3,
version 1), which matched the sequence predicted from the code (EnclaveAPI `PostApiV1Report`,
backend `frontoffice_log` and `frontoffice_report` views) except for point 5:

1. **Reports 1 to 48** were validated, tagged and stored as `In Progress`, as in scenario 5, at
   0.7 to 1.5 s per report.
2. **Report 49** was sent to Cerberus, failed, and EnclaveAPI answered `400`. The run log shows
   `AO: Your report failed schema validation: Schema validation failed: map[report:[must be of
   dict type]]` followed by the harness line `Report 49/100 was deliberately invalid (schema) and
   was rejected as intended: HTTP 400 Bad Request. Continuing with the next report.` The report
   was never forwarded, so there is no row for it.
3. **The run was marked failed right there.** The schema-validation log line is posted with
   status **`Failed`**, and the backend treats a `Failed` log line as an end state exactly like a
   `Failed` report: run status `run-failed`, end date set, failure notification and email, VM
   destroy pipeline queued. The downloaded log's `Report generation failed` and `Run failed`
   rows both carry the 12:32 PM timestamp of report 49, and the UI showed the **Run failed**
   badge while reports were still being accepted.
4. **Reports 50 onwards were still accepted and stored.** The harness kept posting, EnclaveAPI
   validated and forwarded them, and the backend stored them. Because the run had ended, the
   backend rewrote the status of every later report and log to the run's end status, so they are
   stored as `Failed` rather than `In Progress`. They remain tagged `validated_report`, so Report
   Details lists them (first 15 only, see above). Two side effects were visible:
   - Each post slowed to 1.5 to 5.8 s. After the end date the backend rewrites every incoming
     report and log to `Failed`, and `Failed` re-enters the end-state branch, so every post now
     also runs the notification path before returning "run already ended, will skip destroy".
     That branch also fires a fresh **Run Failed** notification per post. The email is guarded
     by a sent flag; the notification is not.
   - EnclaveAPI's `AO: Report N accepted and added to Report Details` counter is its own count
     of accepted reports, so after the rejection it runs one behind the harness index (`AO:
     Report 49 accepted` is harness report 50).
5. **The destroy did cut the run short.** The log ends at harness report 79, accepted at
   12:33 PM, about 60 to 90 s after the failure. Reports 80 to 100 were never sent: the
   algorithm died with the VM. The earlier assumption that the destroy pipeline takes minutes
   did not hold on Dev; combined with the post-failure slowdown, the remaining 51 reports needed
   about 2.5 minutes and got roughly half of that.
6. **Harness exit and VM runner** never happened: the process was killed before its closing log
   line, and the VM runner never reached its drain poll or its `Completed` report. On a run
   that does survive to the end, both would be stored with status rewritten to `Failed` and
   logged as "run already ended, will skip destroy".

Net effect: one schema-invalid report anywhere in the sequence fails the whole run, even though
every other report is stored. The final run status was **Run Failed**, with 48 `In Progress`
reports, no report 49, 30 reports (50 to 79) whose stored status is `Failed`, and 21 reports
never sent. The destroy also discards the algorithm's own tail: anything it would have logged or
posted after the VM went away is simply absent.

Contrast with `REPORT_FAIL_MODE=oversize` (not yet run): the size pre-check in EnclaveAPI
(`00c4739` or later) answers `413` and logs the size message with status **`In Progress`**, not
`Failed`. The run should continue, report 49 is simply missing, and the run ends normally on
report 100 as **Run Completed**. So today a schema failure fails the run and a size failure does
not. Whether a rejected intermediate report should fail a run at all is a platform decision; the
algorithm receives the 400 and could repair and resend, but the `Failed` log line has already
ended the run by the time it does.

The reports CSV export for the run (`Run Configuration 3_run_version_1_reports_csv.zip`) held
78 files of 1,024 KB each, numbered `report_1` to `report_78`: 48 from before the failure and 30
after it, report 49 absent. The export numbers stored reports in sequence, so `report_49` in the
zip is harness report 50. It confirms the count without the API and without the 15-row modal
cap, but it carries the report bodies only, not their stored status.

Still to check from the 2026-11-23 run: the stored status of reports 50 to 79 via the report
endpoint (expected `Failed`), the number of **Run Failed** notifications in the bell (expected
one per post after report 49, about 60), and whether exactly one failure email arrived. Reading
the run log in the UI mid-run shows only the first page of log lines under the synthetic
`Run failed` row, so use **Download logs** for the complete sequence.

The destroy timing is the variable to pin down before relying on anything in point 5. On this
Dev run the VM was gone 60 to 90 s after the failing report. The destroy pipeline notes under
"Two platform behaviours" above assume 5 to 10 minutes, which came from staging on 2026-11-03
and does not transfer to Dev.

### Finding the exact per-report limit

If scenario 1 rejects every report, bisect with `REPORT_COUNT=1` and decreasing
`REPORT_SIZE_MB` (for example 99, 95, 90) until a report is accepted. The reported
`full body` size in the log is the exact request size sent, including the `name` and `status`
fields.

## Troubleshooting

- **HTTP 413**: the request body exceeded a size limit on the API or a proxy in front of it.
- **HTTP 400 / 422**: the report was rejected for validation reasons rather than size; the
  response body in the log explains why.
- **Connection reset or timeout on large reports**: an intermediate proxy may be closing the
  connection before the API responds. The log shows the elapsed time at the point of failure.
- **Memory**: each report is built in memory. A 100 MB report needs roughly 300 MB of RAM
  while it is serialized and sent.

For technical support, contact BeeKeeperAI.
