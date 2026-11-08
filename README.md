# 发票拍照校验入库系统

桌面发票录入与管理程序，整合了 A 模块的 SQLite 数据库/校验、B 模块的百度 OCR/手动兜底，以及发票归档、查询和统计。

## 运行环境

- 本地 OCR 必须使用 Python 3.14；其他功能使用 Python 3.10 或更新版本
- Python 依赖：`requests`、`Pillow`、`python-dotenv`、RapidOCR、ONNX Runtime、`PyMuPDF`、`tkinterdnd2`
- 界面使用 Python 自带的 Tkinter；macOS/Windows 安装 Python 时应包含 Tk 支持

在仓库根目录安装依赖并启动（首次安装 RapidOCR 需要下载运行包）。Windows 使用 Python 3.14：

```powershell
py -3.14 -m venv .venv314
.\.venv314\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

macOS / Linux：

```bash
python3.14 -m venv .venv314
source .venv314/bin/activate
python -m pip install -r requirements.txt
python main.py
```

首次启动会自动创建根目录的 `invoices.db`。照片成功入库后复制到 `photos/`；这两个运行数据路径已加入 `.gitignore`。

## 文件结构

```text
.
├── main.py                    # Tkinter 桌面程序入口和主要交互界面
├── database.py                # SQLite 连接、发票/员工增删查
├── init_database.py           # 建表及旧数据库结构迁移
├── validate.py                # 发票字段、日期、类别、重复票号校验
├── storage.py                 # 文件校验/归档、业务校验和发票入库
├── query.py                   # 发票条件查询和结果金额/笔数汇总
├── ocr.py                     # 百度 OCR 客户端、结果解析和字段规范化
├── local_ocr.py               # 本地 RapidOCR 识别及发票字段提取
├── pdf_support.py             # PDF 校验、首页渲染和文本层读取
├── manual_input.py            # OCR 结果确认、修改、手动录入和取消
├── demo_B.py                  # OCR/手动录入命令行演示，支持离线模式
├── example_B.py               # OCR 和人工录入接口示例
├── requirements.txt           # 按 Python 版本选择的项目依赖
├── .env.example               # 百度 OCR 配置模板，不含真实密钥
├── .gitignore                 # 忽略虚拟环境、密钥和运行数据等
├── README_B.md                # OCR 模块的配置、接口和演示说明
├── tests/
│   ├── test_application.py    # 应用集成、数据库、PDF 和 OCR 测试
│   └── test_d_requirements.py # D 部分校验、OCR 兜底及新 OCR 输出适配测试
├── test_pictures/
│   ├── CN-*.jpg / *.png       # 发票 OCR 测试样例
│   └── screenshots/           # 使用说明和测试演示截图
├── invoices.db                # 首次运行时创建的 SQLite 数据库
└── photos/                    # 成功入库后归档的原始发票文件
```


## 系统功能与代码分工

| 功能 | 说明 | 主要文件 |
| --- | --- | --- |
| 发票识别 | 选择或拖放 JPG/JPEG/PNG/PDF；支持百度云 OCR 和本地 RapidOCR。PDF 识别/预览使用第一页，归档保留原始 PDF；可检索 PDF 会读取文本层补充字段 | `main.py`、`ocr.py`、`local_ocr.py`、`pdf_support.py` |
| 人工确认与兜底 | OCR 字段可核对、修改；识别失败或不使用 OCR 时可手动填写。修改识别到的票号、日期或金额会标记为“经编辑”，取消不会生成记录 | `manual_input.py`、`main.py` |
| 校验和防重 | 入库前校验票号非空、日期格式及非未来日期、金额为有效有限数字、类别在白名单内，并查询重复票号；数据库唯一约束提供最终防重保障。金额不限制正负或大小 | `validate.py`、`database.py`、`storage.py` |
| 文件归档与入库 | 校验通过后将选填的原始发票文件复制到 `photos/`，并将发票核心字段、文件路径和编辑标记写入 SQLite；不附文件也可手动录入 | `storage.py`、`database.py`、`init_database.py` |
| 查询和统计 | 可组合按报销人、月份及类别筛选，并统计当前查询结果的笔数和金额；可打开归档文件或删除记录 | `query.py`、`database.py`、`main.py` |
| 员工与项目说明 | 管理员工姓名和部门；关于页面展示小组成员信息 | `main.py`、`database.py` |
| OCR 接口示例 | 提供离线演示、手动录入演示及模块调用示例 | `demo_B.py`、`example_B.py`、`README_B.md` |

系统入口为 `main.py`。主要数据流程是：选取或手动填写发票 → OCR 识别（可选）→ 人工核对 → `validate.py` 校验 → `storage.py` 归档及入库 → `query.py` 查询统计。

## 界面功能

### 发票录入

1. 可将一张 JPG、JPEG、PNG 图片或 PDF 拖放到“预览与识别信息”区域，程序会自动使用本地 RapidOCR 识别；也可点击“选择发票…”后，手动选择百度 OCR 或本地 RapidOCR。拖放默认使用本地识别，不会自动上传发票。点击预览可放大发票，再点击放大图像或主界面预览即可返回界面内的发票预览；放大窗口也支持工具栏缩放或滚动查看。PDF 预览和识别使用第一页，入库时归档原始 PDF。本地识别会利用 PDF 文本层补充可检索文件的买卖方及金额字段；图片 OCR 会按版面提取发票种类、销售方、不含税合计、合计税额和商品明细。扫描件使用 OCR。两种识别都在后台运行，RapidOCR 首次使用会加载其本地模型。
2. 表单顶部、发票号码之前会显示 OCR 识别到的购买方名称和统一社会信用代码/纳税人识别号；这两项仅供核对，不自动填写报销人。核对票号、日期和价税合计后，从下拉框明确选择报销类别。
3. OCR 失败时，可尝试另一种识别方式，或直接在同一表单手动填写；也可不选照片直接手动录入。
4. 输入报销人，点击“校验并入库”。程序要求发票号、金额等必填字段非空；发票号不限制位数，金额不限制大小或正负，但必须是有效数字。日期、类别和重复票号仍按规则校验；校验通过才保存。
5. 照片会复制到 `photos/`。人工修改 OCR 票号、日期或金额时会显示并保存“经编辑”标记；数据库唯一约束也会阻止重复票号。

报销类别必须由使用者明确选择。人工录入可不附发票文件。数据库只保存 A 模块约定的核心字段、发票文件路径和编辑标记；其他 OCR 票面快照用于界面核对，不会未经确认自动保存。

### 发票查询与统计

按报销人、月份（`YYYY-MM`）和类别组合筛选。结果表显示记录和编辑状态，并统计当前结果的笔数与金额合计。可打开已归档照片或删除所选记录。

### 员工管理

添加员工姓名和部门；已登记姓名可在录入和查询筛选中选择。报销人也允许直接输入，不强制预先登记。

### 关于

小组作业标题“INT 6181_F组小组作业”居中显示，副标题“特别鸣谢全组成员”与标题右边缘对齐；成员姓名与学号也居中显示，字号随窗口空间自适应。成员信息以不可选择、与页面背景一致的表格形式展示。

## 百度 OCR 配置

百度云识别需要具有“增值税发票识别”权限的百度 API Key 和 Secret Key。请在项目根目录的 `.env` 中配置：

```dotenv
BAIDU_API_KEY=你的APIKey
BAIDU_SECRET_KEY=你的SecretKey
```

不要把真实密钥提交到公开仓库。未配置密钥或离线时，可以使用本地 RapidOCR；RapidOCR 是本地推理，不需要百度密钥。首次推理可能需要加载模型文件。两种识别结果都应人工核对。

## 离线验证

运行 A/B 联动和数据库迁移测试：

```bash
python -m unittest discover -s tests -v
```

测试使用临时 SQLite 数据库；云端 OCR 流程用模拟结果测试，不调用百度服务。本地 OCR 集成测试会读取项目样例图片/PDF。当前共 32 项 unittest，覆盖界面辅助逻辑、校验、重复票号、数据库迁移、归档、查询统计、本地 OCR 字段识别和 OCR 失败后的手动录入兜底。金额为 0 或超过 10000 的记录按当前项目规则接受，金额需为有效数字。建议在已安装依赖的 Python 3.14 环境运行完整测试。

B 模块还提供不访问网络的离线交互演示：

```bash
python3 demo_B.py --offline-demo
```

本地 RapidOCR 功能必须在 Python 3.14 环境运行，并安装 `requirements.txt` 中的 RapidOCR 3 和 ONNX Runtime 依赖；其他 Python 版本不属于本项目本地 OCR 的支持环境。百度 OCR 需要可用的网络、百度服务权限和有效配置。两种 OCR 方式提取的字段仍应由用户核对，程序不会自动把识别结果视为已审核发票。
