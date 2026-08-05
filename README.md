# Rural Clinic Interpreter Scheduler

Clinics serving immigrant patients juggle a handful of contract interpreters across languages and appointment times, and a missed match means a canceled visit. Build a scheduler that matches appointment language, modality (in person, phone, video), and interpreter availability, sends confirmations, tracks no-shows, and produces a monthly language-access report for Title VI documentation.

Catalog task: `CIVIC-043`. Part of [thousand](../../README.md).

## What this is

A local command-line scheduler for rural clinics coordinating contract interpreters. It stores
interpreters, recurring availability, and appointments in SQLite. Scheduling checks language,
modality, availability, and appointment conflicts. Assignments create patient and interpreter
confirmations, no-shows remain auditable, and monthly CSV reports summarize language access for
Title VI documentation.

The scheduler treats availability and appointment times as local clinic time. End times are
exclusive, so adjacent appointments can use the same interpreter. Confirmation delivery means
writing durable message files to a clinic-controlled outbox. Staff can print, email, or otherwise
route those files through an approved communication system without storing service credentials.

## Running it

Python 3.10 or newer is the only runtime dependency. Start a database and register an
interpreter:

```bash
python3 clinic_scheduler.py --db clinic.db init --clinic-name "Valley Rural Clinic"
python3 clinic_scheduler.py --db clinic.db add-interpreter \
  --name "Interpreter One" \
  --contact "interpreter-1@clinic.test" \
  --language Spanish \
  --language Mam \
  --modality phone \
  --modality video
python3 clinic_scheduler.py --db clinic.db add-availability \
  --interpreter 1 --weekday Monday --start 08:00 --end 17:00
```

Request coverage, schedule it, and deliver the generated confirmations:

```bash
python3 clinic_scheduler.py --db clinic.db add-appointment \
  --patient-ref PATIENT-001 \
  --patient-contact patient-001@clinic.test \
  --language Spanish \
  --modality video \
  --start 2026-07-06T09:00 \
  --end 2026-07-06T10:00
python3 clinic_scheduler.py --db clinic.db schedule
python3 clinic_scheduler.py --db clinic.db dispatch-confirmations --outbox outbox
```

Record outcomes and generate the monthly language-access report:

```bash
python3 clinic_scheduler.py --db clinic.db complete --appointment 1
python3 clinic_scheduler.py --db clinic.db record-no-show \
  --appointment 2 --party patient --note "Clinic follow-up required"
python3 clinic_scheduler.py --db clinic.db monthly-report \
  --month 2026-07 --output reports/title-vi-2026-07.csv
python3 clinic_scheduler.py --db clinic.db list-appointments
```

Each command returns JSON. Scheduling unmatched appointments returns diagnostic counts for
language, modality, availability, and conflicts. Those appointments remain requested so staff
can change coverage or contact the patient. Confirmation files and the SQLite database contain
patient references and contact details. Store both in a clinic-approved location with access
controls. The report is aggregate operational documentation. The clinic remains responsible for
its legal review and Title VI compliance process.

Run the complete verification from the repository root:

```bash
bash scripts/verify.sh
```

## Status

```text
test_dispatch_no_show_and_monthly_report (test_clinic.ClinicSchedulerTests.test_dispatch_no_show_and_monthly_report) ... ok
test_invalid_appointment_and_no_show_state_fail_closed (test_clinic.ClinicSchedulerTests.test_invalid_appointment_and_no_show_state_fail_closed) ... ok
test_matches_language_case_modality_and_availability (test_clinic.ClinicSchedulerTests.test_matches_language_case_modality_and_availability) ... ok
test_overlap_is_blocked_and_adjacent_visit_is_allowed (test_clinic.ClinicSchedulerTests.test_overlap_is_blocked_and_adjacent_visit_is_allowed) ... ok
test_rejects_wrong_modality_and_time (test_clinic.ClinicSchedulerTests.test_rejects_wrong_modality_and_time) ... ok

----------------------------------------------------------------------
Ran 5 tests in 0.297s

OK
WORKFLOW PASS: assignment, unmatched diagnosis, confirmations, no-show, and report
SOURCE AUDIT PASS: 8 readable files, no sensitive literals
README PASS: status and observed success line are present
VERIFY PASS: 5 unit tests and full CLI workflow passed
```

## Unfinished

- No known functional gaps in the catalog specification.
- Confirmation delivery ends at secure local outbox files. Routing those files through a clinic's
  approved email, SMS, print, or phone workflow is an operational integration outside this tool.
