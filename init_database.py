"""
init_database.py —— 建表脚本（小组作业 A 部分）

运行一次即可：
    python init_database.py           建表（表已存在则跳过，不会丢数据）
    python init_database.py --reset   清空并重建（演示前重置数据用，会二次确认）

建出的表：
    employees  员工表：姓名、部门
    invoices   发票表：票号、日期、金额、类别、报销人、照片路径、录入时间
               票号上的 UNIQUE 约束会在数据库层面挡住重复录入。
"""

import argparse
from contextlib import closing

from database import DB_PATH, get_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    department  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS invoices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no    TEXT NOT NULL UNIQUE,   -- 8 位发票号，UNIQUE 防止重复入库
    invoice_date  TEXT NOT NULL,          -- 开票日期 YYYY-MM-DD
    amount        REAL NOT NULL,          -- 金额（元）
    category      TEXT NOT NULL,          -- 餐饮/交通/办公/其他
    reimburser    TEXT NOT NULL,          -- 报销人
    photo_path    TEXT NOT NULL,          -- 发票照片路径
    created_at    TEXT NOT NULL           -- 录入时间 YYYY-MM-DD HH:MM:SS
);

CREATE INDEX IF NOT EXISTS idx_invoices_date       ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoices_category   ON invoices(category);
CREATE INDEX IF NOT EXISTS idx_invoices_reimburser ON invoices(reimburser);
"""

DROP = """
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS employees;
"""


def init_database(db_path=None, reset=False):
    """创建数据库和表。reset=True 时先删除旧表（数据会丢失）。"""
    with closing(get_connection(db_path)) as conn:
        with conn:
            if reset:
                conn.executescript(DROP)
            conn.executescript(SCHEMA)


def main():
    parser = argparse.ArgumentParser(description="初始化发票校验系统的 SQLite 数据库")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="删除已有数据并重建表（演示前重置数据用）",
    )
    args = parser.parse_args()

    if args.reset:
        answer = input("警告：--reset 会清空所有已录入的发票和员工，输入 yes 继续：")
        if answer.strip().lower() != "yes":
            print("已取消，数据库未做任何改动。")
            return

    init_database(reset=args.reset)

    with closing(get_connection()) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]

    print(f"数据库已就绪：{DB_PATH}")
    print("当前表：" + "、".join(tables))


if __name__ == "__main__":
    main()
