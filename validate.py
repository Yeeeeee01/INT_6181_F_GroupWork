"""
validate.py —— 发票校验模块（小组作业 A 部分）

对外接口（与主程序约定的签名）：
    check(记录) -> list[str]
        返回空列表 []    表示校验通过
        返回错误列表     每个元素是一条中文错误提示，例如 ["发票号不能为空。"]

校验规则：
    1. 发票号必须是非空字符串
    2. 日期必须是 YYYY-MM-DD 格式，且不能是未来日期
    3. 金额必须非空且为有限数字，不限制金额大小或正负
    4. 类别必须在白名单里：餐饮 / 交通 / 办公 / 其他
    5. 查重：发票号不能已经存在于数据库中

规则逐条检查，一次性返回所有错误。
"""

import math
import re
from datetime import date, datetime

import database

# 类别白名单（OCR 识别后由用户从中选择）
CATEGORIES = ("餐饮", "交通", "办公", "其他")

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def check(record, check_duplicate=True, db_path=None):
    """
    校验一条发票记录。

    参数：
        record           —— 字典，至少包含 invoice_no / date / amount / category
        check_duplicate  —— 是否查重，默认 True。仅测试纯规则时可传 False。
        db_path          —— 可选数据库路径；默认使用项目数据库。

    返回：
        list[str]，空列表表示没有问题。
    """
    errors = []

    if not isinstance(record, dict):
        return ["记录格式错误：应为字典，包含 invoice_no、date、amount、category。"]

    # ---------- 1. 发票号 ----------
    invoice_no = record.get("invoice_no")
    if not isinstance(invoice_no, str):
        errors.append("发票号必须是字符串。")
        invoice_no_for_db = None
    else:
        invoice_no = invoice_no.strip()
        invoice_no_for_db = invoice_no or None
        if not invoice_no:
            errors.append("发票号不能为空。")

    # ---------- 2. 日期 ----------
    invoice_date = record.get("date")
    if not isinstance(invoice_date, str) or not _DATE_PATTERN.match(invoice_date.strip()):
        errors.append("日期格式错误，应为 YYYY-MM-DD（例如 2026-11-01）。")
    else:
        try:
            parsed_date = datetime.strptime(invoice_date.strip(), "%Y-%m-%d").date()
        except ValueError:
            errors.append("日期格式错误，应为 YYYY-MM-DD（例如 2026-11-01）。")
        else:
            if parsed_date > date.today():
                errors.append("日期不能是未来日期。")

    # ---------- 3. 金额 ----------
    amount = record.get("amount")
    if amount is None or (
        isinstance(amount, str) and not amount.strip()
    ):
        errors.append("金额不能为空。")
    elif isinstance(amount, bool) or not isinstance(amount, (int, float)):
        errors.append("金额必须是数字。")
    elif not math.isfinite(amount):
        errors.append("金额必须是有效数字。")

    # ---------- 4. 类别 ----------
    category = record.get("category")
    if category not in CATEGORIES:
        errors.append("类别必须是：餐饮/交通/办公/其他 之一。")

    # ---------- 5. 查重 ----------
    if check_duplicate and invoice_no_for_db:
        if database.invoice_exists(invoice_no_for_db, db_path=db_path):
            errors.append("该发票号已存在，不允许重复入库。")

    return errors
