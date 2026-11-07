"""
database.py —— 数据库操作模块（小组作业 A 部分）

对外接口（与 OCR、校验、主程序约定的签名）：
    insert(记录, 照片路径) -> bool      成功返回 True；票号重复（查重失败）返回 False
    invoice_exists(票号) -> bool        该票号是否已入库
    delete_invoice(票号) -> bool        删除一张发票，成功返回 True
    query_invoices(...) -> list[dict]   查询发票，可按报销人 / 月份 / 类别组合筛选

"记录"统一使用字典：
    {
        "invoice_no": "12345678",     # 非空发票号码文本
        "date":       "2026-11-01",   # YYYY-MM-DD
        "amount":     128.50,         # 有限数字，不限制金额范围
        "category":   "餐饮",          # 餐饮 / 交通 / 办公 / 其他
        "reimburser": "张三",          # 报销人，可选，缺省为 "未知"
    }

查询结果字典的键与上面的记录一致，另含 id、photo_path、created_at。
"""

import os
import sqlite3
from contextlib import closing
from datetime import datetime

# 数据库文件固定放在本文件所在目录，避免从不同工作目录运行时找不到库
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invoices.db")

# 查询时统一返回给上层的字段（把 invoice_date 别名成 date，与记录格式保持一致）
_SELECT_FIELDS = """
    id, invoice_no, invoice_date AS date, amount, category,
    reimburser, photo_path, created_at, is_edited
"""


def get_connection(db_path=None):
    """打开数据库连接。db_path 仅供测试覆盖，正常调用不用传。"""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def invoice_exists(invoice_no, db_path=None):
    """
    查重：票号是否已经在 invoices 表里。
    数据库还没初始化时返回 False（方便单独测试 validate）。
    """
    try:
        with closing(get_connection(db_path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM invoices WHERE invoice_no = ? LIMIT 1",
                (str(invoice_no),),
            ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def insert(record, photo_path, db_path=None):
    """
    入库。签名与约定一致：insert(记录, 照片路径)。

    返回：
        True  —— 插入成功
        False —— 票号已存在（由表上的 UNIQUE 约束挡住），查重失败

    数据库未初始化时抛出 RuntimeError，提示先运行 init_database.py。
    """
    if not isinstance(record, dict):
        raise TypeError("记录必须是字典。")

    invoice_no = record.get("invoice_no")
    invoice_date = record.get("date")
    category = record.get("category")
    if not isinstance(invoice_no, str) or not invoice_no.strip():
        raise ValueError("记录缺少有效的发票号（invoice_no）。")
    if not isinstance(invoice_date, str) or not invoice_date.strip():
        raise ValueError("记录缺少有效的日期（date）。")
    if not isinstance(category, str) or not category.strip():
        raise ValueError("记录缺少有效的类别（category）。")

    # 金额允许传 int / float / 数字字符串，统一转成 float 入库
    try:
        if isinstance(record.get("amount"), bool):
            raise TypeError
        amount = float(record["amount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("记录缺少有效的金额（amount）。") from exc

    # 报销人：兼容 reimburser / employee / name 三种键名，缺省为 "未知"
    reimburser = (
        record.get("reimburser")
        or record.get("employee")
        or record.get("name")
        or "未知"
    )
    reimburser = str(reimburser).strip() or "未知"

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with closing(get_connection(db_path)) as conn, conn:
            conn.execute(
                """
                INSERT INTO invoices
                    (invoice_no, invoice_date, amount, category,
                     reimburser, photo_path, created_at, is_edited)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invoice_no.strip(),
                    invoice_date.strip(),
                    amount,
                    category.strip(),
                    reimburser,
                    str(photo_path),
                    created_at,
                    int(bool(record.get("is_edited", False))),
                ),
            )
        return True
    except sqlite3.IntegrityError:
        # 触发 UNIQUE(invoice_no)：同一票号重复入库
        return False
    except sqlite3.OperationalError as exc:
        raise RuntimeError("数据库尚未初始化，请先运行 init_database.py。") from exc


def delete_invoice(invoice_no, db_path=None):
    """按票号删除一条记录；确实删掉了返回 True，票号不存在返回 False。"""
    try:
        with closing(get_connection(db_path)) as conn, conn:
            cur = conn.execute(
                "DELETE FROM invoices WHERE invoice_no = ?", (str(invoice_no),)
            )
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False


def query_invoices(reimburser=None, month=None, category=None, db_path=None):
    """
    组合查询发票，参数全部可选：
        reimburser —— 报销人姓名，如 "张三"
        month      —— 月份，格式 "2026-11"
        category   —— 类别，如 "餐饮"

    返回 list[dict]，按开票日期倒序排列。
    """
    conditions = []
    params = []

    if reimburser:
        conditions.append("reimburser = ?")
        params.append(reimburser)
    if month:
        conditions.append("strftime('%Y-%m', invoice_date) = ?")
        params.append(month)
    if category:
        conditions.append("category = ?")
        params.append(category)

    sql = f"SELECT {_SELECT_FIELDS} FROM invoices"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY invoice_date DESC, id DESC"

    try:
        with closing(get_connection(db_path)) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as exc:
        raise RuntimeError("数据库尚未初始化，请先运行 init_database.py。") from exc


def query_by_reimburser(name, db_path=None):
    """按报销人查询。"""
    return query_invoices(reimburser=name, db_path=db_path)


def query_by_month(month, db_path=None):
    """按月份查询，month 格式为 "2026-11"。"""
    return query_invoices(month=month, db_path=db_path)


def query_by_category(category, db_path=None):
    """按类别查询。"""
    return query_invoices(category=category, db_path=db_path)


def query_all(db_path=None):
    """查询全部发票。"""
    return query_invoices(db_path=db_path)


def add_employee(name, department, db_path=None):
    """新增员工；姓名重复返回 False。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("员工姓名不能为空。")
    try:
        with closing(get_connection(db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO employees (name, department) VALUES (?, ?)",
                (name.strip(), str(department).strip()),
            )
        return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.OperationalError as exc:
        raise RuntimeError("数据库尚未初始化，请先运行 init_database.py。") from exc


def list_employees(db_path=None):
    """列出全部员工，返回 list[dict]（id, name, department）。"""
    try:
        with closing(get_connection(db_path)) as conn:
            rows = conn.execute(
                "SELECT id, name, department FROM employees ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError as exc:
        raise RuntimeError("数据库尚未初始化，请先运行 init_database.py。") from exc
