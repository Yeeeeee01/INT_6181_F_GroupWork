"""B 模块：中国大陆增值税发票 OCR。

最小代码交付为本文件和同目录的 manual_input.py，无其他本地模块依赖。
安装运行依赖：python -m pip install "requests>=2.32,<3" "Pillow>=12,<13" "python-dotenv>=1,<2"
已在 Python 3.14.7 验证；换用小组其他 Python 环境时须重新测试。

配置：在本文件同目录创建 .env，分别填写 BAIDU_API_KEY 和 BAIDU_SECRET_KEY，
或设置同名环境变量（环境变量优先）。应用须具有“增值税发票识别”权限。
密钥不写入源码或公开材料；共享 .env 仅在确认仓库为 Private 后提交，详见 README_B.md。
本模块自动获取和缓存访问凭据。

队友调用：
    import ocr
    from manual_input import collect_invoice
    recognized = ocr.recognize(image_path)
    confirmed = collect_invoice(recognized)
    if confirmed is not None:
        record = confirmed.record  # 交给 A 校验，再由上层决定入库

recognize(path) 返回 invoice_no、date、amount、category 四字段字典，失败返回 None。
recognize_detailed(path) 另外返回 common_fields（常用字段）和 field_warnings。
常用字段是未经人工确认的 OCR 文本快照，与四字段业务记录分别交接。
本模块不等待输入、不写数据库。
支持单张 JPG/JPEG/PNG；金额为价税合计，类别“其他”须在人工流程明确选择。
票号保留前导零和完整位数，未来日期、报销限额和重复票号由 A 判定。
当前只开发和测试中国大陆发票；香港识别待后续有明确需求时再评估。
测试、实测和审查摘要见 README_B.md，原始开发记录保留本地。

建议阅读顺序：
1. 先看文件末尾 recognize / recognize_detailed，了解上层调用契约。
2. 再看 BaiduInvoiceClient.recognize，了解图片、鉴权、请求和失败返回的流程。
3. 最后看 normalize_* 和 _image_form，了解每种字段及图片如何检查。

来源说明：百度接口地址、请求参数、响应字段依据下方官方文档与示例；
本文件中的客户端封装、字段规范化、校验和异常恢复由本项目在 AI 辅助下实现。
真正的图片文字识别由百度云端服务完成。

接口依据：https://ai.baidu.com/ai-doc/OCR/nk3h7xy2t
鉴权依据：https://ai.baidu.com/ai-doc/REFERENCE/Lkru0zoz4
"""
# 延后解析类型注解；类型标注用于阅读和工具检查，不代替实际输入校验。
from __future__ import annotations

# 标准库：图片编码、数据结构、日期和金额解析、内存字节流、日志与文件操作。
import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import logging
import math
import os
from pathlib import Path
import re
import time
from typing import TypedDict
import unicodedata
from urllib.parse import urlencode

# 第三方运行依赖：配置读取、图片预检和 HTTP 请求；其余导入均来自标准库。
from dotenv import dotenv_values
from PIL import Image, UnidentifiedImageError
import requests

# 使用模块日志器，由主程序决定日志输出位置；本模块只记录安全的错误类别。
LOGGER = logging.getLogger(__name__)

# 百度官方服务地址。凭据和 token 由运行时提供，不写入以下常量。
TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
INVOICE_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice"
# 项目采用的请求体上限为 8 MiB。原始文件和编码后的请求体都会检查，
# 因为 Base64 及 URL 编码会增加体积，小于此值的原图仍可能编码后超限。
MAX_FORM_BYTES = 8 * 1024 * 1024


# 一、交给其他组员的数据结构与本地配置

class InvoiceRecord(TypedDict):
    """小组约定的四字段记录；TypedDict 在运行时仍是普通字典。

    字段类型由下面的解析函数实际保证。source、is_edited 等元数据
    放在 manual_input.InputResult 中，避免改变 A/C 已约定的业务字典结构。
    """

    invoice_no: str  # 号码始终是字符串，保留开头的 0 和全部位数。
    date: str        # YYYY-MM-DD；这里只保证日历有效，不判断是否允许报销。
    amount: float    # 大陆票面的价税合计；保持原小组接口的 float 类型。
    category: str    # OCR 阶段为占位值，之后由用户明确选择。


class CommodityItem(TypedDict):
    """常用商品/服务明细；各列按官方 row 行号对齐，不按数组位置拼接。

    除行号外均保留为文本，避免单价、数量的小数精度或免税标识被错误转换。
    某一列缺失为 None；不以 0 代替缺失值，不自动计算行金额。
    """

    row: int
    name: str | None
    specification: str | None
    unit: str | None
    quantity: str | None
    unit_price: str | None
    amount: str | None
    tax_rate: str | None
    tax_amount: str | None


class CommonInvoiceFields(TypedDict):
    """常用识别字段。标量是 OCR 文本或 None，items 为逐行明细。

    这是识别时的快照，后续 collect_invoice 修改 record 不会覆盖它。
    金额/日期在这里保留文本；A 接收的标准化数值仍来自四字段 record。
    digital_invoice_no 是附带保留的条件性字段，仅部分票型有值。
    """

    invoice_no: str | None
    invoice_date: str | None
    invoice_code: str | None
    invoice_type: str | None
    invoice_title: str | None
    digital_invoice_no: str | None
    check_code: str | None
    service_type: str | None
    purchaser_name: str | None
    purchaser_tax_no: str | None
    purchaser_address_phone: str | None
    purchaser_bank_account: str | None
    seller_name: str | None
    seller_tax_no: str | None
    seller_address_phone: str | None
    seller_bank_account: str | None
    amount_without_tax: str | None
    total_tax: str | None
    amount_in_figures: str | None
    amount_in_words: str | None
    payee: str | None
    checker: str | None
    drawer: str | None
    remarks: str | None
    items: list[CommodityItem]


# 常用字段清单：项目键名、百度官方键名、面向队友/用户的中文标签。
# 使用明确白名单，避免把 token、调试内容或未经约定的服务字段直接向外返回。
_COMMON_TEXT_FIELDS = (
    ("invoice_no", "InvoiceNum", "发票号码"),
    ("invoice_date", "InvoiceDate", "开票日期"),
    ("invoice_code", "InvoiceCode", "发票代码"),
    ("invoice_type", "InvoiceType", "发票种类"),
    ("invoice_title", "InvoiceTypeOrg", "发票名称"),
    ("digital_invoice_no", "InvoiceNumDigit", "数电票号（条件字段）"),
    ("check_code", "CheckCode", "校验码"),
    ("service_type", "ServiceType", "消费类型参考"),
    ("purchaser_name", "PurchaserName", "购买方名称/抬头"),
    ("purchaser_tax_no", "PurchaserRegisterNum", "购买方税号"),
    ("purchaser_address_phone", "PurchaserAddress", "购买方地址及电话"),
    ("purchaser_bank_account", "PurchaserBank", "购买方开户行及账号"),
    ("seller_name", "SellerName", "销售方名称"),
    ("seller_tax_no", "SellerRegisterNum", "销售方税号"),
    ("seller_address_phone", "SellerAddress", "销售方地址及电话"),
    ("seller_bank_account", "SellerBank", "销售方开户行及账号"),
    ("amount_without_tax", "TotalAmount", "不含税合计"),
    ("total_tax", "TotalTax", "合计税额"),
    ("amount_in_figures", "AmountInFiguers", "价税合计（小写）"),
    ("amount_in_words", "AmountInWords", "价税合计（大写）"),
    ("payee", "Payee", "收款人"),
    ("checker", "Checker", "复核人"),
    ("drawer", "NoteDrawer", "开票人"),
    ("remarks", "Remarks", "备注"),
)
_COMMON_ITEM_FIELDS = (
    ("name", "CommodityName", "商品/服务名称"),
    ("specification", "CommodityType", "规格型号"),
    ("unit", "CommodityUnit", "单位"),
    ("quantity", "CommodityNum", "数量"),
    ("unit_price", "CommodityPrice", "单价"),
    ("amount", "CommodityAmount", "行金额"),
    ("tax_rate", "CommodityTaxRate", "税率"),
    ("tax_amount", "CommodityTax", "行税额"),
)
COMMON_FIELD_LABELS = {name: label for name, _, label in _COMMON_TEXT_FIELDS}
COMMON_FIELD_LABELS["items"] = "商品/服务明细"
COMMON_ITEM_FIELD_LABELS = {"row": "行号", **{name: label for name, _, label in _COMMON_ITEM_FIELDS}}

# 仅约束扩展信息的处理量；达到限制时给出 field_warnings，不隐藏截断。
MAX_COMMON_TEXT_CHARS = 4096
MAX_COMMON_ROWS = 1000


class OcrError(Exception):
    """本模块预期故障的内部表示，最终由识别入口转换成失败结果。

    code 是便于程序判断的稳定类别；message 是可展示的中文说明。
    retryable 只表示是否允许消耗一次重试机会，实际次数仍由调用层限制。
    为避免带出凭据，创建此错误时不拼接原始网络异常、URL 或服务原文。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        # 保留 Exception 的常规字符串行为，同时提供结构化属性。
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


@dataclass(frozen=True)
class OcrConfig:
    """一次客户端使用的配置快照。

    frozen=True 避免创建客户端后随意修改配置。repr=False 使常规对象打印
    不包含密钥。两个超时分别约束连接和读取等待，并非整个识别操作的总时限。
    """

    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    connect_timeout: float = 3.0
    read_timeout: float = 8.0

    def __post_init__(self):
        """dataclass 构造完成后检查配置，尽早阻止空密钥或非法超时。"""
        if not self.api_key.strip() or not self.secret_key.strip():
            raise OcrError("CONFIG_MISSING", "尚未配置百度 OCR 密钥，请按 ocr.py 文件头说明设置本地 .env；也可直接手动录入。")
        if not all(math.isfinite(x) and x > 0 for x in (self.connect_timeout, self.read_timeout)):
            raise ValueError("超时必须为正的有限秒数")

    @classmethod
    def from_env(cls) -> OcrConfig:
        """读取本文件同目录的 .env，并让同名进程环境变量优先。

        路径根据 __file__ 计算，因此从别的工作目录调用也能找到项目配置。
        使用 dotenv_values 而不是修改 os.environ，避免影响主程序和测试。
        没有 .env 时仍可完全通过环境变量配置；都没有时由 __post_init__ 报错。
        """
        path = Path(__file__).resolve().parent / ".env"
        try:
            # 禁用配置值插值，按文件中的字面值读取凭据。
            local = dotenv_values(path, interpolate=False) if path.is_file() else {}
        except (OSError, UnicodeError):
            raise OcrError("CONFIG_UNREADABLE", "无法读取项目 .env，请检查文件编码和读取权限。") from None
        # 环境变量即使显式设为空也会优先，然后交给配置校验处理。
        # 列表顺序对应 OcrConfig 的 api_key、secret_key；这里不输出它们的值。
        values = [os.environ.get(key, local.get(key)) or "" for key in ("BAIDU_API_KEY", "BAIDU_SECRET_KEY")]
        return cls(*values)


@dataclass
class RecognitionResult:
    """详细识别结果，供需要排错或展示状态的上层代码使用。

    成功时 record 非空；预期失败时 record=None，并提供 error_code/message。
    attempts 只统计发票识别 POST 尝试，不包含获取 token 的请求；
    因而图片预检或首次鉴权就失败时，attempts 可以为 0。
    common_fields 提供常用字段快照；缺少核心字段时也可能保留部分识别内容。
    field_warnings 说明可选字段的异常，不代表业务通过或票面已核验。
    """

    record: InvoiceRecord | None = None
    error_code: str | None = None
    message: str = ""
    request_id: str | None = None  # 经过过滤的数字服务标识，便于排查请求。
    elapsed_ms: int = 0           # 本次处理耗时，包含预检、鉴权和重试等待。
    attempts: int = 0
    common_fields: CommonInvoiceFields | None = None
    field_warnings: tuple[str, ...] = ()
    purchaser_name: str | None = None

    @property
    def ok(self) -> bool:
        """表示已读出有效字段；报销业务是否通过仍由 A 决定。"""
        return self.record is not None


# 二、字段规范化：同时供 OCR 响应和人工录入复用

def _text(value: object) -> str:
    """要求原始值为不超过 128 字符的文本，再做 Unicode 兼容规范化。

    NFKC 可以把全角数字、全角货币符号等转换成兼容形式；
    strip 去掉两端空白，具体字段是否允许内部空白由各自函数决定。
    失败抛 ValueError，由响应解析层或人工输入层分别处理。
    """
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("字段应为不超过 128 字符的文本")
    return unicodedata.normalize("NFKC", value).strip()


def normalize_invoice_no(value: object) -> str:
    """输出大陆发票号码字符串，清理空白，保留前导零与完整长度。

    只接受 ASCII 数字，不把易混淆的字母 O 猜成 0。
    不在这里强制八位或二十位，票型与号码长度政策由 A 校验。
    """
    text = re.sub(r"\s+", "", _text(value))
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError("大陆发票号码应为数字文本；请核对图片，不自动猜测字母 O 或补齐位数")
    return text


def normalize_date(value: object) -> str:
    """把支持的年月日排版转换为 YYYY-MM-DD，非法日历日期抛 ValueError。

    strptime 会检查月份、天数和闰年；依次尝试格式而不猜测日/月顺序。
    未来日期在这里仍可解析，后续是否允许入库由 A 判断。
    """
    text = re.sub(r"\s+", "", _text(value))
    for fmt in ("%Y-%m-%d", "%Y年%m月%d日", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            # 当前格式不匹配就试下一种；全部失败后统一给出可读提示。
            pass
    raise ValueError("日期应为有效的年、月、日，例如 2026-11-01")


def normalize_amount(value: object) -> float:
    """解析人民币金额并返回兼容四字段接口的 float。

    接受文本或常见数值类型、人民币前缀和规范千位分隔符，最多两位小数。
    用 Decimal 检查十进制含义及 float 往返精度，失败时不自动舍入。
    0、负数、超过报销上限的金额仍可如实解析，业务判定留给 A。
    """
    # Python 中 bool 是 int 的子类，所以必须先排除 True/False。
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("金额类型不正确")
    text = _text(str(value))
    # 这里只去掉开头的人民币标识；当前不提供港币或其他币种的解析。
    text = re.sub(r"^(?:人民币|RMB|CNY|¥)\s*", "", text, flags=re.IGNORECASE)
    # 整段文本必须匹配：可选正负号 + 普通数字或规范三位分组 + 最多两位小数。
    # 因而 1,234.50 可用，1,23.45、科学计数、占位符和 NaN 会被拒绝。
    if not re.fullmatch(r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?", text):
        raise ValueError("金额应为数字且最多两位小数，例如 128.50；不能使用占位符或 NaN")
    try:
        number = Decimal(text.replace(",", ""))
        result = float(number)
    except (InvalidOperation, OverflowError, ValueError):
        raise ValueError("金额不能可靠解析") from None
    if not number.is_finite() or not math.isfinite(result):
        raise ValueError("金额必须为有限数值")
    # str(result) 得到 float 的十进制展示，再与原 Decimal 比较，拒绝明显的精度丢失。
    # 这不代表 float 能精确存储所有十进制小数，后续金额累计仍需 A 的精确金额方案。
    if Decimal(str(result)) != number:
        raise ValueError("金额超出兼容接口可可靠表示的精度，请人工核对")
    return result


def normalize_response(payload: object) -> InvoiceRecord:
    """将百度 vat_invoice 响应转换为小组的四字段记录。

    payload 应含 words_result 映射。结构不符抛 RESPONSE_INVALID；
    关键字段缺失或不可靠时抛 FIELDS_INVALID，交给上层走手动兜底。
    字段名称依据官方文档；本函数中的选择、检查和规范化属于项目处理逻辑。
    """
    if not isinstance(payload, Mapping) or not isinstance(payload.get("words_result"), Mapping):
        raise OcrError("RESPONSE_INVALID", "识别响应结构不正确，请重试或手动录入。")
    words = payload["words_result"]
    try:
        no = words.get("InvoiceNum", "")
        digital = words.get("InvoiceNumDigit", "")
        # 官方字段：InvoiceNum 为票号；InvoiceNumDigit 可提供部分票型的数电号码。
        # 只有后者是有效二十位数字时才优先采用，否则继续使用 InvoiceNum。
        # 两个二十位号码同时存在且不一致时，不自动决定哪个正确。
        if isinstance(digital, str) and re.fullmatch(r"[0-9]{20}", digital.strip()):
            if isinstance(no, str) and re.fullmatch(r"[0-9]{20}", no.strip()) and no.strip() != digital.strip():
                raise ValueError("两个二十位票号相互冲突")
            no = digital
        return InvoiceRecord(
            invoice_no=normalize_invoice_no(no),
            date=normalize_date(words.get("InvoiceDate")),
            # AmountInFiguers 是官方字段的原有拼写，表示价税合计。
            # 不可自行改名，也不在缺失时回退到不含税合计 TotalAmount。
            amount=normalize_amount(words.get("AmountInFiguers")),
            category="其他",  # 仅为兼容接口占位，后续必须让用户明确选择。
        )
    except ValueError as exc:
        # 下层字段函数提供格式原因；这里只转为识别层的统一预期错误。
        raise OcrError("FIELDS_INVALID", f"票号、开票日期或价税合计缺失或有歧义：{exc}。请手动录入。") from None


def _common_text(value: object, label: str, warnings: list[str], *, numeric: bool = False) -> str | None:
    """读取扩展字段文本；缺失返回 None，格式异常只记录安全警告。

    保留文字、百分号、免税标识及小数位，不进行金额推导或业务判断。
    金额等官方可能返回数字的字段允许有限数值转文本；税号/号码不猜补前导零。
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
    elif numeric and type(value) in (int, float):
        if isinstance(value, float) and not math.isfinite(value):
            warnings.append(f"{label}：返回非有限数值，未采用。")
            return None
        try:
            text = str(value)
        except ValueError:
            warnings.append(f"{label}：数值文本过长，未采用。")
            return None
    else:
        warnings.append(f"{label}：返回格式异常，未采用。")
        return None
    if len(text) > MAX_COMMON_TEXT_CHARS:
        warnings.append(f"{label}：文本超过长度限制，未采用。")
        return None
    return text or None


def _common_row(value: object) -> int | None:
    """只接受明确的正整数行号；缺失时不按数组位置猜测所属商品。"""
    if type(value) is int:
        row = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,4}", value.strip()):
        row = int(value.strip())
    else:
        return None
    return row if 1 <= row <= MAX_COMMON_ROWS else None


def _extract_common_fields(payload: object) -> tuple[CommonInvoiceFields, tuple[str, ...]]:
    """从官方响应白名单中提取“常用字段”，与核心四字段验证分别处理。

    缺失可选字段不会令整票识别失败；异常字段返回 None，并记录中文警告。
    各明细列按 row 合并，缺列保留 None；同一行同一列出现冲突时不取最后一个值。
    返回新字典和新明细对象，不修改或持有原始服务响应中的可变对象。
    """
    if not isinstance(payload, Mapping) or not isinstance(payload.get("words_result"), Mapping):
        raise OcrError("RESPONSE_INVALID", "识别响应结构不正确，请重试或手动录入。")
    words = payload["words_result"]
    warnings: list[str] = []
    fields = {
        name: _common_text(words.get(source), label, warnings,
                           numeric=source in {"TotalAmount", "TotalTax", "AmountInFiguers"})
        for name, source, label in _COMMON_TEXT_FIELDS
    }
    rows: dict[int, CommodityItem] = {}
    seen: set[tuple[int, str]] = set()
    conflicts: set[tuple[int, str]] = set()
    for name, source, label in _COMMON_ITEM_FIELDS:
        column = words.get(source)
        if column is None:
            continue
        if not isinstance(column, list):
            warnings.append(f"明细{label}：返回结构异常，未采用该列。")
            continue
        if len(column) > MAX_COMMON_ROWS:
            warnings.append(f"明细{label}：超过处理上限，仅处理前 {MAX_COMMON_ROWS} 个单元格。")
        for cell in column[:MAX_COMMON_ROWS]:
            if not isinstance(cell, Mapping):
                warnings.append(f"明细{label}：单元格格式异常，未采用。")
                continue
            row = _common_row(cell.get("row"))
            if row is None:
                warnings.append(f"明细{label}：行号缺失或无效，未按位置猜测。")
                continue
            value = _common_text(cell.get("word"), f"明细{label}", warnings,
                                 numeric=name in {"quantity", "unit_price", "amount", "tax_amount"})
            item = rows.setdefault(row, {"row": row, **{key: None for key, _, _ in _COMMON_ITEM_FIELDS}})
            cell_key = (row, name)
            if cell_key in conflicts:
                continue
            if cell_key in seen:
                if item[name] != value:
                    item[name] = None
                    conflicts.add(cell_key)
                    warnings.append(f"明细{label}：同一行返回相互冲突的值，未自动选择。")
                continue
            seen.add(cell_key)
            item[name] = value
    fields["items"] = [
        rows[row] for row in sorted(rows)
        if any(rows[row][key] is not None for key, _, _ in _COMMON_ITEM_FIELDS)
    ]
    return fields, tuple(dict.fromkeys(warnings))


# 三、图片预检与请求编码

def _image_form(image_path: str | Path) -> str:
    """读取本地图片并返回可直接发送的表单字符串。

    流程为：扩展名和可读性 → 原图大小 → Pillow 格式/尺寸/完整性检查
    → Base64 → urlencode → 编码后大小检查。检查失败抛对应 OcrError。
    保留原图字节，不在这里缩放、裁剪或写回文件。
    """
    try:
        path = Path(image_path)
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise OcrError("IMAGE_FORMAT", "请选择 JPG、JPEG 或 PNG 图片。")
        with path.open("rb") as handle:
            # 多读一个字节用于判断超限，避免一次把任意大的文件全部读入内存。
            raw = handle.read(MAX_FORM_BYTES + 1)
    except (OSError, ValueError, TypeError):
        raise OcrError("IMAGE_UNREADABLE", "图片不存在或无法读取，请检查路径和文件权限。") from None
    if len(raw) > MAX_FORM_BYTES:
        raise OcrError("IMAGE_TOO_LARGE", "图片过大，请使用较小的原始图片。")
    try:
        with Image.open(BytesIO(raw)) as image:
            # 同时检查后缀和实际内容，防止把其他格式简单改名后上传。
            expected = "PNG" if path.suffix.lower() == ".png" else "JPEG"
            if image.format != expected:
                raise OcrError("IMAGE_FORMAT", "文件扩展名与真实图片格式不一致。")
            if min(image.size) < 15 or max(image.size) > 4096:
                raise OcrError("IMAGE_DIMENSIONS", "图片最短边至少 15 像素、最长边至多 4096 像素。")
            # Pillow 检查图片文件结构，不代表发票文字一定清晰或能被 OCR 识别。
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise OcrError("IMAGE_INVALID", "图片损坏或不是有效图片，请重新选择。") from None
    # 官方接口要求 image 为 Base64 后的表单数据；type=normal 对应当前普通票流程。
    # 这里先编码为完整字符串，发送时 data=form，避免让 requests 再次编码。
    form = urlencode({"image": base64.b64encode(raw).decode("ascii"), "type": "normal"})
    if len(form.encode("ascii")) > MAX_FORM_BYTES:
        raise OcrError("IMAGE_TOO_LARGE", "图片编码后的请求超过 8 MiB，请使用较小的图片。")
    return form


# 四、百度客户端：HTTP 传输、鉴权与有边界的识别流程

class BaiduInvoiceClient:
    """复用 Session 和 token 的同步客户端，适用于同一顺序调用流程。

    session、clock、sleep 可注入测试替身，便于离线模拟响应和时间推进。
    默认分别使用真实 requests.Session、单调时钟和等待函数。
    对象中的 token 与重试状态不作线程同步，因此不应在线程间共享本对象。
    """

    def __init__(self, config: OcrConfig, *, session=None, clock=time.monotonic, sleep=time.sleep):
        # 外部显式提供 session 时沿用它；正常使用创建可复用连接的会话。
        self.config = config
        self.session = session if session is not None else requests.Session()
        self._clock, self._sleep = clock, sleep
        self._token: str | None = None
        self._expires_at = 0.0  # 单调时钟上的到期点，不是 Unix 时间戳。

    def _post(self, url: str, **kwargs) -> dict:
        """完成一次 POST 并检查 HTTP 状态和 JSON 类型，不在本层重试。

        kwargs 接收 data、params、headers 等请求参数。网络与 HTTP 临时错误
        会带 retryable 标记；是否重试由鉴权/识别层按自己的总预算决定。
        """
        try:
            # 超时分别用于连接和读取；禁用重定向，保留 requests 默认 TLS 校验。
            response = self.session.post(
                url, timeout=(self.config.connect_timeout, self.config.read_timeout),
                allow_redirects=False, **kwargs,
            )
        except (requests.Timeout, requests.ConnectionError):
            # requests 原始异常可能包含带 token 的 URL，因此只返回固定中文消息。
            raise OcrError("NETWORK", "OCR 网络连接失败或超时，可稍后重试或手动录入。", retryable=True) from None
        except requests.RequestException:
            raise OcrError("NETWORK", "OCR 网络请求失败，请检查本地网络配置。") from None
        if response.status_code != 200:
            # 只把列出的临时 HTTP 状态视为可重试；权限等其他状态直接交给上层处理。
            retryable = response.status_code in {429, 500, 502, 503, 504}
            raise OcrError("HTTP_ERROR", "OCR 服务暂不可用或拒绝请求，请检查权限后重试。", retryable=retryable)
        try:
            # HTTP 200 只表示 HTTP 请求成功；后面还要检查 JSON、服务错误码及字段。
            payload = response.json()
        except ValueError:
            raise OcrError("RESPONSE_INVALID", "OCR 服务没有返回有效 JSON，请稍后重试。") from None
        if not isinstance(payload, dict):
            raise OcrError("RESPONSE_INVALID", "OCR 服务返回的数据类型不正确。")
        return payload

    def _get_token(self, *, force: bool = False) -> str:
        """返回可用 token；没有缓存、已到期或 force=True 时重新鉴权。

        每次鉴权最多两次 POST，临时失败只重试一次。token 只保存在对象内存，
        不写入文件。服务明确报告 token 失效时，识别流程使用 force=True 刷新。
        """
        if not force and self._token and self._clock() < self._expires_at:
            return self._token
        # 刷新失败时不能重新使用已经失效的旧 token。
        self._token, self._expires_at = None, 0.0
        for attempt in range(2):
            # 从请求开始时计算到期点，避免把请求耗时额外算进 token 可用时间。
            started = self._clock()
            try:
                # 参数名称依据百度鉴权协议。凭据放在 POST 表单中传递。
                data = self._post(TOKEN_URL, data={
                    "grant_type": "client_credentials", "client_id": self.config.api_key,
                    "client_secret": self.config.secret_key,
                })
                token, lifetime = data.get("access_token"), data.get("expires_in")
                # 鉴权失败原文不直接展示，避免把服务响应中的敏感内容带给用户。
                if "error" in data:
                    raise OcrError("AUTH_FAILED", "百度鉴权失败，请核对 API Key、Secret Key 和应用服务权限。")
                if (not isinstance(token, str) or not token or isinstance(lifetime, bool)
                        or not isinstance(lifetime, (int, float)) or not math.isfinite(lifetime) or lifetime <= 0):
                    raise OcrError("RESPONSE_INVALID", "鉴权响应缺少有效 token 或有效期。")
                self._token = token
                # 提前少量刷新：保留有效期的 10% 或 60 秒中的较小值，减少临界过期。
                self._expires_at = started + lifetime - min(60, lifetime * 0.1)
                return token
            except OcrError as exc:
                if not exc.retryable or attempt == 1:
                    # token 获取已经使用自己的重试预算，不能让外层再携旧 token 重试。
                    raise OcrError(exc.code, exc.message) from None
                self._sleep(0.25)  # 有界的短暂等待，不使用持续循环请求。
        # 正常控制流应已返回 token 或抛出错误，此处用于暴露不应发生的程序问题。
        raise AssertionError("unreachable")

    def recognize(self, image_path: str | Path) -> RecognitionResult:
        """单张图片识别主流程，成功或预期失败都返回 RecognitionResult。

        图片检查在鉴权之前执行，非法文件不会消耗图片识别请求。
        识别 POST 全程最多三次：共享一次临时错误重试和一次 token 失效刷新机会。
        初始鉴权及刷新各最多两次，因此整个操作最多七次 HTTP POST。
        只捕获 OcrError，程序本身的意外错误仍暴露给开发者排查。
        """
        started = self._clock()
        attempts = 0
        request_id = None
        common_fields = None
        field_warnings = ()
        try:
            form = _image_form(image_path)
            # 两个标记分别限制 token 刷新和临时错误重试，整个循环共享这两项预算。
            refreshed, retried = False, False
            token = self._get_token()
            for _ in range(3):
                attempts += 1  # 在发送前计数，因此超时的尝试也会计入。
                try:
                    # 官方调用协议：URL 参数携带 token，表单携带已编码的图片。
                    data = self._post(INVOICE_URL, params={"access_token": token}, data=form,
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
                    log_id = data.get("log_id")
                    # 只接受数字型服务请求标识，不记录任意服务返回文本。
                    request_id = str(log_id) if re.fullmatch(r"[0-9]{1,32}", str(log_id)) else None
                    code = str(data.get("error_code", ""))
                    # 110/111 表示无效/过期凭据；只允许一次主动刷新，
                    # 避免权限或配置异常时不断申请新 token。
                    if code in {"110", "111"} and not refreshed:
                        refreshed = True
                        token = self._get_token(force=True)
                        continue
                    if code:
                        # 服务错误码依据官方说明；项目用固定消息映射，未知码也安全返回。
                        # 只有 18（频率限制）和 282000（内部错误）允许进入临时重试分支。
                        messages = {
                            "17": "OCR 调用额度不足，请检查控制台额度或改用手动录入。",
                            "18": "OCR 请求过于频繁，请稍后重试。",
                            "110": "OCR 访问凭据无效，请检查服务配置。",
                            "111": "OCR 访问凭据已过期，请检查服务配置。",
                            "6": "当前应用没有发票识别权限，请先开通对应服务。",
                            "282103": "无法识别图片中的目标发票，请更换清晰图片或手动录入。",
                        }
                        raise OcrError("SERVICE_ERROR", messages.get(code, "百度 OCR 返回业务错误，请检查服务权限或手动录入。"), retryable=code in {"18", "282000"})
                    # 先保留可选的常用字段，即使随后核心字段缺失，详细入口也能用于人工核对。
                    common_fields, field_warnings = _extract_common_fields(data)
                    # 网络成功还不够，必须把三个关键字段可靠转换后才算识别成功。
                    record = normalize_response(data)
                    return RecognitionResult(record=record, request_id=request_id,
                                             elapsed_ms=round((self._clock() - started) * 1000), attempts=attempts,
                                             common_fields=common_fields, field_warnings=field_warnings,
                                             purchaser_name=common_fields["purchaser_name"])
                except OcrError as exc:
                    # 永久错误、已经用过临时重试、或已到总次数上限，均停止本轮操作。
                    if not exc.retryable or retried or attempts == 3:
                        raise
                    retried = True
                    self._sleep(0.25)
            raise OcrError("AUTH_FAILED", "访问凭据刷新后仍无法识别，请手动录入并检查配置。")
        except OcrError as exc:
            # 在本层把预期故障收敛为结果对象。日志仅含稳定错误类别，
            # 不输出原始服务响应、完整请求 URL、token 或用户票面内容。
            LOGGER.warning("OCR 失败，错误类别=%s", exc.code)
            return RecognitionResult(error_code=exc.code, message=exc.message, request_id=request_id,
                                     elapsed_ms=round((self._clock() - started) * 1000), attempts=attempts,
                                     common_fields=common_fields, field_warnings=field_warnings,
                                     purchaser_name=(
                                         common_fields["purchaser_name"]
                                         if common_fields is not None
                                         else None
                                     ))


# 五、队友使用的两个公共入口

# 同一进程的顺序调用复用客户端和 token；进程结束后缓存自然消失。
_default_client: BaiduInvoiceClient | None = None


def recognize_detailed(image_path: str | Path, *, client: BaiduInvoiceClient | None = None) -> RecognitionResult:
    """详细入口：需要中文失败原因、耗时和请求次数时使用。

    image_path 为图片路径；client 为可选的显式客户端，便于调用方复用或测试。
    未提供 client 时读取本地配置，并按配置复用模块级默认客户端。
    配置缺失也返回失败结果；意外的编程错误不被大范围捕获。
    common_fields 为常用字段快照，field_warnings 为可选字段解析警告。
    常用字段不是人工确认后的业务记录；上层按需单独展示或保存。
    """
    global _default_client
    if client is None:
        try:
            config = OcrConfig.from_env()
        except OcrError as exc:
            return RecognitionResult(error_code=exc.code, message=exc.message)
        # 每次读取当前配置；密钥或超时配置变化时更换客户端，避免沿用旧凭据缓存。
        if _default_client is None or _default_client.config != config:
            _default_client = BaiduInvoiceClient(config)
        client = _default_client
    return client.recognize(image_path)


def recognize(image_path: str | Path) -> InvoiceRecord | None:
    """小组原有的最简入口：成功返回四字段字典，预期失败返回 None。

    本函数只识别，不等待输入、不写数据库。C 可把返回值直接传给
    manual_input.collect_invoice：有记录时确认/修改，为 None 时手动填写。
    使用者如需显示失败原因，应改用 recognize_detailed，而不是在这里猜测。
    """
    return recognize_detailed(image_path).record
