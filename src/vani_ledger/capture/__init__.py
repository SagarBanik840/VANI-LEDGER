"""Multimodal capture pipeline (bill image sanitization, OCR/VLM, Decimal math, CSV, voice)."""

from vani_ledger.capture.bill_extractor import (
    DEMO_BILL_EXPECTED_PATH,
    DEMO_BILL_IMAGE_PATH,
    BillExtractor,
    CircuitBreaker,
    DemoFixtureExtractor,
    GeminiVisionExtractor,
    RawBillExtraction,
    TesseractExtractor,
    extract_and_audit_bill_file,
    extract_bill_with_fallback,
    load_demo_bill_fixture,
    parse_ocr_bill_text,
    process_bill_upload,
)
from vani_ledger.capture.csv_importer import (
    MAX_CSV_ROWS,
    CsvImportPayload,
    import_sales_csv,
)
from vani_ledger.capture.math_audit import audit_bill_line_items, verify_bill_math
from vani_ledger.capture.sanitizer import (
    ALLOWED_IMAGE_CONTENT_TYPES,
    MAX_IMAGE_PIXELS,
    InvalidUploadError,
    SanitizedImage,
    UploadTooLargeError,
    compute_sha256,
    detect_image_magic,
    validate_and_sanitize_image,
)
from vani_ledger.capture.voice_parser import parse_voice_transcript, process_voice_note

__all__ = [
    "ALLOWED_IMAGE_CONTENT_TYPES",
    "BillExtractor",
    "CircuitBreaker",
    "CsvImportPayload",
    "DEMO_BILL_EXPECTED_PATH",
    "DEMO_BILL_IMAGE_PATH",
    "DemoFixtureExtractor",
    "GeminiVisionExtractor",
    "InvalidUploadError",
    "MAX_CSV_ROWS",
    "MAX_IMAGE_PIXELS",
    "RawBillExtraction",
    "SanitizedImage",
    "TesseractExtractor",
    "UploadTooLargeError",
    "audit_bill_line_items",
    "compute_sha256",
    "detect_image_magic",
    "extract_and_audit_bill_file",
    "extract_bill_with_fallback",
    "import_sales_csv",
    "load_demo_bill_fixture",
    "parse_ocr_bill_text",
    "parse_voice_transcript",
    "process_bill_upload",
    "process_voice_note",
    "validate_and_sanitize_image",
    "verify_bill_math",
]
