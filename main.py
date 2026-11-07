"""Desktop UI entry point for the invoice validation and filing system."""

from __future__ import annotations

import logging
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None

import database
import init_database
import local_ocr
import ocr
import pdf_support
import query
import storage
from ocr import normalize_amount, normalize_date, normalize_invoice_no

LOGGER = logging.getLogger(__name__)
CATEGORIES = ("餐饮", "交通", "办公", "其他")
MAX_VIEWER_IMAGE_DIMENSION = 8192
ABOUT_TITLE = "INT 6181_F组小组作业"
ABOUT_SUBTITLE = "特别鸣谢全组成员"
ABOUT_MEMBERS = (
    ("CHEN JIMAI", "11750193"),
    ("WU YIHENG", "未提供"),
    ("YANG BO", "11769182"),
    ("YE ZHAOLAN", "11796214"),
    ("ZHANG YANG", "11749625"),
)


def _about_font_sizes(width: int, height: int) -> tuple[int, int, int]:
    scale = max(0.85, min(width / 840, height / 560, 1.5))
    return (
        round(30 * scale),
        round(19 * scale),
        round(16 * scale),
    )


def _split_drop_paths(
    data: str, splitlist: Callable[[str], tuple[str, ...]]
) -> tuple[Path, ...]:
    return tuple(Path(value) for value in splitlist(data))


def _scaled_image_size(
    width: int, height: int, scale: float
) -> tuple[int, int]:
    bounded_scale = min(
        scale,
        MAX_VIEWER_IMAGE_DIMENSION / width,
        MAX_VIEWER_IMAGE_DIMENSION / height,
    )
    return max(1, round(width * bounded_scale)), max(1, round(height * bounded_scale))


class InvoiceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("发票拍照校验入库系统")
        self.root.geometry("1120x760")
        self.root.minsize(940, 650)

        self.image_path: Path | None = None
        self.ocr_baseline: dict | None = None
        self.preview_image = None
        self._viewer_window: tk.Toplevel | None = None
        self._viewer_canvas: tk.Canvas | None = None
        self._viewer_source = None
        self._viewer_photo = None
        self._viewer_image_id: int | None = None
        self._viewer_path: Path | None = None
        self._viewer_zoom = 1.0
        self._viewer_fit_mode = True
        self._scan_running = False
        self._scan_request_id = 0

        self._configure_style()
        self._build_header()
        self._build_tabs()
        self.refresh_employees()
        self.refresh_invoices()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("", 20, "bold"))
        style.configure("Hint.TLabel", foreground="#536273")
        style.configure("Treeview", rowheight=28)
        style.configure("Treeview.Heading", font=("", 10, "bold"))

    def _build_header(self) -> None:
        header = ttk.Frame(self.root, padding=(22, 18, 22, 8))
        header.pack(fill="x")
        ttk.Label(header, text="发票拍照校验入库系统", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            header,
            text="图片/PDF 识别 / 手动录入 · 规则校验与查重 · 入库归档 · 查询统计",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(5, 0))

    def _build_tabs(self) -> None:
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=22, pady=(8, 20))
        self.entry_tab = ttk.Frame(self.tabs, padding=18)
        self.query_tab = ttk.Frame(self.tabs, padding=18)
        self.employee_tab = ttk.Frame(self.tabs, padding=18)
        self.about_tab = ttk.Frame(self.tabs, padding=18)
        self.tabs.add(self.entry_tab, text="发票录入")
        self.tabs.add(self.query_tab, text="发票查询与统计")
        self.tabs.add(self.employee_tab, text="员工管理")
        self.tabs.add(self.about_tab, text="关于")
        self._build_entry_tab()
        self._build_query_tab()
        self._build_employee_tab()
        self._build_about_tab()

    def _build_entry_tab(self) -> None:
        self.entry_tab.columnconfigure(0, weight=3)
        self.entry_tab.columnconfigure(1, weight=2)
        self.entry_tab.rowconfigure(1, weight=1)

        photo_box = ttk.LabelFrame(self.entry_tab, text="发票文件", padding=12)
        photo_box.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        photo_box.columnconfigure(0, weight=1)
        self.path_var = tk.StringVar()
        ttk.Entry(photo_box, textvariable=self.path_var, state="readonly").grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        self.choose_button = ttk.Button(
            photo_box, text="选择发票…", command=self.choose_image
        )
        self.choose_button.grid(
            row=0, column=1, padx=(0, 8)
        )
        self.scan_button = ttk.Button(
            photo_box, text="百度 OCR 识别", command=self.start_ocr
        )
        self.scan_button.grid(row=0, column=2, padx=(0, 8))
        self.local_scan_button = ttk.Button(
            photo_box,
            text="本地 RapidOCR 识别",
            command=self.start_local_ocr,
        )
        self.local_scan_button.grid(row=0, column=3)

        form = ttk.LabelFrame(self.entry_tab, text="发票信息（可编辑或手动填写）", padding=16)
        form.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        form.columnconfigure(1, weight=1)
        self.recognized_field_vars = {
            key: tk.StringVar()
            for key in ("purchaser_name", "purchaser_tax_no")
        }
        recognized_fields = (
            ("purchaser_name", "购买方名称"),
            ("purchaser_tax_no", "统一社会信用代码/纳税人识别号"),
        )
        for row, (key, label) in enumerate(recognized_fields):
            ttk.Label(form, text=label).grid(
                row=row, column=0, sticky="w", pady=9
            )
            ttk.Entry(
                form,
                textvariable=self.recognized_field_vars[key],
                state="readonly",
            ).grid(row=row, column=1, sticky="ew", pady=9)

        self.field_vars = {
            key: tk.StringVar()
            for key in ("invoice_no", "date", "amount", "category", "reimburser")
        }
        fields = (
            ("invoice_no", "发票号码"),
            ("date", "开票日期 YYYY-MM-DD"),
            ("amount", "价税合计（元）"),
            ("category", "报销类别"),
            ("reimburser", "报销人"),
        )
        row_offset = len(recognized_fields)
        for row, (key, label) in enumerate(fields, start=row_offset):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=9)
            if key == "category":
                control = ttk.Combobox(
                    form,
                    textvariable=self.field_vars[key],
                    values=CATEGORIES,
                    state="readonly",
                )
            elif key == "reimburser":
                control = ttk.Combobox(
                    form, textvariable=self.field_vars[key], state="normal"
                )
                self.reimburser_box = control
            else:
                control = ttk.Entry(form, textvariable=self.field_vars[key])
            control.grid(row=row, column=1, sticky="ew", pady=9)
            self.field_vars[key].trace_add("write", self._update_edit_marker)

        self.edit_marker_var = tk.StringVar(value="尚未进行 OCR 识别")
        ttk.Label(form, textvariable=self.edit_marker_var, style="Hint.TLabel").grid(
            row=row_offset + len(fields), column=0, columnspan=2, sticky="w", pady=(10, 4)
        )
        self.entry_status_var = tk.StringVar(
            value="选择发票图片或 PDF 后可识别；也可不选文件，直接手动填写。"
        )
        ttk.Label(
            form,
            textvariable=self.entry_status_var,
            wraplength=480,
            justify="left",
        ).grid(row=row_offset + len(fields) + 1, column=0, columnspan=2, sticky="w", pady=8)
        actions = ttk.Frame(form)
        actions.grid(row=row_offset + len(fields) + 2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.clear_button = ttk.Button(
            actions, text="清空表单", command=self.clear_form
        )
        self.clear_button.pack(
            side="right", padx=(8, 0)
        )
        self.save_button = ttk.Button(
            actions, text="校验并入库", command=self.save_current_invoice
        )
        self.save_button.pack(side="right")

        preview = ttk.LabelFrame(self.entry_tab, text="预览与识别信息", padding=12)
        preview.grid(row=1, column=1, sticky="nsew")
        self.preview_label = ttk.Label(
            preview, text="尚未选择发票", anchor="center", justify="center"
        )
        self.preview_label.bind("<Button-1>", self._open_image_viewer)
        self.preview_label.pack(fill="both", expand=True, pady=(4, 12))
        self.drop_hint_var = tk.StringVar(
            value="拖放发票自动识别；点击预览可放大查看。"
        )
        self.drop_hint_label = ttk.Label(
            preview,
            textvariable=self.drop_hint_var,
            style="Hint.TLabel",
            anchor="center",
        )
        self.drop_hint_label.pack(fill="x", pady=(0, 8))
        self.common_fields_var = tk.StringVar(value="可选票面信息将在 OCR 后显示。")
        self.common_fields_label = ttk.Label(
            preview,
            textvariable=self.common_fields_var,
            wraplength=310,
            justify="left",
            style="Hint.TLabel",
        )
        self.common_fields_label.pack(fill="x", anchor="w")
        self._enable_invoice_drop(preview)

    def _enable_invoice_drop(self, preview: ttk.LabelFrame) -> None:
        if DND_FILES is None:
            self.drop_hint_var.set(
                "拖放组件不可用；请点击“选择发票…”，或安装 requirements.txt 后重启。"
            )
            return
        try:
            for widget in (
                preview,
                self.preview_label,
                self.drop_hint_label,
                self.common_fields_label,
            ):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_invoice_drop)
        except tk.TclError:
            LOGGER.exception("Unable to register invoice drag-and-drop targets")
            self.drop_hint_var.set(
                "拖放功能无法启动；请点击“选择发票…”载入文件。"
            )

    def _build_query_tab(self) -> None:
        filters = ttk.LabelFrame(self.query_tab, text="筛选条件", padding=12)
        filters.pack(fill="x", pady=(0, 12))
        self.filter_person_var = tk.StringVar()
        self.filter_month_var = tk.StringVar()
        self.filter_category_var = tk.StringVar()
        ttk.Label(filters, text="报销人").grid(row=0, column=0, padx=(0, 6))
        self.filter_person_box = ttk.Combobox(
            filters, textvariable=self.filter_person_var, width=16
        )
        self.filter_person_box.grid(row=0, column=1, padx=(0, 14))
        ttk.Label(filters, text="月份").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(filters, textvariable=self.filter_month_var, width=12).grid(
            row=0, column=3, padx=(0, 14)
        )
        ttk.Label(filters, text="类别").grid(row=0, column=4, padx=(0, 6))
        ttk.Combobox(
            filters,
            textvariable=self.filter_category_var,
            values=("", *CATEGORIES),
            state="readonly",
            width=10,
        ).grid(row=0, column=5, padx=(0, 14))
        ttk.Button(filters, text="查询", command=self.refresh_invoices).grid(
            row=0, column=6, padx=(0, 8)
        )
        ttk.Button(filters, text="重置", command=self.reset_filters).grid(
            row=0, column=7
        )

        table_frame = ttk.Frame(self.query_tab)
        table_frame.pack(fill="both", expand=True)
        columns = (
            "date",
            "invoice_no",
            "reimburser",
            "amount",
            "category",
            "is_edited",
            "photo_path",
        )
        self.invoice_table = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse"
        )
        headings = {
            "date": ("日期", 100),
            "invoice_no": ("发票号码", 130),
            "reimburser": ("报销人", 100),
            "amount": ("金额（元）", 100),
            "category": ("类别", 90),
            "is_edited": ("经编辑", 80),
            "photo_path": ("发票文件归档路径", 280),
        }
        for column, (title, width) in headings.items():
            self.invoice_table.heading(column, text=title)
            self.invoice_table.column(column, width=width, minwidth=70, anchor="center")
        scrollbar = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.invoice_table.yview
        )
        self.invoice_table.configure(yscrollcommand=scrollbar.set)
        self.invoice_table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.invoice_table.bind("<Double-1>", self.open_selected_photo)

        footer = ttk.Frame(self.query_tab, padding=(0, 12, 0, 0))
        footer.pack(fill="x")
        self.summary_var = tk.StringVar(value="共 0 笔，合计 ¥0.00")
        ttk.Label(footer, textvariable=self.summary_var).pack(side="left")
        ttk.Button(
            footer, text="打开所选发票文件", command=self.open_selected_photo
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            footer, text="删除所选记录", command=self.delete_selected_invoice
        ).pack(side="right")

    def _build_employee_tab(self) -> None:
        form = ttk.LabelFrame(self.employee_tab, text="新增员工", padding=14)
        form.pack(fill="x", pady=(0, 14))
        self.employee_name_var = tk.StringVar()
        self.department_var = tk.StringVar()
        ttk.Label(form, text="姓名").grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(form, textvariable=self.employee_name_var, width=20).grid(
            row=0, column=1, padx=(0, 14)
        )
        ttk.Label(form, text="部门").grid(row=0, column=2, padx=(0, 6))
        ttk.Entry(form, textvariable=self.department_var, width=20).grid(
            row=0, column=3, padx=(0, 14)
        )
        ttk.Button(form, text="添加员工", command=self.add_employee).grid(
            row=0, column=4
        )

        self.employee_table = ttk.Treeview(
            self.employee_tab,
            columns=("name", "department"),
            show="headings",
            height=16,
        )
        self.employee_table.heading("name", text="姓名")
        self.employee_table.heading("department", text="部门")
        self.employee_table.column("name", width=220, anchor="center")
        self.employee_table.column("department", width=300, anchor="center")
        self.employee_table.pack(fill="both", expand=True)

    def _build_about_tab(self) -> None:
        content = ttk.Frame(self.about_tab)
        content.pack(anchor="n", pady=(20, 0))
        self.about_heading_frame = ttk.Frame(content)
        self.about_heading_frame.pack()
        self.about_heading_frame.pack_propagate(False)
        ttk.Label(
            self.about_heading_frame,
            text=ABOUT_TITLE,
            style="AboutTitle.TLabel",
            anchor="center",
        ).pack(fill="x")
        ttk.Label(
            self.about_heading_frame,
            text=ABOUT_SUBTITLE,
            style="AboutSubtitle.TLabel",
            anchor="e",
        ).pack(fill="x", pady=(8, 0))
        members_table = ttk.Frame(content)
        members_table.columnconfigure(0, weight=1)
        members_table.columnconfigure(1, weight=1)
        ttk.Label(
            members_table,
            text="姓名",
            style="AboutHeading.TLabel",
            anchor="center",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 60), pady=(0, 8))
        ttk.Label(
            members_table,
            text="学号",
            style="AboutHeading.TLabel",
            anchor="center",
        ).grid(row=0, column=1, sticky="ew", pady=(0, 8))
        for row, (name, student_id) in enumerate(ABOUT_MEMBERS, start=1):
            ttk.Label(
                members_table, text=name, style="AboutMember.TLabel", anchor="center"
            ).grid(
                row=row, column=0, sticky="ew", padx=(0, 60), pady=3
            )
            ttk.Label(
                members_table,
                text=student_id,
                style="AboutMember.TLabel",
                anchor="center",
            ).grid(
                row=row, column=1, sticky="ew", pady=3
            )
        members_table.pack(pady=(20, 0))
        self.about_tab.bind("<Configure>", self._update_about_fonts, add="+")
        self._update_about_fonts()

    def _update_about_fonts(self, event: tk.Event | None = None) -> None:
        width = event.width if event is not None else self.about_tab.winfo_width()
        height = event.height if event is not None else self.about_tab.winfo_height()
        title_size, subtitle_size, member_size = _about_font_sizes(width, height)
        style = ttk.Style(self.root)
        style.configure("AboutTitle.TLabel", font=("", title_size, "bold"))
        style.configure(
            "AboutSubtitle.TLabel",
            font=("", subtitle_size),
            foreground="#536273",
        )
        style.configure(
            "AboutHeading.TLabel",
            font=("", member_size, "bold"),
        )
        style.configure("AboutMember.TLabel", font=("", member_size))
        title_font = tkfont.Font(
            root=self.root,
            font=style.lookup("AboutTitle.TLabel", "font"),
        )
        subtitle_font = tkfont.Font(
            root=self.root,
            font=style.lookup("AboutSubtitle.TLabel", "font"),
        )
        self.about_heading_frame.configure(
            width=title_font.measure(ABOUT_TITLE),
            height=(
                title_font.metrics("linespace")
                + subtitle_font.metrics("linespace")
                + 12
            ),
        )

    def choose_image(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择发票",
            filetypes=[
                ("发票文件", "*.jpg *.jpeg *.png *.pdf"),
                ("PDF 文件", "*.pdf"),
                ("发票图片", "*.jpg *.jpeg *.png"),
                ("JPEG 图片", "*.jpg *.jpeg"),
                ("PNG 图片", "*.png"),
            ],
        )
        if not selected:
            return
        self._load_invoice(selected)

    def _load_invoice(self, selected: str | Path) -> bool:
        try:
            image_path = storage.validate_image_path(selected)
        except (ValueError, OSError, RuntimeError) as exc:
            messagebox.showerror("发票文件无法使用", str(exc), parent=self.root)
            return False
        self._close_image_viewer()
        self.image_path = image_path
        self.path_var.set(str(self.image_path))
        self._show_preview(self.image_path)
        self.entry_status_var.set(
            "发票已载入。PDF 将识别第一页；拖放时自动使用本地 RapidOCR。"
            if self.image_path.suffix.lower() == ".pdf"
            else "发票图片已载入；拖放时自动使用本地 RapidOCR。"
        )
        self.ocr_baseline = None
        for key in ("invoice_no", "date", "amount", "category"):
            self.field_vars[key].set("")
        self._clear_recognized_fields()
        self.common_fields_var.set("可选票面信息将在 OCR 后显示。")
        self.edit_marker_var.set("尚未进行 OCR 识别")
        self._update_edit_marker()
        return True

    def _on_invoice_drop(self, event) -> str:
        if self._scan_running:
            self.entry_status_var.set("当前发票正在识别，请完成后再拖放下一张。")
            return "break"
        try:
            paths = _split_drop_paths(event.data, self.root.tk.splitlist)
        except (tk.TclError, TypeError, ValueError):
            self.entry_status_var.set("无法读取拖入的文件，请重新拖放或点击“选择发票…”。")
            return "break"
        if len(paths) != 1:
            self.entry_status_var.set("请一次拖放一张发票。")
            return "break"
        if self._load_invoice(paths[0]):
            self._start_recognition("local")
        return "break"

    def _show_preview(self, image_path: Path) -> None:
        try:
            from PIL import ImageTk

            with self._open_invoice_image(image_path) as image:
                image.thumbnail((360, 250))
                self.preview_image = ImageTk.PhotoImage(image.copy())
            self.preview_label.configure(image=self.preview_image, text="")
            self.preview_label.configure(cursor="hand2")
        except (OSError, RuntimeError, ValueError):
            self.preview_image = None
            self.preview_label.configure(image="", text="文件无法预览，但仍可尝试识别。")
            self.preview_label.configure(cursor="")

    @staticmethod
    def _open_invoice_image(image_path: Path):
        from PIL import Image

        if image_path.suffix.lower() == ".pdf":
            return Image.open(BytesIO(pdf_support.render_first_page(image_path)))
        return Image.open(image_path)

    def _open_image_viewer(self, event=None) -> str:
        if event is not None and event.widget is not self.preview_label:
            return "break"
        if self.image_path is None:
            return "break"
        if (
            self._viewer_window is not None
            and self._viewer_window.winfo_exists()
        ):
            if self._viewer_path == self.image_path:
                self._close_image_viewer()
                return "break"
            self._close_image_viewer()

        try:
            with self._open_invoice_image(self.image_path) as image:
                self._viewer_source = image.copy()
        except (OSError, RuntimeError, ValueError) as exc:
            messagebox.showerror("无法放大发票", str(exc), parent=self.root)
            return "break"

        self._viewer_path = self.image_path
        window = tk.Toplevel(self.root)
        self._viewer_window = window
        window.title(f"发票预览 - {self.image_path.name}")
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        window.geometry(f"{int(screen_width * 0.9)}x{int(screen_height * 0.88)}")
        window.minsize(480, 360)
        window.rowconfigure(1, weight=1)
        window.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(window, padding=(10, 8))
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            toolbar, text="−", width=4,
            command=lambda: self._set_viewer_zoom(self._viewer_zoom / 1.25),
        ).pack(side="left")
        ttk.Button(
            toolbar, text="+", width=4,
            command=lambda: self._set_viewer_zoom(self._viewer_zoom * 1.25),
        ).pack(side="left", padx=(5, 0))
        ttk.Button(
            toolbar, text="适合窗口", command=self._fit_image_viewer
        ).pack(side="left", padx=(10, 0))
        ttk.Button(
            toolbar, text="实际大小", command=lambda: self._set_viewer_zoom(1.0)
        ).pack(side="left", padx=(5, 0))
        self._viewer_zoom_var = tk.StringVar()
        ttk.Label(toolbar, textvariable=self._viewer_zoom_var).pack(
            side="left", padx=(10, 0)
        )

        canvas_frame = ttk.Frame(window)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        canvas = tk.Canvas(canvas_frame, background="#333333", highlightthickness=0)
        self._viewer_canvas = canvas
        vertical_scrollbar = ttk.Scrollbar(
            canvas_frame, orient="vertical", command=canvas.yview
        )
        horizontal_scrollbar = ttk.Scrollbar(
            canvas_frame, orient="horizontal", command=canvas.xview
        )
        canvas.configure(
            yscrollcommand=vertical_scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )
        canvas.bind("<Button-1>", self._close_image_viewer)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        canvas.bind("<Configure>", self._redraw_image_viewer)
        window.protocol("WM_DELETE_WINDOW", self._close_image_viewer)
        self._viewer_zoom = 1.0
        self._viewer_fit_mode = True
        window.after_idle(self._redraw_image_viewer)
        return "break"

    def _redraw_image_viewer(self, event=None) -> None:
        if (
            self._viewer_canvas is None
            or self._viewer_source is None
            or self._viewer_window is None
            or not self._viewer_window.winfo_exists()
        ):
            return
        from PIL import Image, ImageTk

        if self._viewer_fit_mode:
            canvas_width = (
                event.width if event is not None
                else self._viewer_canvas.winfo_width()
            )
            canvas_height = (
                event.height if event is not None
                else self._viewer_canvas.winfo_height()
            )
            available_width = max(1, canvas_width - 8)
            available_height = max(1, canvas_height - 8)
            self._viewer_zoom = min(
                1.0,
                available_width / self._viewer_source.width,
                available_height / self._viewer_source.height,
            )
        size = _scaled_image_size(
            self._viewer_source.width,
            self._viewer_source.height,
            self._viewer_zoom,
        )
        display_image = self._viewer_source.resize(
            size, Image.Resampling.LANCZOS
        )
        self._viewer_photo = ImageTk.PhotoImage(
            display_image, master=self._viewer_window
        )
        if self._viewer_image_id is None:
            self._viewer_image_id = self._viewer_canvas.create_image(
                0, 0, anchor="nw", image=self._viewer_photo
            )
        else:
            self._viewer_canvas.itemconfigure(
                self._viewer_image_id, image=self._viewer_photo
            )
        self._viewer_canvas.configure(
            scrollregion=(0, 0, size[0], size[1])
        )
        self._viewer_zoom_var.set(f"{self._viewer_zoom:.0%}")

    def _set_viewer_zoom(self, scale: float) -> None:
        self._viewer_fit_mode = False
        self._viewer_zoom = min(4.0, max(0.1, scale))
        self._redraw_image_viewer()

    def _fit_image_viewer(self) -> None:
        self._viewer_fit_mode = True
        self._redraw_image_viewer()

    def _close_image_viewer(self) -> None:
        window = self._viewer_window
        self._viewer_window = None
        self._viewer_canvas = None
        self._viewer_source = None
        self._viewer_photo = None
        self._viewer_image_id = None
        self._viewer_path = None
        if window is not None and window.winfo_exists():
            window.destroy()

    def start_ocr(self) -> None:
        self._start_recognition("baidu")

    def start_local_ocr(self) -> None:
        self._start_recognition("local")

    def _start_recognition(self, backend: str) -> None:
        if self.image_path is None:
            messagebox.showinfo(
                "请选择发票", "请先选择 JPG、JPEG、PNG 图片或 PDF 发票文件。"
            )
            return
        if self._scan_running:
            return
        self._scan_running = True
        self._scan_request_id += 1
        request_id = self._scan_request_id
        self.scan_button.configure(state="disabled")
        self.local_scan_button.configure(state="disabled")
        self.choose_button.configure(state="disabled")
        self.clear_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        backend_name = "百度 OCR" if backend == "baidu" else "本地 RapidOCR"
        if self.image_path is not None and self.image_path.suffix.lower() == ".pdf":
            backend_name += "（PDF 第 1 页）"
        action = "请求识别" if backend == "baidu" else "本地识别中"
        self.entry_status_var.set(
            f"{backend_name}{action}；完成后可检查并修改识别结果。"
        )
        threading.Thread(
            target=self._recognize_in_background,
            args=(self.image_path, request_id, backend),
            daemon=True,
        ).start()

    def _recognize_in_background(
        self, image_path: Path, request_id: int, backend: str
    ) -> None:
        try:
            if backend == "baidu":
                with pdf_support.recognition_image_path(image_path) as recognition_path:
                    result = ocr.recognize_detailed(recognition_path)
            else:
                result = local_ocr.recognize_detailed(image_path)
            self.root.after(0, self._finish_ocr, result, None, request_id, backend)
        except Exception:
            LOGGER.exception("Unexpected error while recognizing an invoice file")
            try:
                self.root.after(
                    0,
                    self._finish_ocr,
                    None,
                    "识别时发生意外错误；请查看终端日志。",
                    request_id,
                    backend,
                )
            except tk.TclError:
                LOGGER.info("The application closed before the OCR error could be displayed")

    def _finish_ocr(
        self,
        result: ocr.RecognitionResult | None,
        unexpected_error: str | None,
        request_id: int,
        backend: str,
    ) -> None:
        if request_id != self._scan_request_id:
            return
        self._scan_running = False
        self.scan_button.configure(state="normal")
        self.local_scan_button.configure(state="normal")
        self.choose_button.configure(state="normal")
        self.clear_button.configure(state="normal")
        self.save_button.configure(state="normal")
        if unexpected_error:
            self.entry_status_var.set(unexpected_error)
            return
        if result is None:
            self.entry_status_var.set("识别未返回结果，可手动填写。")
            return
        backend_name = "百度 OCR" if backend == "baidu" else "本地 RapidOCR"
        self._set_recognized_fields(result.common_fields)
        self.common_fields_var.set(self._common_fields_summary(result.common_fields))
        if result.record is None:
            self.ocr_baseline = None
            self.entry_status_var.set(
                f"{backend_name} 未完成 [{result.error_code}]："
                f"{result.message}。可继续手动填写或切换另一识别方式。"
            )
            self.edit_marker_var.set("当前使用手动录入兜底")
            return

        self.ocr_baseline = dict(result.record)
        for key in ("invoice_no", "date", "amount"):
            self.field_vars[key].set(str(result.record[key]))
        self.field_vars["category"].set("")
        warning = (
            f"另有 {len(result.field_warnings)} 条可选字段提示。"
            if result.field_warnings
            else ""
        )
        self.entry_status_var.set(
            f"{backend_name}已读出核心字段（{result.elapsed_ms} ms）；"
            "仍需核对票面并明确选择类别。"
            f"{warning}"
        )
        self._update_edit_marker()

    @staticmethod
    def _common_fields_summary(fields: ocr.CommonInvoiceFields | None) -> str:
        if fields is None:
            return "未取得可选票面信息。"
        labels = (
            ("invoice_type", "发票种类"),
            ("seller_name", "销售方"),
            ("amount_without_tax", "不含税合计"),
            ("total_tax", "合计税额"),
        )
        lines = [
            f"{label}：{fields[key] or '未识别/未填写'}"
            for key, label in labels
        ]
        items_summary = f"{len(fields['items'])} 行" if fields["items"] else "未识别"
        lines.append(f"商品明细：{items_summary}")
        return "\n".join(lines)

    def _set_recognized_fields(
        self, fields: ocr.CommonInvoiceFields | None
    ) -> None:
        for key in self.recognized_field_vars:
            value = fields[key] if fields else None
            self.recognized_field_vars[key].set(value or "未识别/未填写")

    def _clear_recognized_fields(self) -> None:
        for variable in self.recognized_field_vars.values():
            variable.set("")

    def _record_is_edited(self) -> bool:
        if self.ocr_baseline is None:
            return False
        parsers = {
            "invoice_no": normalize_invoice_no,
            "date": normalize_date,
            "amount": normalize_amount,
        }
        for key, parser in parsers.items():
            try:
                if parser(self.field_vars[key].get()) != self.ocr_baseline[key]:
                    return True
            except ValueError:
                return True
        return False

    def _update_edit_marker(self, *trace_info) -> None:
        if len(trace_info) not in {0, 3}:
            return
        if self.ocr_baseline is None:
            return
        if self._record_is_edited():
            self.edit_marker_var.set("该发票经过编辑（保存时会记录）")
        else:
            self.edit_marker_var.set("OCR 字段尚未修改")

    def save_current_invoice(self) -> None:
        reimburser = self.field_vars["reimburser"].get().strip()
        if not reimburser:
            messagebox.showerror("缺少报销人", "请输入报销人姓名。", parent=self.root)
            return
        raw_amount = self.field_vars["amount"].get().strip()
        try:
            amount = float(raw_amount)
        except ValueError:
            amount = raw_amount
        record = {
            "invoice_no": self.field_vars["invoice_no"].get().strip(),
            "date": self.field_vars["date"].get().strip(),
            "amount": amount,
            "category": self.field_vars["category"].get().strip(),
            "reimburser": reimburser,
            "is_edited": self._record_is_edited(),
        }
        try:
            inserted = storage.save_invoice(record, self.image_path)
        except storage.InvoiceValidationError as exc:
            messagebox.showerror(
                "发票校验未通过",
                "\n".join(f"• {error}" for error in exc.errors),
                parent=self.root,
            )
            return
        except (ValueError, FileNotFoundError, OSError, RuntimeError) as exc:
            messagebox.showerror("无法入库", str(exc), parent=self.root)
            return
        if not inserted:
            messagebox.showerror(
                "疑似重复报销",
                "该发票号已存在，数据库拒绝重复入库。",
                parent=self.root,
            )
            return
        messagebox.showinfo("入库成功", "发票已通过校验并保存。", parent=self.root)
        self.clear_form()
        self.refresh_invoices()

    def clear_form(self) -> None:
        self._close_image_viewer()
        for variable in self.field_vars.values():
            variable.set("")
        self.image_path = None
        self.path_var.set("")
        self.ocr_baseline = None
        self.preview_image = None
        self.preview_label.configure(image="", text="尚未选择发票")
        self._clear_recognized_fields()
        self.common_fields_var.set("可选票面信息将在 OCR 后显示。")
        self.edit_marker_var.set("尚未进行 OCR 识别")
        self.entry_status_var.set(
            "选择发票图片或 PDF 后可识别；也可不选文件，直接手动填写。"
        )

    def refresh_employees(self) -> None:
        employees = database.list_employees()
        names = [employee["name"] for employee in employees]
        self.reimburser_box.configure(values=names)
        self.filter_person_box.configure(values=["", *names])
        for item in self.employee_table.get_children():
            self.employee_table.delete(item)
        for employee in employees:
            self.employee_table.insert(
                "", "end", values=(employee["name"], employee["department"])
            )

    def add_employee(self) -> None:
        name = self.employee_name_var.get().strip()
        department = self.department_var.get().strip()
        if not name:
            messagebox.showerror("姓名不能为空", "请输入员工姓名。", parent=self.root)
            return
        if not database.add_employee(name, department):
            messagebox.showwarning(
                "员工已存在", "该员工姓名已登记。", parent=self.root
            )
            return
        self.employee_name_var.set("")
        self.department_var.set("")
        self.refresh_employees()

    def refresh_invoices(self) -> None:
        month = self.filter_month_var.get().strip()
        if month and (
            len(month) != 7
            or month[4] != "-"
            or not month[:4].isdigit()
            or not month[5:].isdigit()
            or not 1 <= int(month[5:]) <= 12
        ):
            messagebox.showerror(
                "月份格式错误", "月份请按 YYYY-MM 输入，例如 2026-11。", parent=self.root
            )
            return
        rows = query.query_invoices(
            reimburser=self.filter_person_var.get().strip() or None,
            month=month or None,
            category=self.filter_category_var.get().strip() or None,
        )
        for item in self.invoice_table.get_children():
            self.invoice_table.delete(item)
        for row in rows:
            self.invoice_table.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    row["date"],
                    row["invoice_no"],
                    row["reimburser"],
                    f"{float(row['amount']):.2f}",
                    row["category"],
                    "是" if row["is_edited"] else "否",
                    row["photo_path"],
                ),
            )
        summary = query.summarize_invoices(rows)
        self.summary_var.set(
            f"共 {summary.count} 笔，合计 ¥{summary.total_amount:.2f}"
        )

    def reset_filters(self) -> None:
        self.filter_person_var.set("")
        self.filter_month_var.set("")
        self.filter_category_var.set("")
        self.refresh_invoices()

    def delete_selected_invoice(self) -> None:
        selection = self.invoice_table.selection()
        if not selection:
            messagebox.showinfo("请选择记录", "请先选择一条发票记录。", parent=self.root)
            return
        invoice_no = self.invoice_table.item(selection[0], "values")[1]
        if not messagebox.askyesno(
            "确认删除", f"确定删除发票 {invoice_no} 的记录吗？", parent=self.root
        ):
            return
        if database.delete_invoice(invoice_no):
            self.refresh_invoices()
        else:
            messagebox.showerror(
                "删除失败", "记录不存在或无法删除。", parent=self.root
            )

    def open_selected_photo(self, event=None) -> None:
        if event is not None and event.widget is not self.invoice_table:
            return
        selection = self.invoice_table.selection()
        if not selection:
            messagebox.showinfo("请选择记录", "请先选择一条发票记录。", parent=self.root)
            return
        photo_path = self.invoice_table.item(selection[0], "values")[6]
        if not photo_path:
            messagebox.showinfo(
                "没有照片", "这条记录由手动录入，没有归档照片。", parent=self.root
            )
            return
        path = Path(photo_path)
        if not path.is_file():
            messagebox.showerror(
                "照片不存在", "归档照片已不存在或无法访问。", parent=self.root
            )
            return
        import webbrowser

        webbrowser.open(path.resolve().as_uri())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    init_database.init_database()
    if TkinterDnD is None:
        root = tk.Tk()
    else:
        try:
            root = TkinterDnD.Tk()
        except tk.TclError:
            LOGGER.exception("Unable to initialize Tk drag-and-drop support")
            root = tk.Tk()
    InvoiceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
