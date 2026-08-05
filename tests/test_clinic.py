import csv
import stat
import tempfile
import unittest
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path

from src.clinic import (
    ClinicError,
    add_appointment,
    add_availability,
    add_interpreter,
    complete_appointment,
    connect,
    dispatch_confirmations_to_maildir,
    init_database,
    monthly_report,
    record_no_show,
    schedule,
)


class ClinicSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "clinic.db"
        init_database(self.db_path, "Test Rural Clinic")
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)
        self.db = connect(self.db_path)

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def add_interpreter(
        self,
        languages=("spanish",),
        modalities=("video",),
        start="08:00",
        end="17:00",
    ):
        result = add_interpreter(
            self.db,
            "Interpreter One",
            "interpreter-1@clinic.test",
            languages,
            modalities,
        )
        add_availability(self.db, result["interpreter_id"], "monday", start, end)
        return result["interpreter_id"]

    def add_appointment(
        self,
        language="spanish",
        modality="video",
        start="2026-07-06T09:00",
        end="2026-07-06T10:00",
        patient_ref="PATIENT-001",
    ):
        return add_appointment(
            self.db,
            patient_ref,
            f"{patient_ref.casefold()}@clinic.test",
            language,
            modality,
            start,
            end,
        )["appointment_id"]

    def test_matches_language_case_modality_and_availability(self):
        interpreter_id = self.add_interpreter(languages=("  Spanish ", "mam"))
        appointment_id = self.add_appointment(language="SPANISH")

        result = schedule(self.db, appointment_id)

        self.assertEqual(
            result["scheduled"],
            [
                {
                    "appointment_id": appointment_id,
                    "interpreter_id": interpreter_id,
                    "interpreter": "Interpreter One",
                }
            ],
        )
        self.assertEqual(result["unmatched"], [])
        confirmation_count = self.db.execute(
            "SELECT COUNT(*) FROM confirmations WHERE appointment_id = ?", (appointment_id,)
        ).fetchone()[0]
        self.assertEqual(confirmation_count, 2)

    def test_rejects_wrong_modality_and_time(self):
        self.add_interpreter(modalities=("video",), start="08:00", end="10:00")
        wrong_modality = self.add_appointment(modality="phone")
        outside_hours = self.add_appointment(
            start="2026-07-06T10:00",
            end="2026-07-06T11:00",
            patient_ref="PATIENT-002",
        )

        result = schedule(self.db)

        self.assertEqual(result["scheduled"], [])
        diagnostics = {item["appointment_id"]: item for item in result["unmatched"]}
        self.assertEqual(diagnostics[wrong_modality]["failed_modality"], 1)
        self.assertEqual(diagnostics[outside_hours]["failed_availability"], 1)

    def test_overlap_is_blocked_and_adjacent_visit_is_allowed(self):
        interpreter_id = self.add_interpreter()
        first = self.add_appointment()
        overlap = self.add_appointment(
            start="2026-07-06T09:30",
            end="2026-07-06T10:30",
            patient_ref="PATIENT-002",
        )
        adjacent = self.add_appointment(
            start="2026-07-06T10:00",
            end="2026-07-06T11:00",
            patient_ref="PATIENT-003",
        )

        first_result = schedule(self.db, first)
        remaining_result = schedule(self.db)

        self.assertEqual(first_result["scheduled"][0]["interpreter_id"], interpreter_id)
        self.assertEqual(
            [item["appointment_id"] for item in remaining_result["scheduled"]], [adjacent]
        )
        self.assertEqual(
            [item["appointment_id"] for item in remaining_result["unmatched"]], [overlap]
        )
        self.assertEqual(remaining_result["unmatched"][0]["failed_conflict"], 1)

    def test_dispatch_no_show_and_monthly_report(self):
        self.add_interpreter()
        assigned = self.add_appointment()
        unmatched = self.add_appointment(
            language="mam", patient_ref="PATIENT-002", start="2026-07-06T11:00", end="2026-07-06T12:00"
        )
        schedule_result = schedule(self.db)
        self.assertEqual([item["appointment_id"] for item in schedule_result["scheduled"]], [assigned])
        self.assertEqual([item["appointment_id"] for item in schedule_result["unmatched"]], [unmatched])

        maildir = self.root / "maildir"
        dispatch = dispatch_confirmations_to_maildir(
            self.db, maildir, "clinic@clinic.test"
        )
        self.assertEqual(dispatch["dispatched"], 2)
        self.assertEqual(
            dispatch_confirmations_to_maildir(
                self.db, maildir, "clinic@clinic.test"
            )["dispatched"],
            0,
        )
        self.assertEqual(stat.S_IMODE(maildir.stat().st_mode), 0o700)
        for path_text in dispatch["files"]:
            path = Path(path_text)
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            self.assertEqual(message["From"], "clinic@clinic.test")
            self.assertIn("@clinic.test", message["To"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        event = record_no_show(self.db, assigned, "patient", "Clinic follow-up required")
        self.assertEqual(event["status"], "patient_no_show")
        report_path = self.root / "title-vi.csv"
        report = monthly_report(self.db, "2026-07", report_path)
        self.assertEqual(report["appointments"], 2)
        with report_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        overall = rows[0]
        self.assertEqual(overall["scope"], "all")
        self.assertEqual(overall["requested_visits"], "2")
        self.assertEqual(overall["assigned_visits"], "1")
        self.assertEqual(overall["patient_no_shows"], "1")
        self.assertEqual(overall["unmatched_visits"], "1")
        self.assertEqual(overall["assignment_rate_percent"], "50.0")
        self.assertEqual(overall["delivered_access_visits"], "0")
        self.assertEqual(overall["delivered_access_rate_percent"], "0.0")

    def test_batch_plan_preserves_flexible_interpreter_for_mam(self):
        flexible = self.add_interpreter(languages=("spanish", "mam"))
        spanish_only = add_interpreter(
            self.db,
            "Interpreter Two",
            "interpreter-2@clinic.test",
            ("spanish",),
            ("video",),
        )["interpreter_id"]
        add_availability(self.db, spanish_only, "monday", "08:00", "17:00")
        spanish_visit = self.add_appointment(language="spanish", patient_ref="PATIENT-001")
        mam_visit = self.add_appointment(language="mam", patient_ref="PATIENT-002")

        result = schedule(self.db)

        assignments = {
            row["appointment_id"]: row["interpreter_id"] for row in result["scheduled"]
        }
        self.assertEqual(assignments, {spanish_visit: spanish_only, mam_visit: flexible})
        self.assertEqual(result["unmatched"], [])

    def test_interpreter_no_show_is_not_delivered_access(self):
        self.add_interpreter()
        appointment_id = self.add_appointment()
        schedule(self.db)
        record_no_show(self.db, appointment_id, "interpreter", "Backup unavailable")

        report_path = self.root / "interpreter-no-show.csv"
        monthly_report(self.db, "2026-07", report_path)
        with report_path.open(newline="", encoding="utf-8") as handle:
            overall = next(csv.DictReader(handle))
        self.assertEqual(overall["assigned_visits"], "1")
        self.assertEqual(overall["assignment_rate_percent"], "100.0")
        self.assertEqual(overall["interpreter_no_shows"], "1")
        self.assertEqual(overall["delivered_access_visits"], "0")
        self.assertEqual(overall["delivered_access_rate_percent"], "0.0")

    def test_large_adjacent_batch_does_not_recurse(self):
        self.add_interpreter(start="00:00", end="23:59")
        first_start = datetime(2026, 7, 6, 8, 0)
        for index in range(600):
            start = first_start + timedelta(minutes=index)
            end = start + timedelta(minutes=1)
            add_appointment(
                self.db,
                f"PATIENT-{index:03d}",
                f"patient-{index:03d}@clinic.test",
                "spanish",
                "video",
                start.strftime("%Y-%m-%dT%H:%M"),
                end.strftime("%Y-%m-%dT%H:%M"),
            )

        result = schedule(self.db)

        self.assertEqual(len(result["scheduled"]), 600)
        self.assertEqual(result["unmatched"], [])

    def test_future_outcomes_are_rejected(self):
        future = datetime(2099, 1, 1, 9, 0)
        future += timedelta(days=(7 - future.weekday()) % 7)
        self.add_interpreter()
        appointment_id = self.add_appointment(
            start=future.strftime("%Y-%m-%dT%H:%M"),
            end=(future + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        )
        schedule(self.db)

        with self.assertRaises(ClinicError):
            record_no_show(self.db, appointment_id, "patient", "")
        with self.assertRaises(ClinicError):
            complete_appointment(self.db, appointment_id)

    def test_stale_confirmations_are_not_dispatched(self):
        self.add_interpreter()
        appointment_id = self.add_appointment(
            start="2000-01-03T09:00", end="2000-01-03T10:00"
        )
        schedule(self.db)
        record_no_show(self.db, appointment_id, "patient", "Visit did not occur")

        maildir = self.root / "stale-maildir"
        result = dispatch_confirmations_to_maildir(
            self.db, maildir, "clinic@clinic.test"
        )

        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(list((maildir / "new").iterdir()), [])

    def test_maildir_names_are_unique_across_databases(self):
        self.add_interpreter()
        self.add_appointment()
        schedule(self.db)
        maildir = self.root / "shared-maildir"
        first = dispatch_confirmations_to_maildir(
            self.db, maildir, "clinic@clinic.test"
        )

        second_path = self.root / "replacement.db"
        init_database(second_path, "Replacement Rural Clinic")
        second_db = connect(second_path)
        try:
            second_interpreter = add_interpreter(
                second_db,
                "Interpreter Two",
                "interpreter-2@clinic.test",
                ("spanish",),
                ("video",),
            )["interpreter_id"]
            add_availability(second_db, second_interpreter, "monday", "08:00", "17:00")
            add_appointment(
                second_db,
                "PATIENT-002",
                "patient-002@clinic.test",
                "spanish",
                "video",
                "2026-07-06T11:00",
                "2026-07-06T12:00",
            )
            schedule(second_db)
            second = dispatch_confirmations_to_maildir(
                second_db, maildir, "clinic@clinic.test"
            )
            first_id = self.db.execute(
                "SELECT value FROM settings WHERE key = 'database_id'"
            ).fetchone()[0]
            second_id = second_db.execute(
                "SELECT value FROM settings WHERE key = 'database_id'"
            ).fetchone()[0]
        finally:
            second_db.close()

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(first["dispatched"], 2)
        self.assertEqual(second["dispatched"], 2)
        self.assertEqual(len(list((maildir / "new").iterdir())), 4)

    def test_invalid_appointment_and_no_show_state_fail_closed(self):
        with self.assertRaises(ClinicError):
            self.add_appointment(end="2026-07-06T08:00")
        requested = self.add_appointment()
        with self.assertRaises(ClinicError):
            record_no_show(self.db, requested, "patient", "")


if __name__ == "__main__":
    unittest.main()
