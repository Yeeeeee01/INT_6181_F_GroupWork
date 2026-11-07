"""Offline invoice text recognition using RapidOCR."""

from __future__ import annotations

import logging
import re
import threading
import tempfile
import time
import unicodedata
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from PIL import Image, UnidentifiedImageError

import ocr
import pdf_support

LOGGER = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MIN_CONFIDENCE = 0.3

_ENGINE = None
_ENGINE_LOCK = threading.Lock()

_INVOICE_NO = re.compile(r"发票\s*号码?\s*[:：]?\s*([0-9]{8,20})")
_INVOICE_DATE = re.compile(
    r"开票\s*日期\s*[:：]?\s*"
    r"([0-9]{4}\s*年\s*[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日|"
    r"[0-9]{4}\s*[-/.]\s*[0-9]{1,2}\s*[-/.]\s*[0-9]{1,2})"
)
_INVOICE_TYPE = re.compile(
    r"(?:增值税)?(?:电子)?(?:专用|普通)发票|数电(?:专用|普通)?发票"
)
_PURCHASER_NAME = re.compile(r"(?:名\s*)?称\s*[:：]\s*(.+)")
_PURCHASER_TAX_NO = re.compile(
    r"(?:统一社会信用代码(?:\s*[/／]\s*纳税人识别号)?|"
    r"纳税人识别号|纳税人识别码|税号)\s*[:：]?\s*"
    r"([0-9A-Z]{15,20})",
    re.IGNORECASE,
)
_PURCHASER_TAX_LABEL = re.compile(
    r"统一社会信用代码|纳税人识别号|纳税人识别码|税号"
)
_PURCHASER_SECTION_END = re.compile(r"货物或应税劳务|商品名称|服务名称")
_SMALL_TOTAL = re.compile(
    r"(?:小写|小寫)[^0-9¥￥]*?"
    r"(?:(?:人民币|RMB|CNY|¥|￥)\s*)?"
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_LABELED_TOTAL = re.compile(
    r"价税合计.*?(?:人民币|RMB|CNY|¥)\s*"
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"(?:人民币|RMB|CNY|¥)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)


class LocalOcrUnavailableError(RuntimeError):
    """Raised when the optional RapidOCR runtime cannot be imported."""


def _get_engine():
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                try:
                    try:
                        from rapidocr_onnxruntime import RapidOCR
                    except ImportError:
                        from rapidocr import RapidOCR
                except ImportError as exc:
                    raise LocalOcrUnavailableError(
                        "本地 RapidOCR 组件不可用，请在项目环境安装 "
                        "requirements.txt 中的依赖。"
                    ) from exc
                _ENGINE = RapidOCR()
    return _ENGINE


def _run_engine(image_path: str | Path):
    """Normalize legacy and current RapidOCR results to the same tuple format."""
    result = _get_engine()(str(image_path))
    if isinstance(result, tuple) and len(result) == 2:
        return result

    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    timings = getattr(result, "elapse_list", None)
    if boxes is None or texts is None or scores is None:
        return [], timings
    if not (len(boxes) == len(texts) == len(scores)):
        raise ValueError("RapidOCR 返回结构无效：文本、置信度和位置数量不一致。")
    return list(zip(boxes, texts, scores)), timings


def _validate_image(image_path: str | Path) -> Path:
    path = Path(image_path).expanduser()
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("请选择 JPG、JPEG 或 PNG 图片。")
    try:
        if not path.is_file():
            raise FileNotFoundError("图片不存在或无法读取。")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("图片超过本地识别的 20 MiB 大小限制。")
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG"}:
                raise ValueError("图片扩展名与实际格式不匹配。")
            image.verify()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("图片损坏或无法读取，请重新选择。") from exc
    return path.resolve()


def _recognized_lines(result: object) -> list[tuple[str, float, float, float]]:
    if result is None:
        return []
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
        raise ValueError("RapidOCR 返回结构无效。")

    lines: list[tuple[str, float, float, float]] = []
    for item in result:
        if not isinstance(item, Sequence) or len(item) < 3:
            continue
        box, raw_text, raw_score = item[:3]
        if not isinstance(raw_text, str):
            continue
        try:
            score = float(raw_score)
            ys = [float(point[1]) for point in box]
            y_top, y_bottom = min(ys), max(ys)
        except (TypeError, ValueError, IndexError):
            continue
        if score < MIN_CONFIDENCE:
            continue
        text = unicodedata.normalize("NFKC", raw_text).strip()
        if text:
            lines.append((text, score, y_top, y_bottom))
    return lines


def _recognize_invoice_header(image_path: Path) -> list[tuple[str, float, float, float]]:
    with Image.open(image_path) as image:
        width, height = image.size
        header = image.crop(
            (round(width * 0.6), 0, width, max(1, round(height * 0.18)))
        )
        scale = min(3.0, 1800 / max(1, header.width))
        if scale > 1:
            header = header.resize(
                (round(header.width * scale), round(header.height * scale)),
                Image.Resampling.LANCZOS,
            )
        header = header.convert("RGB")

    with tempfile.TemporaryDirectory(prefix="invoice-ocr-") as temp_dir:
        header_path = Path(temp_dir) / "header.jpg"
        header.save(header_path, format="JPEG", quality=95)
        raw_result, _timings = _run_engine(header_path)
    return [
        line for line in _recognized_lines(raw_result)
        if _INVOICE_NO.search(line[0])
    ]


def _recognize_invoice_type_header(image_path: Path) -> str | None:
    with Image.open(image_path) as image:
        width, height = image.size
        header = image.crop((0, 0, width, max(1, round(height * 0.18))))
        scale = min(3.0, 2400 / max(1, header.width))
        if scale > 1:
            header = header.resize(
                (round(header.width * scale), round(header.height * scale)),
                Image.Resampling.LANCZOS,
            )
        header = header.convert("RGB")

    with tempfile.TemporaryDirectory(prefix="invoice-ocr-") as temp_dir:
        header_path = Path(temp_dir) / "header.jpg"
        header.save(header_path, format="JPEG", quality=95)
        raw_result, _timings = _run_engine(header_path)
    lines = _recognized_lines(raw_result)
    title_text = "".join(
        line[0] for line in sorted(lines, key=lambda line: line[2])
    )
    return _invoice_type_from_text(title_text)


def _invoice_type_from_text(text: str) -> str | None:
    match = _INVOICE_TYPE.search(text)
    if match:
        return match.group(0)
    if "增值税" in text and "支用发票" in text:
        return "增值税专用发票"
    if "增值" in text and "通发票" in text:
        return "增值税普通发票"
    return None


def _unique(values: list[str]) -> str | None:
    normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if len(normalized) > 1:
        raise ValueError("识别到多个互相冲突的候选值。")
    return normalized[0] if normalized else None


def _extract_purchaser_fields(
    lines: list[tuple[str, float, float, float]],
) -> tuple[str | None, str | None]:
    table_header_tops = [
        line[2] for line in lines if _PURCHASER_SECTION_END.search(line[0])
    ]
    seller_heading_tops = [
        line[2] for line in lines if "销售方" in line[0]
    ]
    if table_header_tops:
        section_end = min(table_header_tops)
    elif seller_heading_tops:
        section_end = min(seller_heading_tops)
    else:
        section_end = float("inf")

    names: list[str] = []
    tax_numbers: list[str] = []
    has_unparsed_tax_label = False
    for text, _score, y_top, _y_bottom in lines:
        if y_top >= section_end:
            continue
        name_match = _PURCHASER_NAME.search(text)
        if name_match:
            names.append(name_match.group(1).strip())
        if _PURCHASER_TAX_LABEL.search(text):
            tax_match = _PURCHASER_TAX_NO.search(text)
            if tax_match:
                tax_numbers.append(tax_match.group(1).upper())
            else:
                has_unparsed_tax_label = True

    try:
        purchaser_name = _unique(names)
    except ValueError:
        purchaser_name = None
    try:
        purchaser_tax_no = _unique(tax_numbers)
    except ValueError:
        purchaser_tax_no = None
    if has_unparsed_tax_label:
        purchaser_tax_no = None
    return purchaser_name, purchaser_tax_no


def _extract_purchaser_fields_from_boxes(
    result: object, image_size: tuple[int, int]
) -> tuple[str | None, str | None, bool]:
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
        return None, None, False

    image_width, image_height = image_size
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for item in result:
        if not isinstance(item, Sequence) or len(item) < 3:
            continue
        box, raw_text, raw_score = item[:3]
        if not isinstance(raw_text, str):
            continue
        try:
            score = float(raw_score)
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (TypeError, ValueError, IndexError):
            continue
        if score < MIN_CONFIDENCE or not xs or not ys:
            continue
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if (
            x0 >= image_width * 0.7
            or y0 < image_height * 0.12
            or y0 > image_height * 0.45
        ):
            continue
        text = unicodedata.normalize("NFKC", raw_text).strip()
        if not text:
            continue
        word = (x0, y0, x1, y1, text)
        row = next(
            (
                candidate
                for candidate in rows
                if abs(
                    (candidate[0][1] + candidate[0][3]) / 2
                    - (y0 + y1) / 2
                )
                <= max(
                    4.0,
                    min(candidate[0][3] - candidate[0][1], y1 - y0) * 0.25,
                )
            ),
            None,
        )
        if row is None:
            rows.append([word])
        else:
            row.append(word)

    purchaser_names: list[str] = []
    tax_numbers: list[str] = []
    purchaser_section_started = False
    tax_label_unparsed = False
    for row in sorted(rows, key=lambda group: min(word[1] for word in group)):
        text = "".join(word[4] for word in sorted(row, key=lambda word: word[0]))
        if re.search(r"收款单位|销货单位|销售方", text):
            purchaser_section_started = False
            continue

        payer_match = re.match(
            r"^\s*(?:付款单位(?:[（(][^）)]*[）)])?|"
            r"购买方(?:名称)?|购方名称)\s*[:：]?\s*(.+)",
            text,
        )
        name_match = payer_match or re.match(
            r"^\s*(?:名\s*)?称\s*[:：]\s*(.+)", text
        )
        if name_match:
            value = name_match.group(1).strip(" ：:")
            if value:
                purchaser_names.append(value)
                purchaser_section_started = True
        tax_label = re.match(
            r"^\s*(?:统一社会信用代码|纳税人识别号|纳税人识别码|税号)",
            text,
        )
        if tax_label and purchaser_section_started:
            tax_match = _PURCHASER_TAX_NO.search(re.sub(r"\s+", "", text))
            if tax_match:
                tax_numbers.append(tax_match.group(1).upper())
            else:
                tax_label_unparsed = True

    try:
        purchaser_tax_no = _unique(tax_numbers)
    except ValueError:
        purchaser_tax_no = None
    if tax_label_unparsed:
        purchaser_tax_no = None
    try:
        purchaser_name = _unique(purchaser_names)
    except ValueError:
        purchaser_name = None
    return purchaser_name, purchaser_tax_no, bool(purchaser_names)


def _image_word_rows(
    tokens: list[tuple[float, float, float, float, str]],
) -> list[list[tuple[float, float, float, float, str]]]:
    if not tokens:
        return []
    heights = sorted(word[3] - word[1] for word in tokens)
    row_tolerance = max(3.0, heights[len(heights) // 2] * 0.55)
    rows: list[list[tuple[float, float, float, float, str]]] = []
    row_centers: list[float] = []
    for word in sorted(tokens, key=lambda item: ((item[1] + item[3]) / 2, item[0])):
        center_y = (word[1] + word[3]) / 2
        matching_row = next(
            (
                index
                for index, row_center in enumerate(row_centers)
                if abs(row_center - center_y) <= row_tolerance
            ),
            None,
        )
        if matching_row is None:
            rows.append([word])
            row_centers.append(center_y)
        else:
            row = rows[matching_row]
            row.append(word)
            row_centers[matching_row] = sum(
                (item[1] + item[3]) / 2 for item in row
            ) / len(row)
    return [
        sorted(row, key=lambda item: item[0])
        for row in sorted(rows, key=lambda row: min(word[1] for word in row))
    ]


def _image_money_values(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"^(?:人民币|RMB|CNY|[¥￥Yy])+", "", normalized.strip(), flags=re.I)
    matches = re.findall(
        r"(?<![0-9.])([0-9][0-9,]*(?:\s*\.\s*[0-9]{1,2})?)",
        normalized,
    )
    return [re.sub(r"[\s,]", "", value) for value in matches]


def _extract_image_items(
    tokens: list[tuple[float, float, float, float, str]],
    image_height: int,
) -> list[ocr.CommodityItem]:
    rows = _image_word_rows(tokens)
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if any(
                re.search(r"货物|项目名称|商品名称|服务名称", word[4])
                for word in row
            )
            and "金额" in "".join(word[4] for word in row)
            and any("税率" in word[4] for word in row)
            and any("税额" in word[4] for word in row)
        ),
        None,
    )
    if header_index is None:
        return []
    header_row = rows[header_index]

    column_patterns = (
        ("name", re.compile(r"货物|项目名称|商品名称|服务名称")),
        ("specification", re.compile(r"规格型号")),
        ("unit", re.compile(r"单位")),
        ("quantity", re.compile(r"数量")),
        ("unit_price", re.compile(r"单价")),
        ("amount", re.compile(r"金额|^金$|^额$")),
        ("tax_rate", re.compile(r"税率")),
        ("tax_amount", re.compile(r"税额")),
    )
    columns: list[tuple[float, str]] = []
    for key, pattern in column_patterns:
        matches = [word for word in header_row if pattern.search(word[4])]
        if matches:
            columns.append(
                (
                    (
                        min(word[0] for word in matches)
                        + max(word[2] for word in matches)
                    )
                    / 2,
                    key,
                )
            )
    columns.sort()
    if not {"name", "amount", "tax_amount"}.issubset(
        {key for _center, key in columns}
    ):
        return []

    total_top = min(
        (
            min(word[1] for word in row)
            for row_index, row in enumerate(rows)
            if row_index > header_index
            if "合计" in "".join(word[4] for word in row)
            and "价税合计" not in "".join(word[4] for word in row)
        ),
        default=image_height * 0.82,
    )
    items: list[ocr.CommodityItem] = []
    numeric_pattern = re.compile(r"[0-9]+(?:\.[0-9]+)?%?")
    previous_item_bottom: float | None = None
    for row_index, row in enumerate(rows):
        if row_index <= header_index:
            continue
        row_top = min(word[1] for word in row)
        row_text = "".join(word[4] for word in row)
        if row_top >= total_top or "价税合计" in row_text:
            break
        cells: dict[str, list[str]] = {
            key: [] for _center, key in columns
        }
        centers = [center for center, _key in columns]
        for word in row:
            column_index = min(
                range(len(columns)),
                key=lambda index: abs(centers[index] - word[0]),
            )
            cells[columns[column_index][1]].append(word[4])
        values = {
            key: "".join(value) or None
            for key, value in cells.items()
        }
        has_amounts = any(
            values.get(key)
            and numeric_pattern.search(values[key] or "")
            for key in ("quantity", "unit_price", "amount", "tax_rate", "tax_amount")
        )
        if not has_amounts:
            numeric_values = list(
                re.finditer(r"[0-9]+(?:\.[0-9]+)?%?", row_text)
            )
            if (
                len(numeric_values) >= 5
                and numeric_values[-2].group(0).endswith("%")
            ):
                quantity, unit_price, amount, tax_rate, tax_amount = (
                    match.group(0) for match in numeric_values[-5:]
                )
                values.update(
                    {
                        "name": row_text[:numeric_values[-5].start()].strip() or None,
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "amount": amount,
                        "tax_rate": tax_rate,
                        "tax_amount": tax_amount,
                    }
                )
                has_amounts = True
        if has_amounts:
            items.append(
                {
                    "row": len(items) + 1,
                    "name": values.get("name"),
                    "specification": values.get("specification"),
                    "unit": values.get("unit"),
                    "quantity": values.get("quantity"),
                    "unit_price": values.get("unit_price"),
                    "amount": values.get("amount"),
                    "tax_rate": values.get("tax_rate"),
                    "tax_amount": values.get("tax_amount"),
                }
            )
            previous_item_bottom = max(word[3] for word in row)
        elif (
            items
            and previous_item_bottom is not None
            and -3.0
            <= row_top - previous_item_bottom
            <= max(12.0, image_height * 0.035)
        ):
            for key in ("name", "specification"):
                continuation = values.get(key)
                if continuation:
                    items[-1][key] = (items[-1][key] or "") + continuation
            previous_item_bottom = max(word[3] for word in row)
    return items


def _extract_image_common_fields(
    result: object, image_size: tuple[int, int]
) -> ocr.CommonInvoiceFields:
    fields = _common_fields(None, None)
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
        return fields

    image_width, image_height = image_size
    tokens: list[tuple[float, float, float, float, str]] = []
    for item in result:
        if not isinstance(item, Sequence) or len(item) < 3:
            continue
        box, raw_text, raw_score = item[:3]
        if not isinstance(raw_text, str):
            continue
        try:
            score = float(raw_score)
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (TypeError, ValueError, IndexError):
            continue
        if score < MIN_CONFIDENCE or not xs or not ys:
            continue
        text = unicodedata.normalize("NFKC", raw_text).strip()
        if text:
            tokens.append((min(xs), min(ys), max(xs), max(ys), text))

    rows = _image_word_rows(tokens)
    title_text = "".join(
        word[4]
        for row in rows
        for word in row
        if word[1] < image_height * 0.18
    )
    fields["invoice_type"] = _invoice_type_from_text(title_text)

    seller_headings = [
        word[1]
        for word in tokens
        if re.search(r"销售方|销货方", word[4]) and word[0] < image_width * 0.8
    ]
    for seller_word in (
        word for word in tokens if "售方" in word[4] and word[0] < image_width * 0.8
    ):
        seller_x = (seller_word[0] + seller_word[2]) / 2
        vertical_prefixes = [
            word
            for word in tokens
            if word[4] in {"销", "销货"}
            and 0 <= seller_word[1] - word[1] <= image_height * 0.08
            and abs((word[0] + word[2]) / 2 - seller_x) <= image_width * 0.05
        ]
        if vertical_prefixes:
            seller_headings.append(min(word[1] for word in vertical_prefixes))
    for prefix in (
        word for word in tokens if word[4] in {"销", "销货"} and word[0] < image_width * 0.8
    ):
        prefix_x = (prefix[0] + prefix[2]) / 2
        nearby_seller_parts = [
            word
            for word in tokens
            if word[4] in {"售", "方", "售方"}
            and 0 < word[1] - prefix[1] <= image_height * 0.12
            and abs((word[0] + word[2]) / 2 - prefix_x) <= image_width * 0.05
        ]
        has_sale_and_party = (
            any(word[4] == "售方" for word in nearby_seller_parts)
            or (
                any(word[4] == "售" for word in nearby_seller_parts)
                and any(
                    word[4] == "方"
                    and word[1] > min(
                        part[1]
                        for part in nearby_seller_parts
                        if part[4] == "售"
                    )
                    for word in nearby_seller_parts
                )
            )
        )
        if has_sale_and_party:
            seller_headings.append(prefix[1])
    if seller_headings:
        seller_top = min(seller_headings)
        seller_tokens = [
            word
            for word in tokens
            if word[0] < image_width * 0.7
            and max(0.0, seller_top - image_height * 0.025)
            <= word[1]
            <= image_height * 0.9
        ]
        seller_names: list[str] = []
        seller_tax_numbers: list[str] = []
        for row in _image_word_rows(seller_tokens):
            has_name_label = any("名称" in word[4] or word[4] == "名" for word in row)
            row_parts = []
            for word in row:
                is_duplicate = any(
                    word is not other
                    and word[4] in other[4]
                    and word[0] < other[2]
                    and other[0] < word[2]
                    for other in row
                )
                if is_duplicate or (
                    has_name_label and word[4] in {"备", "备注", "备注："}
                ):
                    continue
                row_parts.append(word[4])
            row_text = "".join(row_parts)
            inline_names: list[str] = []
            for index, word in enumerate(row):
                name_match = _PURCHASER_NAME.search(word[4])
                if not name_match:
                    continue
                name = name_match.group(1).strip()
                right_edge = word[2]
                for following in row[index + 1:]:
                    if following[0] - right_edge > max(12.0, image_width * 0.025):
                        break
                    if following[4] in {"备", "备注", "备注："}:
                        continue
                    if name and following[4] in name:
                        continue
                    name += following[4]
                    right_edge = following[2]
                inline_names.append(name)
            if inline_names:
                seller_names.extend(inline_names)
            elif re.search(r"名称\s*[:：]?", row_text):
                name_label = re.search(r"名称\s*[:：]?", row_text)
                if name_label:
                    seller_names.append(row_text[name_label.end():].strip())
            if _PURCHASER_TAX_LABEL.search(row_text):
                tax_match = _PURCHASER_TAX_NO.search(re.sub(r"\s+", "", row_text))
                if tax_match:
                    seller_tax_numbers.append(tax_match.group(1).upper())
        try:
            fields["seller_name"] = _unique(seller_names)
        except ValueError:
            LOGGER.warning(
                "Conflicting seller names were recognized; leaving the field empty"
            )
        try:
            fields["seller_tax_no"] = _unique(seller_tax_numbers)
        except ValueError:
            LOGGER.warning(
                "Conflicting seller tax numbers were recognized; leaving the field empty"
            )

    total_rows = [
        row
        for row in rows
        if "合计" in "".join(word[4] for word in row)
        and "价税合计" not in "".join(word[4] for word in row)
    ]
    if total_rows:
        total_row = total_rows[-1]
        label_right = max(
            (word[2] for word in total_row if "合计" in word[4]),
            default=0.0,
        )
        amounts: list[tuple[float, str]] = []
        for word in total_row:
            if "合计" in word[4]:
                values = _image_money_values(word[4])
            elif word[0] >= label_right:
                values = _image_money_values(word[4])
            else:
                values = []
            amounts.extend(
                (((word[0] + word[2]) / 2, value) for value in values)
            )
        amounts.sort()
        if len(amounts) >= 2:
            fields["amount_without_tax"] = amounts[0][1]
            fields["total_tax"] = amounts[-1][1]
        elif amounts:
            key = (
                "total_tax"
                if amounts[0][0] >= image_width * 0.82
                else "amount_without_tax"
            )
            fields[key] = amounts[0][1]

    fields["items"] = _extract_image_items(tokens, image_height)
    return fields


def _extract_pdf_party_fields(
    page_width: float,
    words: list[tuple[float, float, float, float, str]],
    *,
    left_side: bool,
) -> tuple[str | None, str | None]:
    middle = page_width / 2
    party_words = [
        word for word in words
        if (word[0] < middle if left_side else word[0] >= middle)
    ]
    section_end = min(
        (
            word[1]
            for word in party_words
            if _PURCHASER_SECTION_END.search(word[4]) or "项目名称" in word[4]
        ),
        default=float("inf"),
    )
    name_values: list[str] = []
    tax_values: list[str] = []
    for label in party_words:
        if label[1] >= section_end:
            continue
        label_text = unicodedata.normalize("NFKC", label[4])
        same_line = sorted(
            (
                word
                for word in party_words
                if word[1] < section_end and abs(word[1] - label[1]) <= 3.5
            ),
            key=lambda word: word[0],
        )
        line_text = "".join(word[4] for word in same_line)
        if "名称" in label_text:
            match = _PURCHASER_NAME.search(line_text)
            if match:
                name_values.append(match.group(1).strip())
        if _PURCHASER_TAX_LABEL.search(label_text):
            match = _PURCHASER_TAX_NO.search(line_text)
            if match:
                tax_values.append(match.group(1).upper())

    try:
        purchaser_name = _unique(name_values)
    except ValueError:
        purchaser_name = None
    try:
        purchaser_tax_no = _unique(tax_values)
    except ValueError:
        purchaser_tax_no = None
    return purchaser_name, purchaser_tax_no


def _pdf_word_rows(
    words: list[tuple[float, float, float, float, str]],
) -> list[list[tuple[float, float, float, float, str]]]:
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for word in sorted(words, key=lambda item: (item[1], item[0])):
        row = next(
            (row for row in rows if abs(row[0][1] - word[1]) <= 3.5),
            None,
        )
        if row is None:
            rows.append([word])
        else:
            row.append(word)
    return rows


def _pdf_text_rows(
    words: list[tuple[float, float, float, float, str]],
) -> list[str]:
    return [
        "".join(word[4] for word in sorted(row, key=lambda item: item[0]))
        for row in _pdf_word_rows(words)
    ]


def _extract_pdf_items(
    words: list[tuple[float, float, float, float, str]],
) -> list[ocr.CommodityItem]:
    word_rows = _pdf_word_rows(words)
    header_row = next(
        (
            row
            for row in word_rows
            if any("项目名称" in word[4] for word in row)
            and any("规格型号" in word[4] for word in row)
            and any("税率/征收率" in word[4] for word in row)
        ),
        None,
    )
    if header_row is None:
        return []

    def center(
        matching_words: list[tuple[float, float, float, float, str]],
    ) -> float | None:
        if not matching_words:
            return None
        return (
            min(word[0] for word in matching_words)
            + max(word[2] for word in matching_words)
        ) / 2

    name_center = center(
        [word for word in header_row if "项目名称" in word[4]]
    )
    spec_center = center(
        [word for word in header_row if "规格型号" in word[4]]
    )
    quantity_center = center(
        [word for word in header_row if word[4] in {"数", "量", "数量"}]
    )
    tax_rate_words = [word for word in header_row if "税率/征收率" in word[4]]
    tax_rate_center = center(tax_rate_words)
    amount_center = center(
        [
            word for word in header_row
            if word[4] in {"金", "额", "金额"}
            and tax_rate_words
            and word[0] < tax_rate_words[0][0]
        ]
    )
    tax_amount_center = center(
        [
            word for word in header_row
            if word[4] in {"税", "额", "税额"}
            and tax_rate_words
            and word[0] > tax_rate_words[0][2]
        ]
    )
    if any(
        value is None
        for value in (
            name_center,
            spec_center,
            quantity_center,
            amount_center,
            tax_rate_center,
            tax_amount_center,
        )
    ):
        return []

    centers = [
        name_center,
        spec_center,
        (spec_center + quantity_center) / 2,
        quantity_center,
        (quantity_center + amount_center) / 2,
        amount_center,
        tax_rate_center,
        tax_amount_center,
    ]
    boundaries = [
        (left + right) / 2
        for left, right in zip(centers, centers[1:])
    ]
    keys = (
        "name",
        "specification",
        "unit",
        "quantity",
        "unit_price",
        "amount",
        "tax_rate",
        "tax_amount",
    )
    header_bottom = max(word[3] for word in header_row)
    total_top = min(
        (
            min(word[1] for word in row)
            for row in word_rows
            if "合计" in "".join(word[4] for word in row)
            and "价税合计" not in "".join(word[4] for word in row)
            and min(word[1] for word in row) > header_bottom
        ),
        default=float("inf"),
    )
    numeric_pattern = re.compile(r"[0-9]+(?:\.[0-9]+)?%?")
    items: list[ocr.CommodityItem] = []
    for row in word_rows:
        if min(word[1] for word in row) <= header_bottom:
            continue
        if min(word[1] for word in row) >= total_top:
            break
        cells: dict[str, list[str]] = {key: [] for key in keys}
        for word in row:
            column = bisect_right(boundaries, (word[0] + word[2]) / 2)
            cells[keys[column]].append(word[4])
        values = {key: "".join(value) or None for key, value in cells.items()}
        has_amounts = any(
            values[key] and numeric_pattern.search(values[key])
            for key in ("quantity", "unit_price", "amount", "tax_rate", "tax_amount")
        )
        if has_amounts:
            items.append({"row": len(items) + 1, **values})
        elif items:
            for key in ("name", "specification"):
                if values[key]:
                    previous = items[-1][key] or ""
                    items[-1][key] = previous + values[key]
    return items


def _extract_pdf_common_fields(
    page_width: float,
    words: list[tuple[float, float, float, float, str]],
) -> ocr.CommonInvoiceFields:
    purchaser_name, purchaser_tax_no = _extract_pdf_party_fields(
        page_width, words, left_side=True
    )
    seller_name, seller_tax_no = _extract_pdf_party_fields(
        page_width, words, left_side=False
    )
    fields = _common_fields(purchaser_name, purchaser_tax_no)
    fields["seller_name"] = seller_name
    fields["seller_tax_no"] = seller_tax_no

    top_text = "".join(
        word[4] for word in sorted(
            (word for word in words if word[1] < 70),
            key=lambda item: (item[1], item[0]),
        )
    )
    invoice_type = re.search(
        r"(?:增值税)?(?:电子)?(?:专用|普通|专用电子|普通电子)发票",
        top_text,
    )
    if invoice_type:
        fields["invoice_type"] = invoice_type.group(0)

    amount_pattern = re.compile(r"[0-9]+(?:,[0-9]{3})*\.[0-9]{2}")
    for row in _pdf_text_rows(words):
        if "合计" not in row or "价税合计" in row:
            continue
        amounts = amount_pattern.findall(row)
        if len(amounts) >= 2:
            fields["amount_without_tax"] = amounts[0].replace(",", "")
            fields["total_tax"] = amounts[1].replace(",", "")
            break
    fields["items"] = _extract_pdf_items(words)
    return fields


def _common_fields(
    purchaser_name: str | None, purchaser_tax_no: str | None
) -> ocr.CommonInvoiceFields:
    return {
        "invoice_no": None,
        "invoice_date": None,
        "invoice_code": None,
        "invoice_type": None,
        "invoice_title": None,
        "digital_invoice_no": None,
        "check_code": None,
        "service_type": None,
        "purchaser_name": purchaser_name,
        "purchaser_tax_no": purchaser_tax_no,
        "purchaser_address_phone": None,
        "purchaser_bank_account": None,
        "seller_name": None,
        "seller_tax_no": None,
        "seller_address_phone": None,
        "seller_bank_account": None,
        "amount_without_tax": None,
        "total_tax": None,
        "amount_in_figures": None,
        "amount_in_words": None,
        "payee": None,
        "checker": None,
        "drawer": None,
        "remarks": None,
        "items": [],
    }


def _parse_fields(
    lines: list[tuple[str, float, float, float]],
) -> tuple[dict, list[str], str | None, str | None]:
    invoice_numbers: list[str] = []
    dates: list[str] = []
    total_amounts: list[str] = []

    for text, _score, y_top, y_bottom in lines:
        number_match = _INVOICE_NO.search(text)
        if number_match:
            invoice_numbers.append(number_match.group(1))

        date_match = _INVOICE_DATE.search(text)
        if date_match:
            dates.append(date_match.group(1))

        total_match = _SMALL_TOTAL.search(text) or _LABELED_TOTAL.search(text)
        if total_match:
            total_amounts.append(total_match.group(1))
            continue

        if "小写" in text or "小寫" in text:
            label_center = (y_top + y_bottom) / 2
            label_height = max(1.0, y_bottom - y_top)
            nearby_amounts = []
            for other_text, _other_score, other_top, other_bottom in lines:
                if other_text == text:
                    continue
                center = (other_top + other_bottom) / 2
                if abs(center - label_center) > max(18.0, label_height * 1.5):
                    continue
                amount_match = _MONEY.search(other_text)
                if amount_match:
                    nearby_amounts.append(amount_match.group(1))
            total_amounts.extend(nearby_amounts)

    errors = []
    record = {"category": "其他"}
    for key, candidates, normalizer, label in (
        ("invoice_no", invoice_numbers, ocr.normalize_invoice_no, "发票号码"),
        ("date", dates, ocr.normalize_date, "开票日期"),
        ("amount", total_amounts, ocr.normalize_amount, "价税合计"),
    ):
        try:
            candidate = _unique(candidates)
            if candidate is None:
                errors.append(f"未能可靠识别{label}")
            else:
                record[key] = normalizer(candidate)
        except ValueError as exc:
            errors.append(f"{label}无法确认：{exc}")

    purchaser_name, purchaser_tax_no = _extract_purchaser_fields(lines)
    return record, errors, purchaser_name, purchaser_tax_no


def _recognize_image_detailed(image_path: str | Path) -> ocr.RecognitionResult:
    """Recognize an image locally; failures are returned for manual entry."""
    started = time.perf_counter()
    try:
        path = _validate_image(image_path)
        raw_result, _timings = _run_engine(path)
        lines = _recognized_lines(raw_result)
        header_retry_done = False
        if not any(_INVOICE_NO.search(line[0]) for line in lines):
            lines.extend(_recognize_invoice_header(path))
            header_retry_done = True
        record, errors, purchaser_name, purchaser_tax_no = _parse_fields(lines)
        with Image.open(path) as image:
            spatial_name, spatial_tax_no, buyer_section_found = (
                _extract_purchaser_fields_from_boxes(raw_result, image.size)
            )
            image_fields = _extract_image_common_fields(raw_result, image.size)
            if image_fields["invoice_type"] is None and not header_retry_done:
                image_fields["invoice_type"] = _recognize_invoice_type_header(path)
        if buyer_section_found:
            purchaser_name = purchaser_name or spatial_name
            has_tax_label = any(
                _PURCHASER_TAX_LABEL.search(line[0]) for line in lines
            )
            if not purchaser_tax_no and not has_tax_label:
                purchaser_tax_no = spatial_tax_no
        common_fields = _common_fields(purchaser_name, purchaser_tax_no)
        for key, value in image_fields.items():
            if value is not None and (key != "items" or value):
                common_fields[key] = value
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if errors:
            return ocr.RecognitionResult(
                error_code="LOCAL_FIELDS_INVALID",
                message="；".join(errors) + "，请核对票面或手动填写。",
                elapsed_ms=elapsed_ms,
                attempts=1,
                common_fields=common_fields,
            )
        return ocr.RecognitionResult(
            record=ocr.InvoiceRecord(**record),
            message="本地 RapidOCR 识别完成，请核对票面。",
            elapsed_ms=elapsed_ms,
            attempts=1,
            common_fields=common_fields,
        )
    except LocalOcrUnavailableError as exc:
        return ocr.RecognitionResult(
            error_code="LOCAL_OCR_UNAVAILABLE",
            message=str(exc),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
    except ValueError as exc:
        return ocr.RecognitionResult(
            error_code="LOCAL_IMAGE_INVALID",
            message=str(exc),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
    except Exception:
        LOGGER.exception("Unexpected error during local RapidOCR recognition")
        return ocr.RecognitionResult(
            error_code="LOCAL_OCR_FAILED",
            message="本地识别遇到意外错误，请查看终端日志或改用百度 OCR/手动填写。",
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )


def recognize_detailed(image_path: str | Path) -> ocr.RecognitionResult:
    """Recognize an image or PDF locally, using searchable PDF text when present."""
    path = Path(image_path).expanduser()
    if path.suffix.lower() != ".pdf":
        return _recognize_image_detailed(path)

    try:
        path = pdf_support.validate_pdf(path)
        page_width, words = pdf_support.first_page_text_words(path)
        pdf_fields = _extract_pdf_common_fields(page_width, words)
        with pdf_support.recognition_image_path(path) as rendered_path:
            result = _recognize_image_detailed(rendered_path)
        pdf_field_keys = (
            "purchaser_name",
            "purchaser_tax_no",
            "seller_name",
            "seller_tax_no",
            "invoice_type",
            "amount_without_tax",
            "total_tax",
            "items",
        )
        if not any(pdf_fields[key] for key in pdf_field_keys):
            return result

        common_fields = dict(result.common_fields or _common_fields(None, None))
        for key in pdf_field_keys:
            if pdf_fields[key] is not None and (
                key != "items" or pdf_fields[key]
            ):
                common_fields[key] = pdf_fields[key]
        return replace(result, common_fields=common_fields)
    except (OSError, RuntimeError, ValueError) as exc:
        return ocr.RecognitionResult(
            error_code="LOCAL_PDF_INVALID",
            message=str(exc),
        )
