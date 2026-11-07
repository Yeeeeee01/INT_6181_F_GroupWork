"""B 模块调用示例：与 ocr.py、manual_input.py 放在同一目录。

真实识别并人工确认：python demo_B.py --image test_pictures/CN-05_aliyun_vat_example_1.jpg
只查看真实识别结果：在上面命令末尾加 --scan-only。
纯手动填写：python demo_B.py --manual
无需密钥的接口演示：python demo_B.py --offline-demo

本文件只调用 B 的公共接口并展示返回值，不写数据库、不实现 A/C 的业务。
队友集成时直接导入两个核心模块即可，不需要依赖或解析本脚本的控制台输出。
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

import ocr
from manual_input import collect_invoice


def _show_optional(scan: ocr.RecognitionResult) -> None:
    """示范 C 如何读取可选字段；这里只展示，不将它们标记为已确认。"""
    common = scan.common_fields
    if common is not None:
        print("\n可选字段：队友按需展示/保存，以下仍是未经人工确认的 OCR 快照。")
        for key in ("invoice_type", "purchaser_name", "seller_name", "amount_without_tax", "total_tax", "purchaser_tax_no"):
            value = common[key]
            print(f"  {ocr.COMMON_FIELD_LABELS[key]}：{value if value is not None else '未识别/票面未填写'}")
        for item in common["items"]:
            print(f"  明细行 {item['row']}：名称={item['name']}，金额={item['amount']}，税率={item['tax_rate']}")
    for warning in scan.field_warnings:
        print("可选字段解析提示：" + warning)


def _print_result(payload: dict) -> None:
    """便于演示阅读；实际程序集成应直接使用 API 返回的对象。"""
    print("\n【接口输出 JSON】")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="B 模块：识别、可选字段与人工兜底调用示例")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image", type=Path, help="调用真实百度 OCR；图片路径相对于当前终端目录")
    mode.add_argument("--manual", action="store_true", help="仅手动填写，不需要百度密钥")
    mode.add_argument("--offline-demo", action="store_true", help="使用内置模拟响应演示接口，不识别图片、不联网")
    parser.add_argument("--scan-only", action="store_true", help="仅输出待确认结果，不进入人工输入")
    args = parser.parse_args(argv)
    if args.manual and args.scan_only:
        parser.error("--manual 不能与 --scan-only 同时使用")

    scan = None
    record = None
    mode_name = "manual"
    if args.image:
        mode_name = "cloud"
        print("正在调用真实百度增值税发票识别，请稍候……")
        # 一次识别同时返回核心字段、可选扩展和状态，不再另外调用 recognize 重复收费。
        scan = ocr.recognize_detailed(args.image)
        record = scan.record
        if scan.ok:
            print(f"识别成功：{scan.elapsed_ms} ms，识别请求 {scan.attempts} 次；仍需核对票面。")
        else:
            print(f"识别未完成 [{scan.error_code}]：{scan.message}")
        _show_optional(scan)
    elif args.offline_demo:
        mode_name = "offline_demo"
        print("【离线模拟】使用内置测试响应，不识别图片、不调用百度，不能作为 OCR 准确率证据。")
        # 仅演示公共字段转换接口；真实图片始终使用上面的 recognize_detailed。
        record = ocr.normalize_response({"words_result": {
            "InvoiceNum": "00123456", "InvoiceDate": "2024年02月29日", "AmountInFiguers": "81.00",
        }})
    else:
        print("纯手动模式：不调用百度，直接填写四字段业务记录。")

    # 扩展快照单独放置；人工改 record 不会同步改写它。
    payload = {
        "mode": mode_name,
        "ocr_success": scan.ok if scan is not None else None,
        "common_fields": scan.common_fields if scan is not None else None,
        "field_warnings": list(scan.field_warnings) if scan is not None else [],
        "common_fields_reviewed": False,
    }
    if args.scan_only:
        payload.update(record=record, review_required=True)
        if scan is not None:
            payload.update(error_code=scan.error_code, message=scan.message,
                           elapsed_ms=scan.elapsed_ms, attempts=scan.attempts)
        _print_result(payload)
        return 0 if record is not None else 2

    if args.image and record is None:
        print("将进入手动填写兜底；可输入 q 取消。")
    # 成功识别时确认/修改；没有识别结果时同一接口自动从空白录入。
    confirmed = collect_invoice(record)
    if confirmed is None:
        print("本次取消，没有待交接记录。")
        return 1
    payload.update(asdict(confirmed))
    _print_result(payload)
    print("\n仅将 confirmed.record 交给 A 的实际校验接口；通过后由 C 调用项目的保存逻辑。")
    if args.offline_demo:
        print("离线示例的 source=ocr 仅表示传入了初始值；mode=offline_demo，未发生真实 OCR。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
