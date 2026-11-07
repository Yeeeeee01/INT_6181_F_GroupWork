"""B 模块接口调用参考：与 ocr.py、manual_input.py 放在同一目录。

运行方式（先按 README_B 安装依赖，真实识别还需要百度配置）：
    python example_B.py                         # 默认：真实识别并人工确认
    python example_B.py simple                  # 只识别，查看原四字段接口
    python example_B.py manual                  # 纯手动填写，不请求百度
    python example_B.py --image test_pictures/其他图片.png

三个 example_* 函数只是整理调用步骤，队友可以参考后直接使用两个核心模块。
导入本文件不执行识别；直接运行也只执行所选示例，不连续调用两个识别入口。
本文件展示 Python 返回对象；队友不需要解析打印文字，也不需要新增业务接口。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import ocr  # 提供 recognize、recognize_detailed 和相关返回类型。
from manual_input import InputResult, collect_invoice  # 提供人工确认和手动填写。


def example_simple(image_path: str | Path) -> ocr.InvoiceRecord | None:
    """示例一：只取得四字段字典，不等待输入；预期识别失败返回 None。"""
    record = ocr.recognize(image_path)
    if record is None:
        print("未取得核心字段；需要填写时可接着调用 collect_invoice(None)。")
        return None
    # 票号为 str，日期为 YYYY-MM-DD，含税金额为 float；类别“其他”是待确认占位。
    # 这份记录尚未经过人工确认，正式录入应继续执行示例二中的确认步骤。
    print("待确认的四字段记录：", record)
    return record


def example_complete(image_path: str | Path) -> InputResult | None:
    """示例二：识别一次 → 确认/修改，失败时手动填写；取消返回 None。"""
    # 1. 详细入口一次返回核心记录、可选字段和诊断信息，不再调用 recognize。
    scan = ocr.recognize_detailed(image_path)
    if not scan.ok:
        # ok 只说明核心字段是否解析成功；识别成功也仍需对照票面确认。
        print(f"识别未完成 [{scan.error_code}]：{scan.message}；转为手动填写。")
    for warning in scan.field_warnings:
        print("可选字段解析提示：", warning)  # 警告不等于核心记录失败或识别准确率。

    # 2. 有记录时逐项确认/修改；record=None 时，同一接口从空白填写。
    # 类别必须明确选择；q/quit/取消、输入结束或 Ctrl+C 均可能返回 None。
    confirmed = collect_invoice(scan.record)
    if confirmed is None:
        print("本次取消，不产生待保存记录。")
        return None

    # 3. 后续校验/保存流程接收这份四字段字典；来源和编辑信息单独读取。
    record = confirmed.record
    print("人工确认后的四字段记录：", record)
    print("来源：", confirmed.source)  # ocr：接收了识别值；manual：从空白填写。
    print("是否编辑：", confirmed.is_edited)
    print("修改字段：", confirmed.changed_fields)  # 如 ("amount",)；首次选类别不算编辑。
    # 在这里把 record 传给小组已有的业务校验接口；通过后再由保存模块处理。

    # 4. 以下为常用“可选”字段示范。快照可能为 None，单项缺失也允许为 None。
    # 修改 record.amount 不会改写该快照；需要传递时应和确认记录分别交接。
    common = scan.common_fields
    if common is not None:
        print("可选字段（未经逐项人工确认的 OCR 快照）：")
        for key in ("invoice_type", "purchaser_name", "seller_name", "amount_without_tax", "total_tax"):
            print(f"  {ocr.COMMON_FIELD_LABELS[key]}：{common.get(key)}")
        print("  原识别价税合计文本：", common.get("amount_in_figures"))
        # 明细已按 row 行号对齐；金额、税率保留文本，缺列为 None，无明细时为 []。
        for item in common.get("items", []):
            print("  明细：", item["row"], item["name"], item["amount"], item["tax_rate"])

    # 返回核心模块原有的 InputResult 对象；调用方先判断 None，再读取 .record 等属性。
    # 如需扩展快照，请参考上方 scan.common_fields 的取法，单独保留或传递。
    return confirmed


def example_manual() -> InputResult | None:
    """示例三：直接填写，无需先调用 OCR，也不需要百度凭据。"""
    confirmed = collect_invoice()  # 不传初始值，等价于 collect_invoice(None)。
    if confirmed is None:
        print("本次取消，不产生待保存记录。")
        return None
    print("手动填写后的四字段记录：", confirmed.record)
    print("来源：", confirmed.source)  # manual；没有 OCR 基线，is_edited=False。
    return confirmed


def main(argv: list[str] | None = None) -> int:
    """仅用于选择示例；业务程序集成时直接参考上方的接口调用语句。"""
    parser = argparse.ArgumentParser(description="B 模块三个公共接口调用示例")
    parser.add_argument("mode", nargs="?", choices=("complete", "simple", "manual"), default="complete")
    parser.add_argument("--image", type=Path, help="图片路径；相对路径按当前工作目录解析")
    args = parser.parse_args(argv)
    if args.mode == "manual":
        if args.image is not None:
            parser.error("manual 模式不接收 --image；它直接调用 collect_invoice()")
        return 0 if example_manual() is not None else 1
    # 默认样例按本文件定位，所以从其他目录启动也能找到随包提供的图片。
    image_path = args.image if args.image is not None else Path(__file__).resolve().parent / "test_pictures/CN-05_aliyun_vat_example_1.jpg"
    if args.mode == "simple":
        return 0 if example_simple(image_path) is not None else 2
    return 0 if example_complete(image_path) is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())  # 0：完成；1：取消；2：仅识别失败或参数错误。
