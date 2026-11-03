"""B 模块：人工确认、修改和 OCR 失败后的手动录入。

与 ocr.py 放在同一目录，运行依赖和百度配置参见 ocr.py 文件头说明。
纯手动录入无需百度密钥，也不调用云服务；仍须安装 ocr.py 的导入依赖。

调用 collect_invoice(ocr_result) 确认识别值，collect_invoice(None) 从空白录入。
成功返回 InputResult；将其 .record 四字段字典交给 A/C 原有接口。
source、is_edited、changed_fields、region、currency 是单独交接的元数据。
类别必须明确选择，修改票号、日期或金额后标记 is_edited=True。
输入 q/quit/取消或结束输入时返回 None。函数不写数据库、不执行 A 的业务规则。

流程：检查调用方传入的初始值 → 逐项保留或重填三个核心字段 → 明确选择类别
→ 比较规范化后的修改情况 → 返回业务记录和单独的元数据。
这里的交互和编辑标记为本项目实现，没有直接引用百度示例代码。

常用扩展字段的交接：
    scan = ocr.recognize_detailed(image_path)
    confirmed = collect_invoice(scan.record)
    # confirmed.record 是人工确认后的四字段记录。
    # scan.common_fields 是识别时的常用字段原文快照，供 C 另行展示/传递。
    # 修改 confirmed.record 不会把 scan.common_fields 也改成已确认值。
手动填写仍只处理三个核心字段和类别，避免一次要求用户补填所有可选信息。
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping

# 复用本项目的规范化规则，保证 OCR 返回值与手动填写结果采用同一格式。
# 只导入模块不会自动请求百度；手动模式无需获取 token。
from ocr import InvoiceRecord, normalize_amount, normalize_date, normalize_invoice_no

# 元组顺序对应界面的 1~4；类别是用户选择的报销分类，不由 OCR 自行推断。
CATEGORIES = ("餐饮", "交通", "办公", "其他")


@dataclass
class InputResult:
    """人工确认或填写后的结果容器。

    只把 record 交给 A 原有的四字段接口；其他属性单独交给 C 展示或传递。
    source 由是否传入 initial 决定，因此调用方应把实际 OCR 结果用于 initial。
    当前地区和币种固定为中国大陆、人民币，不包含香港或跨币种功能。
    """

    record: InvoiceRecord
    source: str                    # "ocr"：确认识别值；"manual"：从空白填写。
    is_edited: bool                 # 是否改变 OCR 的票号、日期或金额。
    changed_fields: tuple[str, ...] # 具体变动字段，顺序与字段填写顺序一致。
    region: str = "CN"
    currency: str = "CNY"


class _Cancelled(Exception):
    """内部取消信号，使任意输入位置都能直接结束本次流程。"""

    pass


def collect_invoice(initial: Mapping | None = None, *, input_fn: Callable | None = None,
                    output_fn: Callable | None = None) -> InputResult | None:
    """确认 OCR 结果或从空白填写一张大陆发票。

    参数：
        initial：来自 ocr.recognize 的四字段字典；None 表示纯手动填写。
        input_fn：可选输入函数，接收提示文本、返回字符串；默认使用 input。
        output_fn：可选输出函数，接收提示消息；默认使用 print。
        后两项是仅限关键字的参数，供测试或其他交互层注入替身。

    返回：
        完成时返回 InputResult；主动取消、输入结束或键盘中断时返回 None。
        .record 供 A/C 使用，其余属性单独交接，不混入原有四字段业务字典。

    用户输入格式不正确会重新提示；调用方传入缺字段或非法 initial 属于
    程序接口使用错误，会直接暴露异常，便于队友定位集成问题。
    """
    # 不在函数定义时固定 input/print，运行时选择，便于正常交互和测试替换。
    reader = input_fn if input_fn is not None else input
    writer = output_fn if output_fn is not None else print
    # 映射同时规定解析规则及字段顺序：票号 → 日期 → 金额。
    parsers = {"invoice_no": normalize_invoice_no, "date": normalize_date, "amount": normalize_amount}
    # 建立独立、规范化的比较基线，不修改调用方传入的字典。
    # 类别不进入基线：首次明确选择类别不计作修改 OCR 核心字段。
    # 此步骤在交互 try 之前；程序接口的结构错误不伪装成用户填写错误。
    baseline = {key: parser(initial[key]) for key, parser in parsers.items()} if initial is not None else {}

    def read(prompt: str) -> str:
        """统一去掉输入两端空白，并把取消命令转换成内部异常。"""
        value = reader(prompt).strip()
        # casefold 让 Q、Quit 等大小写变体也能取消。
        if value.casefold() in {"q", "quit", "取消"}:
            raise _Cancelled
        return value

    writer("请确认票号、开票日期和价税合计；输入 q 可取消。类别必须明确选择。")
    values = {}  # 存放本次确认后的新值，不在 initial 上直接修改。
    try:
        # 第一阶段：逐项填写或保留三个核心字段；每一项独立重试。
        for key, label in (("invoice_no", "发票号码"), ("date", "开票日期 YYYY-MM-DD"), ("amount", "价税合计 CNY")):
            default = baseline.get(key)
            # 用 is not None 判断有无默认值，避免把数值 0.0 错当作“没有值”。
            suffix = f" [{default}]（回车保留）" if default is not None else ""
            while True:
                raw = read(f"{label}{suffix}：")
                if not raw and default is not None:
                    # 只有存在 OCR 默认值时，空输入才表示保留。
                    values[key] = default
                    break
                try:
                    # 无默认值时空输入也会进入解析，因此必填字段不会被悄悄跳过。
                    values[key] = parsers[key](raw)
                    break
                except ValueError as exc:
                    # 当前字段格式有误就重新填写该字段，保留前面已确认的值。
                    writer(f"输入有误：{exc}")
        # 第二阶段：类别必须明确选择，即使 OCR 的 category 已经是“其他”。
        while True:
            raw = read("请选择类别 1餐饮 / 2交通 / 3办公 / 4其他：")
            if raw in {"1", "2", "3", "4"}:
                # 界面从 1 编号，元组从 0 索引，因此减 1。
                category = CATEGORIES[int(raw) - 1]
                break
            if raw in CATEGORIES:
                # 同时接受直接输入类别中文名，便于键盘操作。
                category = raw
                break
            writer("请选择 1 至 4 或输入对应类别名称。")
    except (EOFError, KeyboardInterrupt, _Cancelled):
        # 第三阶段的前置分支：输入结束、Ctrl+C 和主动取消均不产生半条记录。
        writer("已取消本次录入。")
        return None
    # 第三阶段：按规范化结果比较，日期仅从斜杠改为横线等排版变化不算编辑。
    # 纯手动模式没有 OCR 基线，因此 source=manual，且 changes 为空。
    changes = tuple(key for key in parsers if initial is not None and values[key] != baseline[key])
    # 新建四字段记录，保持与 ocr.recognize 的数据契约一致。
    record = InvoiceRecord(**values, category=category)
    if changes:
        writer("该发票经过编辑；修改字段：" + "、".join(changes))
    # 本模块只生成记录和元数据；校验通过后是否保存由 A/C 接续处理。
    return InputResult(record=record, source="ocr" if initial is not None else "manual",
                       is_edited=bool(changes), changed_fields=changes)
