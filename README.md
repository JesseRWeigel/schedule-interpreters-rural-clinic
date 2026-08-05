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

```bash
python3 clinic_scheduler.py --help
```

The verification command is defined before implementation:

```bash
bash scripts/verify.sh
```

## Status

**NOT YET VERIFIED.** No verify command has been run against this project.

A maintainer or agent must replace this section with the pasted output of the verify
command. Per `AGENTS.md`, a project is not done until that output appears here and
`tools/logrun.py` has recorded exit code 0.

## Unfinished

- Everything.
