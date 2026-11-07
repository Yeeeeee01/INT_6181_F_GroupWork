"""D-part regression tests for invoice validation and manual OCR fallback."""

from contextlib import redirect_stdout
from datetime import date, timedelta
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import database
import demo_B
import init_database
import local_ocr
import ocr
import storage
import validate
from manual_input import CATEGORIES, collect_invoice as real_collect_invoice


class InvoiceValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "invoices.sqlite3")
        init_database.init_database(self.db_path)
        self.record = {
            "invoice_no": "00123456",
            "date": date.today().isoformat(),
            "amount": 81.0,
            "category": "餐饮",
            "reimburser": "测试员工",
            "is_edited": False,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_invalid_invoice_number_is_reported(self):
        for invoice_no, expected_error in (
            (12345678, "发票号必须是字符串。"),
            ("   ", "发票号不能为空。"),
        ):
            with self.subTest(invoice_no=invoice_no):
                errors = validate.check(
                    {**self.record, "invoice_no": invoice_no},
                    db_path=self.db_path,
                )
                self.assertIn(expected_error, errors)

    def test_future_invoice_date_is_rejected(self):
        future_date = (date.today() + timedelta(days=1)).isoformat()

        errors = validate.check(
            {**self.record, "date": future_date},
            db_path=self.db_path,
        )

        self.assertIn("日期不能是未来日期。", errors)

    def test_amount_must_be_present_and_numeric(self):
        cases = (
            ("", "金额不能为空。"),
            ("not-a-number", "金额必须是数字。"),
            (float("inf"), "金额必须是有效数字。"),
        )
        for amount, expected_error in cases:
            with self.subTest(amount=amount):
                errors = validate.check(
                    {**self.record, "amount": amount},
                    db_path=self.db_path,
                )
                self.assertIn(expected_error, errors)

    def test_zero_and_large_amounts_follow_current_unrestricted_policy(self):
        for amount in (0, 10000, 12000.50):
            with self.subTest(amount=amount):
                errors = validate.check(
                    {**self.record, "amount": amount},
                    db_path=self.db_path,
                )
                self.assertEqual(errors, [])

    def test_duplicate_invoice_is_rejected(self):
        self.assertTrue(database.insert(self.record, "", db_path=self.db_path))

        with self.assertRaises(storage.InvoiceValidationError) as context:
            storage.save_invoice(self.record, db_path=self.db_path)

        self.assertIn(
            "该发票号已存在，不允许重复入库。",
            context.exception.errors,
        )
        self.assertEqual(len(database.query_all(self.db_path)), 1)


class OcrFallbackTests(unittest.TestCase):
    def test_rapidocr_v3_output_is_normalized_for_existing_parsers(self):
        boxes = ([[0, 0], [20, 0], [20, 10], [0, 10]],)
        output = SimpleNamespace(
            boxes=boxes,
            txts=("发票号码:00123456",),
            scores=(0.99,),
            elapse_list=[0.1],
        )
        engine = Mock(return_value=output)
        with patch.object(local_ocr, "_get_engine", return_value=engine):
            recognized, timings = local_ocr._run_engine("invoice.jpg")

        self.assertEqual(len(recognized), 1)
        self.assertEqual(recognized[0][1], "发票号码:00123456")
        self.assertEqual(recognized[0][2], 0.99)
        self.assertEqual(timings, [0.1])

    def test_failed_ocr_uses_manual_entry_without_network_or_crash(self):
        failed_scan = ocr.RecognitionResult(
            record=None,
            error_code="IMAGE_UNREADABLE",
            message="无法读取图片",
        )
        answers = iter(("00123456", "2024-02-29", "81.00", "1"))

        def collect_manually(initial):
            return real_collect_invoice(
                initial,
                input_fn=lambda _prompt: next(answers),
                output_fn=lambda _message: None,
            )

        with (
            patch(
                "demo_B.ocr.recognize_detailed",
                return_value=failed_scan,
            ) as recognize,
            patch(
                "demo_B.collect_invoice",
                side_effect=collect_manually,
            ) as collect,
            patch("demo_B._print_result") as print_result,
            redirect_stdout(io.StringIO()),
        ):
            exit_code = demo_B.main(["--image", "unreadable.jpg"])

        self.assertEqual(exit_code, 0)
        recognize.assert_called_once_with(Path("unreadable.jpg"))
        collect.assert_called_once_with(None)
        payload = print_result.call_args.args[0]
        self.assertFalse(payload["ocr_success"])
        self.assertEqual(payload["source"], "manual")
        self.assertEqual(payload["record"]["invoice_no"], "00123456")
        self.assertEqual(payload["record"]["category"], CATEGORIES[0])


if __name__ == "__main__":
    unittest.main()
