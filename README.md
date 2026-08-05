# Rural Clinic Interpreter Scheduler

Clinics serving immigrant patients juggle a handful of contract interpreters across languages and appointment times, and a missed match means a canceled visit. Build a scheduler that matches appointment language, modality (in person, phone, video), and interpreter availability, sends confirmations, tracks no-shows, and produces a monthly language-access report for Title VI documentation.

Catalog task: `CIVIC-043`. Part of [thousand](../../README.md).

## What this is

A local command-line scheduler for rural clinics coordinating contract interpreters. It stores
interpreters, recurring availability, and appointments in SQLite. Scheduling checks language,
modality, availability, and appointment conflicts. Assignments create patient and interpreter
confirmations, no-shows remain auditable, and monthly CSV reports distinguish assignment from
delivered access for Title VI documentation.

The scheduler treats availability and appointment times as local clinic time. End times are
exclusive, so adjacent appointments can use the same interpreter. Confirmation delivery uses a
standard clinic-controlled Maildir. Messages move atomically into its `new` directory as RFC 5322
email with mode `0600`, ready for a local mail agent or authorized staff workflow.

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
python3 clinic_scheduler.py --db clinic.db dispatch-confirmations \
  --maildir maildir --sender clinic@clinic.test
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
language, modality, availability, and conflicts. Batch scheduling maximizes the number of covered
visits before applying deterministic load and identifier tie breakers. Unmatched appointments
remain requested so staff can change coverage or contact the patient.

Interpreter and patient contact values must be plain email addresses. Outcome commands reject
future appointments, and the dispatcher only sends confirmations for visits that are still
scheduled. Pending messages remain in the database as audit evidence when an outcome makes them
obsolete.

Confirmation files and the SQLite database contain patient references and contact details. The
tool creates them with mode `0600` and secures Maildir folders with mode `0700`. Store their parent
directories in a clinic-approved location with access controls. The aggregate report exposes both
assignment rate and delivered-access rate. Only completed visits count as delivered access, so an
interpreter no-show never raises that measure. The clinic remains responsible for its legal review
and Title VI compliance process.

Run the complete verification from the repository root:

```bash
bash scripts/verify.sh
```

## Status

```text
test_batch_plan_preserves_flexible_interpreter_for_mam (test_clinic.ClinicSchedulerTests.test_batch_plan_preserves_flexible_interpreter_for_mam) ... ok
test_dispatch_no_show_and_monthly_report (test_clinic.ClinicSchedulerTests.test_dispatch_no_show_and_monthly_report) ... ok
test_future_outcomes_are_rejected (test_clinic.ClinicSchedulerTests.test_future_outcomes_are_rejected) ... ok
test_interpreter_no_show_is_not_delivered_access (test_clinic.ClinicSchedulerTests.test_interpreter_no_show_is_not_delivered_access) ... ok
test_invalid_appointment_and_no_show_state_fail_closed (test_clinic.ClinicSchedulerTests.test_invalid_appointment_and_no_show_state_fail_closed) ... ok
test_large_adjacent_batch_does_not_recurse (test_clinic.ClinicSchedulerTests.test_large_adjacent_batch_does_not_recurse) ... ok
test_maildir_names_are_unique_across_databases (test_clinic.ClinicSchedulerTests.test_maildir_names_are_unique_across_databases) ... ok
test_matches_language_case_modality_and_availability (test_clinic.ClinicSchedulerTests.test_matches_language_case_modality_and_availability) ... ok
test_overlap_is_blocked_and_adjacent_visit_is_allowed (test_clinic.ClinicSchedulerTests.test_overlap_is_blocked_and_adjacent_visit_is_allowed) ... ok
test_rejects_wrong_modality_and_time (test_clinic.ClinicSchedulerTests.test_rejects_wrong_modality_and_time) ... ok
test_stale_confirmations_are_not_dispatched (test_clinic.ClinicSchedulerTests.test_stale_confirmations_are_not_dispatched) ... ok

----------------------------------------------------------------------
Ran 11 tests in 3.408s

OK
WORKFLOW PASS: assignment, Maildir delivery, no-show, secure database, and report
SOURCE AUDIT PASS: 8 readable files, no sensitive literals
README PASS: status and observed success line are present
VERIFY PASS: 11 unit tests and full CLI workflow passed
```

## Unfinished

- No known functional gaps in the catalog specification.
- Direct SMTP and SMS transports are not built in. Maildir delivery is the supported confirmation
  channel and can feed a clinic's existing local mail transport without service credentials.
