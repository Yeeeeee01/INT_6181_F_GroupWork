"""Invoice validation and photo storage for the integrated application."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
from uuid import uuid4

import database
import pdf_support
import validate

PROJECT_DIR = Path(__file__).resolve().parent
PHOTOS_DIR = PROJECT_DIR / "photos"
ALLOWED_INVOICE_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf"}


class InvoiceValidationError(ValueError):
    """Raised when an invoice fails one or more business validation rules."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def validate_image_path(image_path: str | Path) -> Path:
    """Return a usable invoice image or PDF path."""
    path = Path(image_path).expanduser()
    if path.suffix.lower() not in ALLOWED_INVOICE_SUFFIXES:
        raise ValueError("文件格式不支持，请选择 JPG、JPEG、PNG 或 PDF 文件。")
    if not path.is_file():
        raise FileNotFoundError("找不到发票文件，请重新选择。")
    if path.suffix.lower() == ".pdf":
        return pdf_support.validate_pdf(path)
    return path.resolve()


def save_invoice(
    record: dict,
    image_path: str | Path | None = None,
    *,
    db_path: str | None = None,
    photos_dir: str | Path | None = None,
) -> bool:
    """Validate, archive an optional photo, and insert an invoice atomically."""
    errors = validate.check(record, db_path=db_path)
    if errors:
        raise InvoiceValidationError(errors)

    photo_path = ""
    archived_photo: Path | None = None
    committed = False
    try:
        if image_path is not None:
            source = validate_image_path(image_path)
            target_dir = Path(photos_dir) if photos_dir is not None else PHOTOS_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_invoice_no = re.sub(
                r"[^A-Za-z0-9_-]+", "_", record["invoice_no"]
            ).strip("_")[:64] or "invoice"
            archived_photo = target_dir / (
                f"{safe_invoice_no}_{uuid4().hex}{source.suffix.lower()}"
            )
            shutil.copy2(source, archived_photo)
            photo_path = str(archived_photo.resolve())

        inserted = database.insert(record, photo_path, db_path=db_path)
        if inserted:
            committed = True
        return inserted
    finally:
        if archived_photo is not None and not committed:
            archived_photo.unlink(missing_ok=True)
