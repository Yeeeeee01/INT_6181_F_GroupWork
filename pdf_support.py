"""PDF validation and first-page rendering for invoice OCR."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Iterator

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_RENDER_PIXELS = 4096
PDF_SUFFIX = ".pdf"


def _pymupdf_module():
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError(
            "PDF 功能依赖 PyMuPDF，请安装 requirements.txt 中的依赖。"
        ) from exc
    return pymupdf


def _open_pdf(pdf_path: str | Path):
    path = Path(pdf_path).expanduser()
    if path.suffix.lower() != PDF_SUFFIX:
        raise ValueError("请选择 PDF 文件。")
    pymupdf = _pymupdf_module()
    try:
        if not path.is_file():
            raise FileNotFoundError("找不到 PDF 文件，请重新选择。")
        if path.stat().st_size > MAX_PDF_BYTES:
            raise ValueError("PDF 文件超过 20 MiB 大小限制。")
        document = pymupdf.open(path)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("PDF 文件无法读取或已损坏，请重新选择。") from exc
    if document.is_encrypted:
        document.close()
        raise ValueError("暂不支持加密 PDF，请先解除密码保护。")
    if document.page_count < 1:
        document.close()
        raise ValueError("PDF 不包含可识别的页面。")
    return document


def validate_pdf(pdf_path: str | Path) -> Path:
    """Validate a PDF and return its absolute path."""
    path = Path(pdf_path).expanduser()
    document = _open_pdf(path)
    document.close()
    return path.resolve()


def first_page_text_words(
    pdf_path: str | Path,
) -> tuple[float, list[tuple[float, float, float, float, str]]]:
    """Return first-page text words with their positions and page width."""
    document = _open_pdf(pdf_path)
    try:
        page = document.load_page(0)
        words = [
            (
                float(word[0]),
                float(word[1]),
                float(word[2]),
                float(word[3]),
                str(word[4]),
            )
            for word in page.get_text("words")
            if len(word) >= 5 and str(word[4]).strip()
        ]
        return float(page.rect.width), words
    finally:
        document.close()


def render_first_page(pdf_path: str | Path) -> bytes:
    """Render the first PDF page to JPEG bytes at a bounded resolution."""
    pymupdf = _pymupdf_module()
    document = _open_pdf(pdf_path)
    try:
        page = document.load_page(0)
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            raise ValueError("PDF 第一页尺寸无效。")
        scale = min(200 / 72, MAX_RENDER_PIXELS / max(rect.width, rect.height))
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        return pixmap.tobytes("jpeg", jpg_quality=90)
    except (RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("PDF 第一页无法渲染，请检查文件是否损坏。") from exc
    finally:
        document.close()


@contextmanager
def recognition_image_path(file_path: str | Path) -> Iterator[Path]:
    """Yield the original image or a temporary JPEG rendered from a PDF."""
    path = Path(file_path).expanduser()
    if path.suffix.lower() != PDF_SUFFIX:
        yield path
        return

    image_bytes = render_first_page(path)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(image_bytes)
        yield temporary_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
