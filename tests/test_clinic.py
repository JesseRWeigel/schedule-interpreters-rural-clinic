import csv
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.clinic import (
    ClinicError,
    add_appointment,
    add_availability,
    add_interpreter,
    connect,
    dispatch_confirmations,
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

        outbox = self.root / "outbox"
        dispatch = dispatch_confirmations(self.db, outbox)
        self.assertEqual(dispatch["dispatched"], 2)
        self.assertEqual(dispatch_confirmations(self.db, outbox)["dispatched"], 0)
        for path_text in dispatch["files"]:
            path = Path(path_text)
            self.assertTrue(path.read_text(encoding="utf-8").startswith("Destination:"))
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
        self.assertEqual(overall["access_rate_percent"], "50.0")

    def test_invalid_appointment_and_no_show_state_fail_closed(self):
        with self.assertRaises(ClinicError):
            self.add_appointment(end="2026-07-06T08:00")
        requested = self.add_appointment()
        with self.assertRaises(ClinicError):
            record_no_show(self.db, requested, "patient", "")


if __name__ == "__main__":
    unittest.main()
