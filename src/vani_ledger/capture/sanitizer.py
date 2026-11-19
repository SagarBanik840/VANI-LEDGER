"""Image upload sanitization, magic-byte validation, decompression-bomb guard, and SHA-256."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from vani_ledger.config import get_settings

ALLOWED_IMAGE_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)
MIME_TO_MAGIC_FORMAT: dict[str, str] = {
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
}
MAX_IMAGE_PIXELS: int = 36_000_000


class UploadTooLargeError(ValueError):
    """Raised when an uploaded payload exceeds MAX_UPLOAD_BYTES (maps to HTTP 413)."""


class InvalidUploadError(ValueError):
    """Raised when an uploaded payload fails type, magic-byte, or integrity checks (HTTP 422)."""


@dataclass(frozen=True)
class SanitizedImage:
    """Verified, re-encoded, EXIF-stripped image saved under a random UUID filename."""

    saved_path: Path | None
    sanitized_bytes: bytes
    content_sha256: str
    width: int
    height: int
    detected_format: str


def compute_sha256(data: bytes) -> str:
    """Return lowercase SHA-256 hex digest of raw payload bytes."""
    return hashlib.sha256(data).hexdigest()


def detect_image_magic(raw_bytes: bytes) -> str | None:
    """Identify image format strictly from leading binary magic bytes."""
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        return "webp"
    return None


def validate_and_sanitize_image(
    raw_bytes: bytes,
    content_type: str | None,
    *,
    filename: str | None = None,
    max_bytes: int | None = None,
    upload_dir: Path | None = None,
    save_to_disk: bool = True,
) -> SanitizedImage:
    """Validate size, MIME, magic bytes, and pixel bounds; re-encode RGB JPEG under UUID name.

    Security invariants:
    - Client ``filename`` is ignored to prevent path traversal (e.g. ``../../etc/passwd``).
    - Re-encoding strips EXIF metadata and any trailing polyglot payloads.
    """
    _ = filename  # Intentionally never used in any filesystem path.
    settings = get_settings()
    byte_limit = max_bytes if max_bytes is not None else settings.max_upload_bytes

    if len(raw_bytes) > byte_limit:
        raise UploadTooLargeError(f"Upload exceeds maximum allowed size of {byte_limit} bytes")
    if not raw_bytes:
        raise InvalidUploadError("Uploaded image payload is empty")

    normalized_mime = (content_type or "").split(";")[0].strip().lower()
    if normalized_mime not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise InvalidUploadError(
            "Unsupported image Content-Type; allowed: image/jpeg, image/png, image/webp"
        )

    detected_format = detect_image_magic(raw_bytes)
    if detected_format is None:
        raise InvalidUploadError("File magic bytes do not match JPEG, PNG, or WEBP")

    expected_format = MIME_TO_MAGIC_FORMAT[normalized_mime]
    if detected_format != expected_format:
        raise InvalidUploadError(
            f"Declared Content-Type ({normalized_mime}) does not match file signature"
        )

    content_sha256 = compute_sha256(raw_bytes)

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(raw_bytes)) as probe:
            width, height = probe.size
            if width <= 0 or height <= 0:
                raise InvalidUploadError("Image has invalid dimensions")
            if width * height > MAX_IMAGE_PIXELS:
                raise InvalidUploadError(
                    f"Image exceeds maximum pixel count ({MAX_IMAGE_PIXELS:,})"
                )
            probe.verify()

        with Image.open(io.BytesIO(raw_bytes)) as opened:
            width, height = opened.size
            if width * height > MAX_IMAGE_PIXELS:
                raise InvalidUploadError(
                    f"Image exceeds maximum pixel count ({MAX_IMAGE_PIXELS:,})"
                )
            rgb = opened.convert("RGB")
            rgb.info.clear()
            out_buf = io.BytesIO()
            rgb.save(out_buf, format="JPEG", quality=92, optimize=True)
            sanitized_bytes = out_buf.getvalue()
    except InvalidUploadError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise InvalidUploadError("Corrupt, truncated, or unsafe image payload") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    saved_path: Path | None = None
    if save_to_disk:
        target_dir = (upload_dir or settings.upload_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_filename = f"{uuid4().hex}.jpg"
        candidate = (target_dir / safe_filename).resolve()
        if candidate.parent != target_dir:
            raise InvalidUploadError("Invalid upload storage target")
        candidate.write_bytes(sanitized_bytes)
        saved_path = candidate

    return SanitizedImage(
        saved_path=saved_path,
        sanitized_bytes=sanitized_bytes,
        content_sha256=content_sha256,
        width=width,
        height=height,
        detected_format=detected_format,
    )
