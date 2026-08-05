"""SQLite-backed interpreter scheduling and language-access reporting."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, parseaddr
from pathlib import Path
from typing import Iterable


MODALITIES = ("in-person", "phone", "video")
WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
SCHEMA_VERSION = "1"


class ClinicError(Exception):
    """A user-facing validation or state error."""


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interpreters (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    contact TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interpreter_languages (
    interpreter_id INTEGER NOT NULL REFERENCES interpreters(id) ON DELETE CASCADE,
    language TEXT NOT NULL,
    PRIMARY KEY (interpreter_id, language)
);

CREATE TABLE IF NOT EXISTS interpreter_modalities (
    interpreter_id INTEGER NOT NULL REFERENCES interpreters(id) ON DELETE CASCADE,
    modality TEXT NOT NULL CHECK (modality IN ('in-person', 'phone', 'video')),
    PRIMARY KEY (interpreter_id, modality)
);

CREATE TABLE IF NOT EXISTS availability (
    id INTEGER PRIMARY KEY,
    interpreter_id INTEGER NOT NULL REFERENCES interpreters(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    CHECK (start_time < end_time),
    UNIQUE (interpreter_id, weekday, start_time, end_time)
);

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    patient_ref TEXT NOT NULL,
    patient_contact TEXT NOT NULL,
    language TEXT NOT NULL,
    modality TEXT NOT NULL CHECK (modality IN ('in-person', 'phone', 'video')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (
        status IN ('requested', 'scheduled', 'completed', 'cancelled',
                   'patient_no_show', 'interpreter_no_show')
    ),
    assigned_interpreter_id INTEGER REFERENCES interpreters(id),
    created_at TEXT NOT NULL,
    scheduled_at TEXT
);

CREATE INDEX IF NOT EXISTS appointments_time_idx
    ON appointments(starts_at, ends_at);
CREATE INDEX IF NOT EXISTS appointments_interpreter_idx
    ON appointments(assigned_interpreter_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS confirmations (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('patient', 'interpreter')),
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    output_path TEXT,
    UNIQUE (appointment_id, recipient_type)
);

CREATE TABLE IF NOT EXISTS no_show_events (
    id INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    party TEXT NOT NULL CHECK (party IN ('patient', 'interpreter')),
    note TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    UNIQUE (appointment_id)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: str, label: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ClinicError(f"{label} cannot be empty")
    return cleaned


def normalize_language(value: str) -> str:
    return normalize_text(value, "language").casefold()


def normalize_email(value: str, label: str) -> str:
    cleaned = normalize_text(value, label)
    display_name, address = parseaddr(cleaned)
    if display_name or address != cleaned or address.count("@") != 1:
        raise ClinicError(f"{label} must be a plain email address")
    local_part, domain = address.rsplit("@", 1)
    if not local_part or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ClinicError(f"{label} must be a plain email address")
    return address


def parse_local_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ClinicError(f"{label} must use YYYY-MM-DDTHH:MM") from exc
    return parsed


def parse_clock(value: str, label: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ClinicError(f"{label} must use HH:MM in 24-hour time") from exc


def parse_month(value: str) -> tuple[date, date]:
    try:
        start = datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise ClinicError("month must use YYYY-MM") from exc
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ClinicError(f"database does not exist: {db_path}; run init first")
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    version = db.execute(
        "SELECT value FROM settings WHERE key = 'schema_version'"
    ).fetchone()
    if version is None or version["value"] != SCHEMA_VERSION:
        db.close()
        raise ClinicError("database schema is missing or unsupported; run init")
    return db


def init_database(db_path: Path, clinic_name: str) -> dict:
    clinic_name = normalize_text(clinic_name, "clinic name")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO settings(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        db.execute(
            "INSERT INTO settings(key, value) VALUES('clinic_name', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (clinic_name,),
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('database_id', ?)",
            (uuid.uuid4().hex,),
        )
        db.commit()
    finally:
        db.close()
    try:
        db_path.chmod(0o600)
    except OSError as exc:
        raise ClinicError(f"cannot secure database file: {exc}") from exc
    return {"database": str(db_path), "clinic_name": clinic_name, "initialized": True}


def add_interpreter(
    db: sqlite3.Connection,
    name: str,
    contact: str,
    languages: Iterable[str],
    modalities: Iterable[str],
) -> dict:
    name = normalize_text(name, "interpreter name")
    contact = normalize_email(contact, "interpreter contact")
    language_set = sorted({normalize_language(value) for value in languages})
    modality_set = sorted(set(modalities))
    if not language_set:
        raise ClinicError("at least one --language is required")
    if not modality_set:
        raise ClinicError("at least one --modality is required")
    unknown = set(modality_set) - set(MODALITIES)
    if unknown:
        raise ClinicError(f"unsupported modality: {sorted(unknown)[0]}")

    with db:
        cursor = db.execute(
            "INSERT INTO interpreters(name, contact, created_at) VALUES(?, ?, ?)",
            (name, contact, utc_now()),
        )
        interpreter_id = cursor.lastrowid
        db.executemany(
            "INSERT INTO interpreter_languages(interpreter_id, language) VALUES(?, ?)",
            [(interpreter_id, language) for language in language_set],
        )
        db.executemany(
            "INSERT INTO interpreter_modalities(interpreter_id, modality) VALUES(?, ?)",
            [(interpreter_id, modality) for modality in modality_set],
        )
    return {
        "interpreter_id": interpreter_id,
        "name": name,
        "languages": language_set,
        "modalities": modality_set,
    }


def add_availability(
    db: sqlite3.Connection,
    interpreter_id: int,
    weekday_name: str,
    start_value: str,
    end_value: str,
) -> dict:
    weekday_key = weekday_name.casefold()
    if weekday_key not in WEEKDAYS:
        raise ClinicError("weekday must be a weekday name or three-letter abbreviation")
    start = parse_clock(start_value, "start")
    end = parse_clock(end_value, "end")
    if start >= end:
        raise ClinicError("availability end must be after start")
    interpreter = db.execute(
        "SELECT id FROM interpreters WHERE id = ? AND active = 1", (interpreter_id,)
    ).fetchone()
    if interpreter is None:
        raise ClinicError(f"active interpreter {interpreter_id} does not exist")
    try:
        with db:
            cursor = db.execute(
                "INSERT INTO availability(interpreter_id, weekday, start_time, end_time) "
                "VALUES(?, ?, ?, ?)",
                (interpreter_id, WEEKDAYS[weekday_key], start.strftime("%H:%M"), end.strftime("%H:%M")),
            )
    except sqlite3.IntegrityError as exc:
        raise ClinicError("that availability window already exists") from exc
    return {
        "availability_id": cursor.lastrowid,
        "interpreter_id": interpreter_id,
        "weekday": WEEKDAYS[weekday_key],
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
    }


def add_appointment(
    db: sqlite3.Connection,
    patient_ref: str,
    patient_contact: str,
    language: str,
    modality: str,
    starts_at: str,
    ends_at: str,
) -> dict:
    patient_ref = normalize_text(patient_ref, "patient reference")
    patient_contact = normalize_email(patient_contact, "patient contact")
    language = normalize_language(language)
    if modality not in MODALITIES:
        raise ClinicError(f"unsupported modality: {modality}")
    start = parse_local_datetime(starts_at, "start")
    end = parse_local_datetime(ends_at, "end")
    if start.date() != end.date():
        raise ClinicError("appointments must start and end on the same clinic day")
    if start >= end:
        raise ClinicError("appointment end must be after start")
    with db:
        cursor = db.execute(
            "INSERT INTO appointments(patient_ref, patient_contact, language, modality, "
            "starts_at, ends_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                patient_ref,
                patient_contact,
                language,
                modality,
                start.strftime("%Y-%m-%dT%H:%M"),
                end.strftime("%Y-%m-%dT%H:%M"),
                utc_now(),
            ),
        )
    return {
        "appointment_id": cursor.lastrowid,
        "status": "requested",
        "language": language,
        "modality": modality,
    }


def _eligible_candidates(db: sqlite3.Connection, appointment: sqlite3.Row) -> tuple[list[sqlite3.Row], dict]:
    start = parse_local_datetime(appointment["starts_at"], "stored start")
    weekday = start.weekday()
    start_clock = start.strftime("%H:%M")
    end_clock = parse_local_datetime(appointment["ends_at"], "stored end").strftime("%H:%M")
    interpreters = db.execute(
        "SELECT id, name, contact FROM interpreters WHERE active = 1 ORDER BY id"
    ).fetchall()
    failures: Counter[str] = Counter()
    candidates: list[sqlite3.Row] = []

    for interpreter in interpreters:
        if db.execute(
            "SELECT 1 FROM interpreter_languages WHERE interpreter_id = ? AND language = ?",
            (interpreter["id"], appointment["language"]),
        ).fetchone() is None:
            failures["language"] += 1
            continue
        if db.execute(
            "SELECT 1 FROM interpreter_modalities WHERE interpreter_id = ? AND modality = ?",
            (interpreter["id"], appointment["modality"]),
        ).fetchone() is None:
            failures["modality"] += 1
            continue
        if db.execute(
            "SELECT 1 FROM availability WHERE interpreter_id = ? AND weekday = ? "
            "AND start_time <= ? AND end_time >= ?",
            (interpreter["id"], weekday, start_clock, end_clock),
        ).fetchone() is None:
            failures["availability"] += 1
            continue
        if db.execute(
            "SELECT 1 FROM appointments WHERE assigned_interpreter_id = ? AND id != ? "
            "AND status IN ('scheduled', 'completed', 'patient_no_show', 'interpreter_no_show') "
            "AND starts_at < ? AND ends_at > ?",
            (interpreter["id"], appointment["id"], appointment["ends_at"], appointment["starts_at"]),
        ).fetchone() is not None:
            failures["conflict"] += 1
            continue
        candidates.append(interpreter)

    diagnostics = {
        "active_interpreters": len(interpreters),
        "failed_language": failures["language"],
        "failed_modality": failures["modality"],
        "failed_availability": failures["availability"],
        "failed_conflict": failures["conflict"],
    }
    return candidates, diagnostics


def _confirmation_messages(
    appointment: sqlite3.Row, interpreter: sqlite3.Row, clinic_name: str
) -> list[tuple[str, str, str, str]]:
    detail = (
        f"Clinic: {clinic_name}\n"
        f"Appointment: {appointment['starts_at']} to {appointment['ends_at']} local clinic time\n"
        f"Language: {appointment['language']}\n"
        f"Modality: {appointment['modality']}\n"
        f"Patient reference: {appointment['patient_ref']}"
    )
    patient_subject = f"Interpreter confirmed for appointment {appointment['id']}"
    patient_body = (
        f"{detail}\nInterpreter: {interpreter['name']}\n"
        "Contact the clinic if the appointment details change."
    )
    interpreter_subject = f"Interpretation assignment for appointment {appointment['id']}"
    interpreter_body = (
        f"{detail}\n"
        "Reply through the clinic's approved process if you cannot accept this assignment."
    )
    return [
        ("patient", appointment["patient_contact"], patient_subject, patient_body),
        ("interpreter", interpreter["contact"], interpreter_subject, interpreter_body),
    ]


def _optimal_batch_plan(
    db: sqlite3.Connection, appointments: list[sqlite3.Row]
) -> tuple[tuple[int, int], ...]:
    """Maximize assignments while respecting candidate eligibility and batch conflicts."""
    candidates_by_appointment: dict[int, tuple[int, ...]] = {}
    interpreter_ids: set[int] = set()
    load_by_id: dict[int, int] = {}
    for appointment in appointments:
        candidates, _ = _eligible_candidates(db, appointment)
        for candidate in candidates:
            interpreter_id = candidate["id"]
            interpreter_ids.add(interpreter_id)
            if interpreter_id not in load_by_id:
                load_by_id[interpreter_id] = db.execute(
                    "SELECT COUNT(*) AS count FROM appointments "
                    "WHERE assigned_interpreter_id = ? AND status != 'cancelled'",
                    (interpreter_id,),
                ).fetchone()["count"]
        candidates_by_appointment[appointment["id"]] = tuple(
            candidate["id"]
            for candidate in sorted(candidates, key=lambda item: (load_by_id[item["id"]], item["id"]))
        )

    ordered_interpreters = tuple(sorted(interpreter_ids))
    interpreter_index = {
        interpreter_id: index for index, interpreter_id in enumerate(ordered_interpreters)
    }
    empty_state = tuple("" for _ in ordered_interpreters)
    empty_counts = tuple(0 for _ in ordered_interpreters)
    states: dict[
        tuple[str, ...], tuple[int, int, tuple[int, ...], tuple[tuple[int, int], ...]]
    ] = {
        empty_state: (0, 0, empty_counts, ())
    }

    def is_better(
        candidate: tuple[int, int, tuple[int, ...], tuple[tuple[int, int], ...]],
        current: tuple[int, int, tuple[int, ...], tuple[tuple[int, int], ...]] | None,
    ) -> bool:
        if current is None:
            return True
        candidate_count, candidate_cost, _, candidate_plan = candidate
        current_count, current_cost, _, current_plan = current
        return (
            candidate_count > current_count
            or (
                candidate_count == current_count
                and (candidate_cost, candidate_plan) < (current_cost, current_plan)
            )
        )

    for appointment in appointments:
        normalized_states: dict[
            tuple[str, ...],
            tuple[int, int, tuple[int, ...], tuple[tuple[int, int], ...]],
        ] = {}
        for busy_until, value in states.items():
            normalized_busy = tuple(
                end if end > appointment["starts_at"] else "" for end in busy_until
            )
            if is_better(value, normalized_states.get(normalized_busy)):
                normalized_states[normalized_busy] = value

        next_states = dict(normalized_states)
        candidate_ids = candidates_by_appointment[appointment["id"]]
        for busy_until, (count, balance_cost, batch_counts, plan) in normalized_states.items():
            for interpreter_id in candidate_ids:
                slot = interpreter_index[interpreter_id]
                if busy_until[slot] > appointment["starts_at"]:
                    continue
                next_busy_list = list(busy_until)
                next_busy_list[slot] = appointment["ends_at"]
                next_busy = tuple(next_busy_list)
                next_counts_list = list(batch_counts)
                previous_load = load_by_id[interpreter_id] + next_counts_list[slot]
                next_counts_list[slot] += 1
                candidate_value = (
                    count + 1,
                    balance_cost + (2 * previous_load) + 1,
                    tuple(next_counts_list),
                    plan + ((appointment["id"], interpreter_id),),
                )
                if is_better(candidate_value, next_states.get(next_busy)):
                    next_states[next_busy] = candidate_value
        states = next_states

    best = None
    for value in states.values():
        if is_better(value, best):
            best = value
    return best[3] if best is not None else ()


def schedule(db: sqlite3.Connection, appointment_id: int | None = None) -> dict:
    db.execute("BEGIN IMMEDIATE")
    try:
        query = "SELECT * FROM appointments WHERE status = 'requested'"
        params: tuple[object, ...] = ()
        if appointment_id is not None:
            query += " AND id = ?"
            params = (appointment_id,)
        query += " ORDER BY starts_at, id"
        appointments = db.execute(query, params).fetchall()
        if appointment_id is not None and not appointments:
            existing = db.execute(
                "SELECT status FROM appointments WHERE id = ?", (appointment_id,)
            ).fetchone()
            if existing is None:
                raise ClinicError(f"appointment {appointment_id} does not exist")
            raise ClinicError(
                f"appointment {appointment_id} cannot be scheduled from status {existing['status']}"
            )
        clinic_name = db.execute(
            "SELECT value FROM settings WHERE key = 'clinic_name'"
        ).fetchone()["value"]
        plan = dict(_optimal_batch_plan(db, appointments))
        scheduled: list[dict] = []
        for appointment in appointments:
            interpreter_id = plan.get(appointment["id"])
            if interpreter_id is None:
                continue
            interpreter = db.execute(
                "SELECT id, name, contact FROM interpreters WHERE id = ?", (interpreter_id,)
            ).fetchone()
            timestamp = utc_now()
            db.execute(
                "UPDATE appointments SET status = 'scheduled', assigned_interpreter_id = ?, "
                "scheduled_at = ? WHERE id = ? AND status = 'requested'",
                (interpreter["id"], timestamp, appointment["id"]),
            )
            for recipient_type, recipient, subject, body in _confirmation_messages(
                appointment, interpreter, clinic_name
            ):
                db.execute(
                    "INSERT INTO confirmations(appointment_id, recipient_type, recipient, "
                    "subject, body, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (appointment["id"], recipient_type, recipient, subject, body, timestamp),
                )
            scheduled.append(
                {
                    "appointment_id": appointment["id"],
                    "interpreter_id": interpreter["id"],
                    "interpreter": interpreter["name"],
                }
            )
        unmatched: list[dict] = []
        for appointment in appointments:
            if appointment["id"] in plan:
                continue
            _, diagnostics = _eligible_candidates(db, appointment)
            unmatched.append({"appointment_id": appointment["id"], **diagnostics})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"scheduled": scheduled, "unmatched": unmatched}


def _mark_dispatched(
    db: sqlite3.Connection, confirmation_id: int, transport: str, output_path: str | None = None
) -> None:
    with db:
        db.execute(
            "UPDATE confirmations SET dispatched_at = ?, output_path = ? WHERE id = ?",
            (utc_now(), output_path or transport, confirmation_id),
        )


def dispatch_confirmations_via_sendmail(
    db: sqlite3.Connection, sendmail_path: Path, sender: str
) -> dict:
    sender = normalize_email(sender, "sender")
    try:
        resolved_sendmail = sendmail_path.resolve(strict=True)
    except OSError as exc:
        raise ClinicError(f"sendmail executable does not exist: {sendmail_path}") from exc
    if not resolved_sendmail.is_file() or not os.access(resolved_sendmail, os.X_OK):
        raise ClinicError(f"sendmail path is not an executable file: {resolved_sendmail}")
    clinic_now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    pending = db.execute(
        "SELECT c.* FROM confirmations c "
        "JOIN appointments a ON a.id = c.appointment_id "
        "WHERE c.dispatched_at IS NULL AND a.status = 'scheduled' AND a.starts_at > ? "
        "ORDER BY c.id",
        (clinic_now,),
    ).fetchall()
    database_id_row = db.execute(
        "SELECT value FROM settings WHERE key = 'database_id'"
    ).fetchone()
    if database_id_row is None:
        database_id = uuid.uuid4().hex
        with db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES('database_id', ?)", (database_id,)
            )
    else:
        database_id = database_id_row["value"]
    recipients: list[str] = []
    for message in pending:
        email = EmailMessage()
        email["From"] = sender
        email["To"] = message["recipient"]
        email["Subject"] = message["subject"]
        email["Date"] = format_datetime(datetime.fromisoformat(message["created_at"]))
        email["Message-ID"] = (
            f"<civic043-{database_id}-confirmation-{message['id']}@clinic.local>"
        )
        email.set_content(message["body"])
        content = email.as_bytes(policy=SMTP)
        try:
            result = subprocess.run(
                [str(resolved_sendmail), "-i", "-f", sender, "--", message["recipient"]],
                input=content,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClinicError(f"sendmail delivery failed for confirmation {message['id']}: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:300]
            raise ClinicError(
                f"sendmail rejected confirmation {message['id']} with exit {result.returncode}: {detail}"
            )
        _mark_dispatched(db, message["id"], f"sendmail:{resolved_sendmail}")
        recipients.append(message["recipient"])
    return {"dispatched": len(recipients), "transport": "sendmail", "recipients": recipients}


def record_no_show(
    db: sqlite3.Connection, appointment_id: int, party: str, note: str
) -> dict:
    appointment = db.execute(
        "SELECT status, assigned_interpreter_id, starts_at FROM appointments WHERE id = ?",
        (appointment_id,),
    ).fetchone()
    if appointment is None:
        raise ClinicError(f"appointment {appointment_id} does not exist")
    if appointment["status"] != "scheduled":
        raise ClinicError(
            f"no-show can only be recorded for scheduled appointments, found {appointment['status']}"
        )
    if parse_local_datetime(appointment["starts_at"], "stored start") > datetime.now():
        raise ClinicError("no-show cannot be recorded before the appointment starts")
    if party not in ("patient", "interpreter"):
        raise ClinicError("party must be patient or interpreter")
    if party == "interpreter" and appointment["assigned_interpreter_id"] is None:
        raise ClinicError("an interpreter no-show requires an assigned interpreter")
    status = f"{party}_no_show"
    with db:
        db.execute(
            "INSERT INTO no_show_events(appointment_id, party, note, recorded_at) VALUES(?, ?, ?, ?)",
            (appointment_id, party, note.strip(), utc_now()),
        )
        db.execute("UPDATE appointments SET status = ? WHERE id = ?", (status, appointment_id))
    return {"appointment_id": appointment_id, "status": status, "party": party}


def complete_appointment(db: sqlite3.Connection, appointment_id: int) -> dict:
    appointment = db.execute(
        "SELECT status, ends_at FROM appointments WHERE id = ?", (appointment_id,)
    ).fetchone()
    if appointment is None:
        raise ClinicError(f"appointment {appointment_id} does not exist")
    if appointment["status"] != "scheduled":
        raise ClinicError(
            f"appointment {appointment_id} cannot be completed from status {appointment['status']}"
        )
    if parse_local_datetime(appointment["ends_at"], "stored end") > datetime.now():
        raise ClinicError("appointment cannot be completed before it ends")
    with db:
        cursor = db.execute(
            "UPDATE appointments SET status = 'completed' WHERE id = ? AND status = 'scheduled'",
            (appointment_id,),
        )
    if cursor.rowcount != 1:
        raise ClinicError(f"appointment {appointment_id} changed while it was being completed")
    return {"appointment_id": appointment_id, "status": "completed"}


REPORT_FIELDS = (
    "report_month",
    "clinic_name",
    "scope",
    "language",
    "requested_visits",
    "assigned_visits",
    "completed_visits",
    "patient_no_shows",
    "interpreter_no_shows",
    "cancelled_visits",
    "unmatched_visits",
    "assignment_rate_percent",
    "delivered_access_visits",
    "delivered_access_rate_percent",
)


def _report_row(month: str, clinic_name: str, scope: str, language: str, rows: list[sqlite3.Row]) -> dict:
    total = len(rows)
    assigned = sum(row["assigned_interpreter_id"] is not None for row in rows)
    statuses = Counter(row["status"] for row in rows)
    return {
        "report_month": month,
        "clinic_name": clinic_name,
        "scope": scope,
        "language": language,
        "requested_visits": total,
        "assigned_visits": assigned,
        "completed_visits": statuses["completed"],
        "patient_no_shows": statuses["patient_no_show"],
        "interpreter_no_shows": statuses["interpreter_no_show"],
        "cancelled_visits": statuses["cancelled"],
        "unmatched_visits": sum(
            row["assigned_interpreter_id"] is None and row["status"] == "requested" for row in rows
        ),
        "assignment_rate_percent": f"{(assigned / total * 100) if total else 0:.1f}",
        "delivered_access_visits": statuses["completed"],
        "delivered_access_rate_percent": (
            f"{(statuses['completed'] / total * 100) if total else 0:.1f}"
        ),
    }


def monthly_report(db: sqlite3.Connection, month: str, output: Path) -> dict:
    start, end = parse_month(month)
    rows = db.execute(
        "SELECT status, language, assigned_interpreter_id FROM appointments "
        "WHERE starts_at >= ? AND starts_at < ? ORDER BY starts_at, id",
        (f"{start.isoformat()}T00:00", f"{end.isoformat()}T00:00"),
    ).fetchall()
    clinic_name = db.execute(
        "SELECT value FROM settings WHERE key = 'clinic_name'"
    ).fetchone()["value"]
    by_language: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_language[row["language"]].append(row)
    report_rows = [_report_row(month, clinic_name, "all", "ALL", rows)]
    report_rows.extend(
        _report_row(month, clinic_name, "language", language, by_language[language])
        for language in sorted(by_language)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
            writer.writeheader()
            writer.writerows(report_rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ClinicError(f"could not write report: {exc}") from exc
    return {"output": str(output), "month": month, "appointments": len(rows), "rows": len(report_rows)}


def list_appointments(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute(
        "SELECT a.id, a.patient_ref, a.language, a.modality, a.starts_at, a.ends_at, "
        "a.status, a.assigned_interpreter_id, i.name AS interpreter "
        "FROM appointments a LEFT JOIN interpreters i ON i.id = a.assigned_interpreter_id "
        "ORDER BY a.starts_at, a.id"
    ).fetchall()
    return [dict(row) for row in rows]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Schedule language interpreters for a rural clinic"
    )
    parser.add_argument(
        "--db", type=Path, default=Path("clinic.db"), help="SQLite database path"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize or update the clinic database")
    init.add_argument("--clinic-name", required=True)

    interpreter = commands.add_parser("add-interpreter", help="register a contract interpreter")
    interpreter.add_argument("--name", required=True)
    interpreter.add_argument("--contact", required=True)
    interpreter.add_argument("--language", action="append", required=True)
    interpreter.add_argument("--modality", action="append", choices=MODALITIES, required=True)

    availability = commands.add_parser("add-availability", help="add recurring weekly availability")
    availability.add_argument("--interpreter", type=int, required=True)
    availability.add_argument("--weekday", required=True)
    availability.add_argument("--start", required=True)
    availability.add_argument("--end", required=True)

    appointment = commands.add_parser("add-appointment", help="request interpreter coverage")
    appointment.add_argument("--patient-ref", required=True)
    appointment.add_argument("--patient-contact", required=True)
    appointment.add_argument("--language", required=True)
    appointment.add_argument("--modality", choices=MODALITIES, required=True)
    appointment.add_argument("--start", required=True)
    appointment.add_argument("--end", required=True)

    schedule_command = commands.add_parser("schedule", help="assign eligible available interpreters")
    schedule_command.add_argument("--appointment", type=int)

    dispatch = commands.add_parser(
        "dispatch-confirmations", help="send pending confirmations through a local mail transfer agent"
    )
    dispatch.add_argument("--sendmail", type=Path, required=True)
    dispatch.add_argument("--sender", required=True)

    no_show = commands.add_parser("record-no-show", help="record a patient or interpreter no-show")
    no_show.add_argument("--appointment", type=int, required=True)
    no_show.add_argument("--party", choices=("patient", "interpreter"), required=True)
    no_show.add_argument("--note", default="")

    complete = commands.add_parser("complete", help="mark a scheduled appointment completed")
    complete.add_argument("--appointment", type=int, required=True)

    report = commands.add_parser("monthly-report", help="write a monthly Title VI documentation CSV")
    report.add_argument("--month", required=True)
    report.add_argument("--output", type=Path, required=True)

    commands.add_parser("list-appointments", help="show appointments and assignment status")
    return parser


def run_command(args: argparse.Namespace) -> object:
    if args.command == "init":
        return init_database(args.db, args.clinic_name)
    db = connect(args.db)
    try:
        if args.command == "add-interpreter":
            return add_interpreter(db, args.name, args.contact, args.language, args.modality)
        if args.command == "add-availability":
            return add_availability(db, args.interpreter, args.weekday, args.start, args.end)
        if args.command == "add-appointment":
            return add_appointment(
                db,
                args.patient_ref,
                args.patient_contact,
                args.language,
                args.modality,
                args.start,
                args.end,
            )
        if args.command == "schedule":
            return schedule(db, args.appointment)
        if args.command == "dispatch-confirmations":
            return dispatch_confirmations_via_sendmail(db, args.sendmail, args.sender)
        if args.command == "record-no-show":
            return record_no_show(db, args.appointment, args.party, args.note)
        if args.command == "complete":
            return complete_appointment(db, args.appointment)
        if args.command == "monthly-report":
            return monthly_report(db, args.month, args.output)
        if args.command == "list-appointments":
            return list_appointments(db)
        raise ClinicError(f"unknown command: {args.command}")
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_command(args)
    except (ClinicError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
