import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import database
import init_database
import local_ocr
import main
import ocr
import pdf_support
import query
import storage
import validate
from manual_input import collect_invoice
from PIL import Image


class ApplicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = str(self.root / "invoices.sqlite3")
        self.photos_dir = self.root / "photos"
        init_database.init_database(self.db_path)
        self.record = {
            "invoice_no": "00123456",
            "date": "2026-11-06",
            "amount": 81.0,
            "category": "餐饮",
            "reimburser": "测试员工",
            "is_edited": True,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_about_tab_follows_employee_tab(self):
        app = main.InvoiceApp.__new__(main.InvoiceApp)
        app.root = object()
        notebook = Mock()
        frames = [object() for _ in range(4)]
        app._build_entry_tab = Mock()
        app._build_query_tab = Mock()
        app._build_employee_tab = Mock()
        app._build_about_tab = Mock()

        with (
            patch.object(main.ttk, "Notebook", return_value=notebook),
            patch.object(main.ttk, "Frame", side_effect=frames),
        ):
            app._build_tabs()

        self.assertEqual(
            [call.kwargs["text"] for call in notebook.add.call_args_list],
            ["发票录入", "发票查询与统计", "员工管理", "关于"],
        )
        self.assertIs(app.about_tab, frames[3])
        app._build_about_tab.assert_called_once_with()

    def test_about_tab_displays_title_and_subtitle(self):
        app = main.InvoiceApp.__new__(main.InvoiceApp)
        app.root = object()
        app.about_tab = SimpleNamespace(
            bind=Mock(),
            winfo_width=Mock(return_value=1000),
            winfo_height=Mock(return_value=700),
        )
        content_frame, heading_frame, members_table = Mock(), Mock(), Mock()
        labels = [Mock() for _ in range(4 + len(main.ABOUT_MEMBERS) * 2)]
        label = Mock(side_effect=labels)
        title_font, subtitle_font = Mock(), Mock()
        title_font.measure.return_value = 240
        title_font.metrics.return_value = 32
        subtitle_font.metrics.return_value = 22
        style = Mock()
        style.return_value.lookup.side_effect = ("title font", "subtitle font")

        with (
            patch.object(main.ttk, "Label", label),
            patch.object(
                main.ttk,
                "Frame",
                side_effect=[content_frame, heading_frame, members_table],
            ) as frame,
            patch.object(main.ttk, "Style", style),
            patch.object(
                main.tkfont, "Font", side_effect=[title_font, subtitle_font]
            ) as font,
            patch.object(
                main.ttk, "Treeview", side_effect=AssertionError
            ) as treeview,
        ):
            app._build_about_tab()

        self.assertEqual(
            [call.kwargs["text"] for call in label.call_args_list],
            [
                main.ABOUT_TITLE,
                main.ABOUT_SUBTITLE,
                "姓名",
                "学号",
                *(name for member in main.ABOUT_MEMBERS for name in (member[0], member[1])),
            ],
        )
        self.assertEqual(
            [call.kwargs["style"] for call in label.call_args_list],
            [
                "AboutTitle.TLabel",
                "AboutSubtitle.TLabel",
                "AboutHeading.TLabel",
                "AboutHeading.TLabel",
                *(style for _member in main.ABOUT_MEMBERS for style in (
                    "AboutMember.TLabel",
                    "AboutMember.TLabel",
                )),
            ],
        )
        self.assertTrue(
            all("background" not in call.kwargs for call in label.call_args_list)
        )
        self.assertEqual(
            [call.kwargs.get("anchor") for call in label.call_args_list[:4]],
            ["center", "e", "center", "center"],
        )
        content_frame.pack.assert_called_once_with(anchor="n", pady=(20, 0))
        heading_frame.pack.assert_called_once_with()
        heading_frame.pack_propagate.assert_called_once_with(False)
        self.assertEqual(
            [item.pack.call_args for item in labels[:2]],
            [
                unittest.mock.call(fill="x"),
                unittest.mock.call(fill="x", pady=(8, 0)),
            ],
        )
        self.assertEqual(
            frame.call_args_list,
            [
                unittest.mock.call(app.about_tab),
                unittest.mock.call(content_frame),
                unittest.mock.call(content_frame),
            ],
        )
        self.assertIs(app.about_heading_frame, heading_frame)
        treeview.assert_not_called()
        self.assertEqual(
            [members_table.columnconfigure.call_args_list],
            [[unittest.mock.call(0, weight=1), unittest.mock.call(1, weight=1)]],
        )
        self.assertEqual(
            [item.grid.call_args for item in labels[2:]],
            [
                unittest.mock.call(
                    row=0, column=0, sticky="ew", padx=(0, 60), pady=(0, 8)
                ),
                unittest.mock.call(
                    row=0, column=1, sticky="ew", pady=(0, 8)
                ),
                *[
                    unittest.mock.call(
                        row=row,
                        column=column,
                        sticky="ew",
                        **({"padx": (0, 60)} if column == 0 else {}),
                        pady=3,
                    )
                    for row in range(1, len(main.ABOUT_MEMBERS) + 1)
                    for column in (0, 1)
                ],
            ],
        )
        members_table.pack.assert_called_once_with(pady=(20, 0))
        app.about_tab.bind.assert_called_once_with(
            "<Configure>", app._update_about_fonts, add="+"
        )
        style.assert_called_once_with(app.root)
        font.assert_has_calls(
            [
                unittest.mock.call(
                    root=app.root,
                    font="title font",
                ),
                unittest.mock.call(
                    root=app.root,
                    font="subtitle font",
                ),
            ]
        )
        heading_frame.configure.assert_called_once_with(width=240, height=66)
        self.assertEqual(
            [call.args[0] for call in style.return_value.configure.call_args_list],
            [
                "AboutTitle.TLabel",
                "AboutSubtitle.TLabel",
                "AboutHeading.TLabel",
                "AboutMember.TLabel",
            ],
        )

    def test_about_font_sizes_scale_with_available_space(self):
        compact = main._about_font_sizes(420, 280)
        normal = main._about_font_sizes(840, 560)
        expanded = main._about_font_sizes(1260, 840)

        self.assertEqual(normal, (30, 19, 16))
        self.assertTrue(all(size > small for size, small in zip(normal, compact)))
        self.assertTrue(all(size > normal_size for size, normal_size in zip(expanded, normal)))

    def test_viewer_resize_preserves_aspect_ratio_and_caps_large_images(self):
        self.assertEqual(main._scaled_image_size(1200, 800, 0.5), (600, 400))
        self.assertEqual(
            main._scaled_image_size(12000, 8000, 1.0), (8192, 5461)
        )

    def test_clicking_invoice_preview_again_closes_enlarged_viewer(self):
        image_path = self.root / "invoice.jpg"
        window = SimpleNamespace(winfo_exists=Mock(return_value=True))
        app = main.InvoiceApp.__new__(main.InvoiceApp)
        app.image_path = image_path
        app.preview_label = object()
        app._viewer_window = window
        app._viewer_path = image_path
        app._close_image_viewer = Mock()

        result = app._open_image_viewer(
            SimpleNamespace(widget=app.preview_label)
        )

        self.assertEqual(result, "break")
        app._close_image_viewer.assert_called_once_with()
        window.winfo_exists.assert_called_once_with()

    def test_drag_drop_splits_quoted_paths_and_starts_local_recognition(self):
        import tkinter

        interpreter = tkinter.Tcl()
        dropped_path = str(self.root / "invoice with spaces.pdf")
        drop_data = interpreter.eval(f"list {{{dropped_path}}}")
        app = main.InvoiceApp.__new__(main.InvoiceApp)
        app.root = SimpleNamespace(tk=interpreter)
        app._scan_running = False
        app._load_invoice = Mock(return_value=True)
        app._start_recognition = Mock()

        event = SimpleNamespace(data=drop_data)
        self.assertEqual(app._on_invoice_drop(event), "break")

        app._load_invoice.assert_called_once_with(Path(dropped_path))
        app._start_recognition.assert_called_once_with("local")

    def test_save_archives_photo_and_duplicate_is_rejected(self):
        source = self.root / "sample.jpg"
        source.write_bytes(b"sample image")

        self.assertTrue(
            storage.save_invoice(
                self.record,
                source,
                db_path=self.db_path,
                photos_dir=self.photos_dir,
            )
        )
        saved = database.query_all(self.db_path)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["invoice_no"], "00123456")
        self.assertEqual(saved[0]["is_edited"], 1)
        self.assertTrue(Path(saved[0]["photo_path"]).is_file())

        with self.assertRaises(storage.InvoiceValidationError):
            storage.save_invoice(
                self.record,
                source,
                db_path=self.db_path,
                photos_dir=self.photos_dir,
            )
        self.assertEqual(len(list(self.photos_dir.iterdir())), 1)

    def test_pdf_first_page_renders_for_ocr_and_original_is_archived(self):
        import pymupdf

        source = self.root / "sample.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "Invoice sample")
        document.save(source)
        document.close()

        self.assertEqual(storage.validate_image_path(source), source.resolve())
        with pdf_support.recognition_image_path(source) as rendered_path:
            self.assertEqual(rendered_path.suffix, ".jpg")
            with Image.open(rendered_path) as rendered:
                self.assertEqual(rendered.format, "JPEG")
                self.assertGreater(rendered.width, 0)
            self.assertTrue(rendered_path.is_file())
        self.assertFalse(rendered_path.exists())

        self.assertTrue(
            storage.save_invoice(
                self.record,
                source,
                db_path=self.db_path,
                photos_dir=self.photos_dir,
            )
        )
        archived_path = Path(database.query_all(self.db_path)[0]["photo_path"])
        self.assertEqual(archived_path.suffix, ".pdf")
        self.assertEqual(archived_path.read_bytes(), source.read_bytes())

    def test_invalid_pdf_is_rejected_before_recognition(self):
        source = self.root / "invalid.pdf"
        source.write_text("not a PDF", encoding="utf-8")
        with self.assertRaises(ValueError):
            storage.validate_image_path(source)

    def test_local_rapidocr_reads_searchable_pdf_buyer_fields(self):
        import pymupdf

        source = self.root / "searchable-invoice.pdf"
        document = pymupdf.open()
        document.new_page(width=595, height=397)
        document.save(source)
        document.close()

        words = [
            (161.7, 22.7, 283.0, 42.7, "电子发票（增值税专用发票）"),
            (32.0, 95.8, 59.0, 104.8, "名称："),
            (56.7, 95.8, 245.7, 104.8, "购买方测试有限公司"),
            (32.0, 126.3, 156.2, 134.8, "统一社会信用代码/纳税人识别号："),
            (151.6, 124.1, 281.2, 137.7, "91310000123456789X"),
            (316.9, 95.8, 343.9, 104.8, "名称："),
            (340.2, 95.8, 466.2, 104.8, "销售方测试有限公司"),
            (316.9, 126.3, 441.1, 134.8, "统一社会信用代码/纳税人识别号："),
            (436.5, 124.1, 566.1, 137.7, "91440000987654321A"),
            (58.0, 261.3, 119.2, 270.3, "合计"),
            (391.1, 260.0, 435.0, 272.4, "55953.60"),
            (436.0, 260.0, 441.0, 272.4, "¥"),
            (470.0, 260.0, 510.0, 272.4, "3357.22"),
            (511.0, 260.0, 516.0, 272.4, "¥"),
            (12.8, 151.2, 112.0, 157.2, "项目名称"),
            (120.5, 151.2, 152.5, 157.2, "规格型号"),
            (200.0, 151.2, 206.0, 157.2, "单位"),
            (270.0, 151.2, 282.0, 157.2, "数量"),
            (336.0, 151.2, 355.0, 157.2, "单价"),
            (410.0, 151.2, 429.0, 157.2, "金额"),
            (445.0, 151.2, 496.0, 157.2, "税率/征收率"),
            (550.0, 151.2, 576.0, 157.2, "税额"),
            (12.8, 161.9, 112.0, 168.0, "*信息系统服务*技术服务"),
            (120.5, 161.9, 129.5, 168.0, "无"),
            (200.0, 161.9, 206.0, 168.0, "项"),
            (286.0, 161.9, 290.0, 168.0, "1"),
            (293.4, 161.9, 361.4, 168.0, "55953.60"),
            (397.7, 161.9, 433.7, 168.0, "55953.60"),
            (467.5, 161.9, 477.0, 168.0, "6%"),
            (550.0, 161.9, 576.0, 168.0, "3357.22"),
            (12.8, 174.3, 17.0, 180.0, "费"),
        ]
        core_result = ocr.RecognitionResult(
            record=ocr.InvoiceRecord(
                invoice_no="00123456",
                date="2026-04-24",
                amount=59310.82,
                category="其他",
            ),
            common_fields=local_ocr._common_fields(None, None),
        )
        with (
            patch.object(
                pdf_support,
                "first_page_text_words",
                return_value=(595.3, words),
            ),
            patch.object(
                local_ocr, "_recognize_image_detailed", return_value=core_result
            ),
        ):
            result = local_ocr.recognize_detailed(source)
        self.assertEqual(
            result.common_fields["purchaser_name"], "购买方测试有限公司"
        )
        self.assertEqual(
            result.common_fields["purchaser_tax_no"], "91310000123456789X"
        )
        self.assertEqual(result.common_fields["seller_name"], "销售方测试有限公司")
        self.assertEqual(
            result.common_fields["seller_tax_no"], "91440000987654321A"
        )
        self.assertEqual(result.common_fields["invoice_type"], "增值税专用发票")
        self.assertEqual(result.common_fields["amount_without_tax"], "55953.60")
        self.assertEqual(result.common_fields["total_tax"], "3357.22")
        self.assertEqual(len(result.common_fields["items"]), 1)
        item = result.common_fields["items"][0]
        self.assertEqual(item["name"], "*信息系统服务*技术服务费")
        self.assertEqual(item["quantity"], "1")
        self.assertEqual(item["unit_price"], "55953.60")
        self.assertEqual(item["amount"], "55953.60")
        self.assertEqual(item["tax_rate"], "6%")
        self.assertEqual(item["tax_amount"], "3357.22")

    def test_manual_entry_and_query_summary(self):
        manual_record = {**self.record, "invoice_no": "87654321", "is_edited": False}
        self.assertTrue(storage.save_invoice(manual_record, db_path=self.db_path))
        rows = query.query_invoices(
            reimburser="测试员工",
            month="2026-11",
            category="餐饮",
            db_path=self.db_path,
        )
        summary = query.summarize_invoices(rows)
        self.assertEqual(summary.count, 1)
        self.assertEqual(summary.total_amount, Decimal("81.0"))
        self.assertEqual(rows[0]["photo_path"], "")

    def test_summary_adds_fractional_currency_without_float_drift(self):
        summary = query.summarize_invoices(
            [{"amount": 0.1}, {"amount": 0.2}]
        )
        self.assertEqual(summary.total_amount, Decimal("0.3"))

    def test_ocr_record_can_flow_through_validation_and_storage(self):
        recognized = ocr.normalize_response(
            {
                "words_result": {
                    "InvoiceNum": "00123456",
                    "InvoiceDate": "2024年02月29日",
                    "AmountInFiguers": "81.00",
                }
            }
        )
        confirmed = {
            **recognized,
            "category": "餐饮",
            "reimburser": "测试员工",
            "is_edited": False,
        }
        self.assertTrue(storage.save_invoice(confirmed, db_path=self.db_path))
        rows = database.query_all(self.db_path)
        self.assertEqual(rows[0]["invoice_no"], "00123456")
        self.assertEqual(rows[0]["date"], "2024-02-29")

    def test_invoice_number_and_amount_only_require_nonempty_values(self):
        long_invoice_no = "INV/2026/" + "1234567890" * 2
        for invoice_no, amount in (
            (long_invoice_no, 12000.0),
            ("INV-0", 0),
        ):
            with self.subTest(invoice_no=invoice_no, amount=amount):
                errors = validate.check(
                    {
                        **self.record,
                        "invoice_no": invoice_no,
                        "amount": amount,
                    },
                    db_path=self.db_path,
                )
                self.assertEqual(errors, [])

        missing_invoice = validate.check(
            {**self.record, "invoice_no": "   "}, db_path=self.db_path
        )
        self.assertIn("发票号不能为空。", missing_invoice)
        missing_amount = validate.check(
            {**self.record, "amount": ""}, db_path=self.db_path
        )
        self.assertIn("金额不能为空。", missing_amount)

        source = self.root / "sample.jpg"
        source.write_bytes(b"sample image")
        self.assertTrue(
            storage.save_invoice(
                {
                    **self.record,
                    "invoice_no": "../../invoice/12345678901234567890",
                    "amount": 12000,
                },
                source,
                db_path=self.db_path,
                photos_dir=self.photos_dir,
            )
        )
        archived_path = Path(database.query_all(self.db_path)[0]["photo_path"])
        self.assertEqual(archived_path.parent, self.photos_dir.resolve())
        self.assertTrue(archived_path.is_file())

    def test_duplicate_database_guard_remains(self):
        database.insert(self.record, "", db_path=self.db_path)
        self.assertIn(
            "该发票号已存在，不允许重复入库。",
            validate.check(self.record, db_path=self.db_path),
        )

    def test_validation_rules_report_future_date_amount_and_category_errors(self):
        cases = (
            ({**self.record, "date": (date.today() + timedelta(days=1)).isoformat()},
             "日期不能是未来日期。"),
            ({**self.record, "category": "住宿"}, "类别必须是：餐饮/交通/办公/其他 之一。"),
            ({**self.record, "invoice_no": 12345678}, "发票号必须是字符串。"),
            ({**self.record, "amount": "abc"}, "金额必须是数字。"),
        )
        for invalid_record, expected_error in cases:
            with self.subTest(error=expected_error):
                self.assertIn(
                    expected_error,
                    validate.check(invalid_record, db_path=self.db_path),
                )

    def test_manual_input_fallback_creates_record_without_ocr(self):
        answers = iter(("00123456", "2024-02-29", "81.00", "1"))

        def read_answer(prompt):
            self.assertTrue(prompt)
            return next(answers)

        def collect_message(message):
            self.assertIsInstance(message, str)

        confirmed = collect_invoice(
            input_fn=read_answer,
            output_fn=collect_message,
        )
        self.assertIsNotNone(confirmed)
        self.assertEqual(confirmed.record["invoice_no"], "00123456")
        self.assertEqual(confirmed.record["category"], "餐饮")
        self.assertEqual(confirmed.source, "manual")

    def test_local_rapidocr_recognizes_sample_invoice(self):
        image = Path(__file__).resolve().parents[1] / (
            "test_pictures/CN-05_aliyun_vat_example_1.jpg"
        )
        result = local_ocr.recognize_detailed(image)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.record["invoice_no"], "03801959")
        self.assertEqual(result.record["date"], "2020-12-15")
        self.assertEqual(result.record["amount"], 81.0)
        self.assertEqual(result.record["category"], "其他")
        self.assertEqual(
            result.common_fields["purchaser_name"], "一号技术有限责任公司"
        )
        self.assertEqual(
            result.common_fields["purchaser_tax_no"], "91420200000123401"
        )

        import pymupdf

        pdf_path = self.root / "sample-invoice.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_image(page.rect, filename=str(image))
        document.save(pdf_path)
        document.close()
        with pdf_support.recognition_image_path(pdf_path) as rendered_path:
            pdf_result = local_ocr.recognize_detailed(rendered_path)
        self.assertTrue(pdf_result.ok, pdf_result.message)
        self.assertEqual(pdf_result.record["invoice_no"], result.record["invoice_no"])
        self.assertEqual(pdf_result.record["date"], result.record["date"])
        self.assertEqual(pdf_result.record["amount"], result.record["amount"])
        self.assertEqual(
            pdf_result.common_fields["purchaser_name"],
            result.common_fields["purchaser_name"],
        )
        self.assertEqual(
            pdf_result.common_fields["purchaser_tax_no"],
            result.common_fields["purchaser_tax_no"],
        )

        confirmed = {
            **result.record,
            "category": "餐饮",
            "reimburser": "本地 OCR 测试",
        }
        self.assertTrue(storage.save_invoice(confirmed, db_path=self.db_path))
        saved = database.query_all(self.db_path)
        self.assertEqual(saved[0]["invoice_no"], "03801959")
        self.assertEqual(saved[0]["category"], "餐饮")

    def test_local_ocr_retries_invoice_number_in_cropped_header(self):
        source = self.root / "header-retry.png"
        Image.new("RGB", (1000, 800), "white").save(source)
        box = [[0, 0], [20, 0], [20, 10], [0, 10]]
        page_result = [
            (box, "开票日期:2021年04月01日", 0.99),
            (box, "(小写)¥100.00", 0.99),
        ]
        header_result = [(box, "发票号码:07960724", 0.99)]
        header_paths = []

        def recognize(image_path):
            image_path = Path(image_path)
            if image_path == source.resolve():
                return page_result, None
            header_paths.append(image_path)
            with Image.open(image_path) as image:
                self.assertEqual(image.size, (1200, 432))
            return header_result, None

        engine = Mock(side_effect=recognize)
        with patch.object(local_ocr, "_get_engine", return_value=engine):
            result = local_ocr._recognize_image_detailed(source)

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.record["invoice_no"], "07960724")
        self.assertEqual(result.record["date"], "2021-04-01")
        self.assertEqual(result.record["amount"], 100.0)
        self.assertEqual(len(header_paths), 1)
        self.assertFalse(header_paths[0].exists())

    def test_local_purchaser_parser_does_not_use_seller_fields(self):
        lines = [
            ("称:买方有限公司", 1.0, 95.0, 110.0),
            ("纳税人识别号:91310000123456789X", 1.0, 130.0, 145.0),
            ("货物或应税劳务、服务名称", 1.0, 180.0, 195.0),
            ("销售方", 1.0, 300.0, 330.0),
            ("名称:卖方有限公司", 1.0, 310.0, 325.0),
            ("纳税人识别号:91440000987654321A", 1.0, 330.0, 345.0),
        ]
        self.assertEqual(
            local_ocr._extract_purchaser_fields(lines),
            ("买方有限公司", "91310000123456789X"),
        )

    def test_local_purchaser_parser_joins_adjacent_label_and_value_boxes(self):
        def box(x0, y0, x1, y1, text):
            return ([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, 0.99)

        standard_invoice = [
            box(216, 157, 241, 177, "称："),
            box(263, 157, 441, 177, "买方测试有限公司"),
            box(148, 188, 240, 208, "纳税人识别号："),
            box(263, 190, 388, 207, "91310000123456789X"),
        ]
        self.assertEqual(
            local_ocr._extract_purchaser_fields_from_boxes(
                standard_invoice, (1298, 768)
            ),
            ("买方测试有限公司", "91310000123456789X", True),
        )

        individual_invoice = [
            box(10, 183, 333, 203, "收款单位：卖方测试有限公司"),
            box(9, 217, 197, 237, "税号：914400009876543"),
            box(11, 250, 186, 270, "付款单位（个人）：个人"),
            box(9, 283, 118, 303, "纳税人识别号："),
        ]
        self.assertEqual(
            local_ocr._extract_purchaser_fields_from_boxes(
                individual_invoice, (990, 720)
            ),
            ("个人", None, True),
        )

    def test_local_image_parser_extracts_invoice_type_and_seller_fields(self):
        def box(x0, y0, x1, y1, text):
            return ([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, 0.99)

        recognized = [
            box(470, 50, 900, 110, "江苏增值税电子普通发票"),
            box(180, 563, 213, 645, "销售方"),
            box(220, 558, 241, 580, "名称"),
            box(303, 556, 527, 580, "名称：南京滴滴出行科技有限公司"),
            box(221, 577, 509, 603, "纳税人识别号：91320105MA20JN20M"),
        ]
        fields = local_ocr._extract_image_common_fields(
            recognized, (1365, 768)
        )
        self.assertEqual(fields["invoice_type"], "增值税电子普通发票")
        self.assertEqual(fields["seller_name"], "南京滴滴出行科技有限公司")
        self.assertEqual(fields["seller_tax_no"], "91320105MA20JN20M")

    def test_local_image_parser_extracts_all_five_preview_summary_fields(self):
        def box(x0, y0, x1, y1, text):
            return ([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, 0.99)

        recognized = [
            box(300, 30, 700, 60, "增值税电子普通发票"),
            box(100, 300, 240, 320, "货物或应税劳务、服务名称"),
            box(300, 300, 360, 320, "规格型号"),
            box(390, 300, 420, 320, "单位"),
            box(450, 300, 490, 320, "数量"),
            box(520, 300, 570, 320, "单价"),
            box(620, 300, 670, 320, "金额"),
            box(700, 300, 760, 320, "税率"),
            box(800, 300, 850, 320, "税额"),
            box(100, 340, 250, 360, "商品服务"),
            box(390, 340, 420, 360, "项"),
            box(450, 340, 460, 360, "1"),
            box(520, 340, 570, 360, "10.00"),
            box(620, 340, 670, 360, "10.00"),
            box(700, 340, 740, 360, "6%"),
            box(800, 340, 850, 360, "0.60"),
            box(100, 500, 160, 520, "合计"),
            box(620, 500, 670, 520, "¥10.00"),
            box(800, 500, 850, 520, "¥0.60"),
            box(100, 600, 150, 620, "销售方"),
            box(100, 630, 150, 650, "名称"),
            box(160, 630, 350, 650, "测试销售有限公司"),
        ]

        fields = local_ocr._extract_image_common_fields(recognized, (1000, 800))

        self.assertEqual(fields["invoice_type"], "增值税电子普通发票")
        self.assertEqual(fields["seller_name"], "测试销售有限公司")
        self.assertEqual(fields["amount_without_tax"], "10.00")
        self.assertEqual(fields["total_tax"], "0.60")
        self.assertEqual(len(fields["items"]), 1)
        self.assertEqual(
            main.InvoiceApp._common_fields_summary(fields),
            "发票种类：增值税电子普通发票\n"
            "销售方：测试销售有限公司\n"
            "不含税合计：10.00\n"
            "合计税额：0.60\n"
            "商品明细：1 行",
        )

    def test_local_rapidocr_extracts_buyer_fields_from_all_supported_samples(self):
        samples_dir = Path(__file__).resolve().parents[1] / "test_pictures"
        expected = {
            "CN-05_aliyun_vat_example_1.jpg": (
                "一号技术有限责任公司",
                "91420200000123401",
            ),
            "CN-06_aliyun_vat_example_3.jpg": (
                "三号技术有限责任公司",
                "91420200000123403",
            ),
            "CN-07_aliyun_vat_example_2.jpg": (
                "二号技术有限责任公司",
                "91420200000123402",
            ),
            "CN-11_tencent_vat_example.jpg": (
                "腾讯优图有限公司",
                "91440300MA55UJ6L44",
            ),
            "CN-12_aliyun_personal_name_einvoice.png": ("黄宏伟", None),
            "CN-13_aliyun_personal_einvoice.png": ("个人", None),
            "CN-14_wppop_digital_ordinary_conflict.png": (None, None),
        }
        for filename, purchaser_fields in expected.items():
            with self.subTest(sample=filename):
                result = local_ocr.recognize_detailed(samples_dir / filename)
                self.assertEqual(
                    (
                        result.common_fields["purchaser_name"],
                        result.common_fields["purchaser_tax_no"],
                    ),
                    purchaser_fields,
                )
                if filename == "CN-05_aliyun_vat_example_1.jpg":
                    self.assertEqual(
                        (
                            result.common_fields["invoice_type"],
                            result.common_fields["seller_name"],
                            result.common_fields["amount_without_tax"],
                            result.common_fields["total_tax"],
                            len(result.common_fields["items"]),
                        ),
                        (
                            "增值税电子普通发票",
                            "一号技术有限责任公司",
                            "76.42",
                            "4.58",
                            1,
                        ),
                    )

    def test_existing_database_is_migrated_without_losing_rows(self):
        legacy_path = str(self.root / "legacy.sqlite3")
        with closing(sqlite3.connect(legacy_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    department TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_no TEXT NOT NULL UNIQUE,
                    invoice_date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    reimburser TEXT NOT NULL,
                    photo_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO invoices
                    (invoice_no, invoice_date, amount, category, reimburser,
                     photo_path, created_at)
                VALUES ('12345678', '2026-11-06', 10, '其他', '旧员工', '',
                        '2026-11-06 12:00:00');
                """
            )

        init_database.init_database(legacy_path)
        rows = database.query_all(legacy_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["is_edited"], 0)


if __name__ == "__main__":
    unittest.main()
