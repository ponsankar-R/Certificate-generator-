import csv
import io
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urlparse
from zipfile import ZIP_DEFLATED, ZipFile

import qrcode
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from sqlalchemy.exc import SQLAlchemyError

from database import SessionLocal
from models import Certificate


class CertificateRequestError(Exception):
    pass


MAX_TITLE_LENGTH = 200
MAX_NAME_LENGTH = 40


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


MAX_ROWS = _env_int("MAX_ROWS", _env_int("MAX_CSV_ROWS", 5000))
MAX_WORKERS = _env_int("MAX_WORKERS", 4)

DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
]

NAME_HEADER_CANDIDATES = {
    "candidatename",
    "candidate",
    "name",
    "studentname",
    "participantname",
}

UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    value = str(value).strip() if value is not None else ""
    if not value:
        raise ValueError("Date is required.")

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        pass

    raise ValueError("Invalid date format. Use YYYY-MM-DD.")


def _normalize_header(value: str) -> str:
    value = str(value or "").replace("\ufeff", "")
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _detect_csv_columns(fieldnames):
    name_col = None

    for col in fieldnames:
        normalized = _normalize_header(col)
        if normalized in NAME_HEADER_CANDIDATES:
            name_col = col
            break

    if not name_col:
        raise CertificateRequestError(
            "CSV must contain a 'Candidate Name' or 'Name' column."
        )

    return name_col


def _failed_row(row_number, candidate_name, date_value, reason):
    return {
        "row_number": row_number,
        "candidate_name": candidate_name or "",
        "date": date_value or "",
        "status": "Failed",
        "certificate_file": "",
        "reason": reason,
    }


def _extract_name_from_dict(item: dict) -> str:
    for key, val in item.items():
        if _normalize_header(key) in NAME_HEADER_CANDIDATES:
            return str(val or "").strip()
    for key in ("name", "candidate"):
        if key in item:
            return str(item[key] or "").strip()
    return ""


def _parse_and_validate_data(raw_bytes: bytes, general_date_input):
    if not general_date_input or not str(general_date_input).strip():
        raise CertificateRequestError("A certificate date is required.")

    try:
        certificate_date = _parse_date(general_date_input)
    except ValueError:
        raise CertificateRequestError(
            "Date is invalid. Please provide a valid date (e.g. YYYY-MM-DD)."
        )

    date_display_str = certificate_date.strftime("%d %b %Y")
    date_iso_str = certificate_date.isoformat()

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    text = text.strip()
    if not text:
        raise CertificateRequestError("Provided file is empty.")

    # Try parsing as JSON first
    parsed_json = None
    if text.startswith(("[", "{")):
        try:
            parsed_json = json.loads(text)
        except json.JSONDecodeError:
            parsed_json = None

    valid_items = []
    failed_items = []

    if parsed_json is not None:
        # Handles JSON array or JSON wrapper object: {"names": [...]}, {"candidates": [...]}
        raw_list = parsed_json
        if isinstance(parsed_json, dict):
            for candidate_key in ("names", "candidates", "data", "participants", "students"):
                if candidate_key in parsed_json and isinstance(parsed_json[candidate_key], list):
                    raw_list = parsed_json[candidate_key]
                    break
            else:
                for v in parsed_json.values():
                    if isinstance(v, list):
                        raw_list = v
                        break

        if not isinstance(raw_list, list):
            raise CertificateRequestError(
                "JSON structure must be an array of names or contain a list of candidates."
            )

        if len(raw_list) == 0:
            raise CertificateRequestError("JSON contains no candidate records.")

        if len(raw_list) > MAX_ROWS:
            raise CertificateRequestError(f"Records exceed maximum allowed limit: {MAX_ROWS}.")

        for idx, entry in enumerate(raw_list, start=1):
            if isinstance(entry, dict):
                raw_name = _extract_name_from_dict(entry)
            else:
                raw_name = str(entry or "").strip()

            if not raw_name:
                failed_items.append(
                    _failed_row(idx, raw_name, date_iso_str, "Candidate name is required.")
                )
                continue

            if len(raw_name) > MAX_NAME_LENGTH:
                failed_items.append(
                    _failed_row(
                        idx,
                        raw_name,
                        date_iso_str,
                        f"Candidate name must be {MAX_NAME_LENGTH} characters or fewer.",
                    )
                )
                continue

            valid_items.append(
                {
                    "row_number": idx,
                    "name": raw_name,
                    "date_obj": certificate_date,
                    "date_display": date_display_str,
                    "certificate_id": str(uuid.uuid4()),
                }
            )

    else:
        # Parse as CSV
        reader = csv.DictReader(io.StringIO(text))

        if not reader.fieldnames:
            raise CertificateRequestError("CSV file contains no valid columns or data.")

        name_col = _detect_csv_columns(reader.fieldnames)
        row_count = 0

        for row in reader:
            row_count += 1
            if row_count > MAX_ROWS:
                raise CertificateRequestError(f"CSV exceeds maximum allowed rows: {MAX_ROWS}.")

            # Skip blank rows
            if not any(str(v).strip() for v in row.values() if v is not None):
                continue

            row_number = reader.line_num
            raw_name = (row.get(name_col) or "").strip()

            if not raw_name:
                failed_items.append(
                    _failed_row(row_number, raw_name, date_iso_str, "Candidate name is required.")
                )
                continue

            if len(raw_name) > MAX_NAME_LENGTH:
                failed_items.append(
                    _failed_row(
                        row_number,
                        raw_name,
                        date_iso_str,
                        f"Candidate name must be {MAX_NAME_LENGTH} characters or fewer.",
                    )
                )
                continue

            valid_items.append(
                {
                    "row_number": row_number,
                    "name": raw_name,
                    "date_obj": certificate_date,
                    "date_display": date_display_str,
                    "certificate_id": str(uuid.uuid4()),
                }
            )

        if row_count == 0:
            raise CertificateRequestError("CSV contains no data rows.")

    if not valid_items and not failed_items:
        raise CertificateRequestError("File contains only blank rows.")

    return valid_items, failed_items


def _sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", str(name))
    name = name.encode("ascii", "ignore").decode("ascii")

    name = re.sub(r"[^\w\s-]", "", name).strip()
    name = re.sub(r"\s+", " ", name).replace(" ", "_")
    name = name[:80].strip("._")

    if not name:
        name = "certificate"

    reserved_windows_names = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }

    if name.upper() in reserved_windows_names:
        name = f"certificate_{name}"

    return name


def _assign_filenames(valid_items):
    used_filenames = set()

    for item in valid_items:
        base = _sanitize_filename(item["name"])
        filename = f"{base}.pdf"
        suffix = 1

        while filename.lower() in used_filenames:
            suffix += 1
            filename = f"{base}_{suffix}.pdf"

        used_filenames.add(filename.lower())
        item["filename"] = filename


def _build_report_bytes(rows):
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(
        [
            "Row",
            "Candidate Name",
            "Date",
            "Status",
            "Certificate File",
            "Reason",
        ]
    )

    sorted_rows = sorted(
        rows,
        key=lambda x: (
            x.get("row_number") or 0,
            str(x.get("candidate_name") or "").lower(),
        ),
    )

    for row in sorted_rows:
        writer.writerow(
            [
                row.get("row_number", ""),
                row.get("candidate_name", ""),
                row.get("date", ""),
                row.get("status", ""),
                row.get("certificate_file", ""),
                row.get("reason", ""),
            ]
        )

    return output.getvalue().encode("utf-8-sig")


def generate_certificates_zip(
    title: str,
    default_date=None,
    csv_bytes: bytes = None,
    general_date=None,
    file_bytes: bytes = None,
    **kwargs,
) -> io.BytesIO:
    title = (title or "").strip()

    if not title:
        raise CertificateRequestError("Title is required.")

    if len(title) > MAX_TITLE_LENGTH:
        raise CertificateRequestError(
            f"Title must be {MAX_TITLE_LENGTH} characters or fewer."
        )

    selected_date = default_date if default_date is not None else general_date
    if not selected_date:
        raise CertificateRequestError("A certificate issue date is required.")

    raw_data = file_bytes if file_bytes is not None else csv_bytes
    if not raw_data:
        raise CertificateRequestError("No file bytes provided.")

    valid_items, failed_items = _parse_and_validate_data(raw_data, selected_date)
    _assign_filenames(valid_items)

    temp_dir = tempfile.mkdtemp(prefix="certificate_zip_")
    generation_results = []

    try:
        if valid_items:
            worker_count = min(MAX_WORKERS, len(valid_items), 16)
            worker_count = max(1, worker_count)

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(_generate_single, item, title, temp_dir)
                    for item in valid_items
                ]

                for future in as_completed(futures):
                    generation_results.append(future.result())

        report_rows = failed_items + generation_results
        report_bytes = _build_report_bytes(report_rows)

        zip_buffer = io.BytesIO()

        with ZipFile(zip_buffer, "w", compression=ZIP_DEFLATED, compresslevel=9) as zip_file:
            zip_file.writestr("certificates/00_report.csv", report_bytes)

            successful = [
                item
                for item in generation_results
                if item.get("status") == "Generated"
            ]

            for result in sorted(
                successful,
                key=lambda x: str(x.get("certificate_file", "")).lower(),
            ):
                file_path = os.path.join(temp_dir, result["certificate_file"])
                if os.path.exists(file_path):
                    zip_file.write(
                        file_path,
                        arcname=f"certificates/{result['certificate_file']}",
                    )

        zip_buffer.seek(0)
        return zip_buffer

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _remove_file(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _generate_single(item, title: str, output_dir: str):
    file_path = os.path.join(output_dir, item["filename"])

    try:
        pdf_bytes = _render_pdf(item, title)

        with open(file_path, "wb") as pdf_file:
            pdf_file.write(pdf_bytes)

        db = SessionLocal()
        try:
            certificate = Certificate(
                id=item["certificate_id"],
                candidate_name=item["name"],
                title=title,
                certificate_date=item["date_obj"],
                status="generated",
            )
            db.add(certificate)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise
        finally:
            db.close()

        return {
            "row_number": item["row_number"],
            "candidate_name": item["name"],
            "date": item["date_obj"].isoformat(),
            "status": "Generated",
            "certificate_file": item["filename"],
            "reason": "",
        }

    except SQLAlchemyError:
        _remove_file(file_path)
        return {
            "row_number": item["row_number"],
            "candidate_name": item["name"],
            "date": item["date_obj"].isoformat(),
            "status": "Failed",
            "certificate_file": "",
            "reason": "Database error while saving certificate.",
        }

    except Exception as exc:
        _remove_file(file_path)
        return {
            "row_number": item["row_number"],
            "candidate_name": item["name"],
            "date": item["date_obj"].isoformat(),
            "status": "Failed",
            "certificate_file": "",
            "reason": str(exc),
        }


def _pdf_safe_text(value) -> str:
    return str(value).encode("cp1252", "replace").decode("cp1252", "replace")


def _wrap_text(text, font_name: str, font_size: int, max_width: float):
    text = _pdf_safe_text(text)
    words = text.split()

    if not words:
        return [""]

    lines = []
    current = ""

    for word in words:
        test = f"{current} {word}".strip()

        if stringWidth(test, font_name, font_size) <= max_width:
            current = test
            continue

        if current:
            lines.append(current)

        if stringWidth(word, font_name, font_size) <= max_width:
            current = word
        else:
            chunk = ""
            for ch in word:
                if stringWidth(chunk + ch, font_name, font_size) <= max_width:
                    chunk += ch
                else:
                    lines.append(chunk)
                    chunk = ch
            current = chunk

    if current:
        lines.append(current)

    return lines


def _verification_url(certificate_id: str) -> str:
    base_url = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
    return f"{base_url}/verify/{certificate_id}"


def _make_qr_png(data: str) -> io.BytesIO:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)

    return buffer


def _render_pdf(item, title: str) -> bytes:
    safe_title = _pdf_safe_text(title)
    safe_name = _pdf_safe_text(item["name"])

    packet = io.BytesIO()
    page_width, page_height = landscape(A4)

    c = canvas.Canvas(packet, pagesize=landscape(A4))
    c.setTitle(f"{safe_name} - {safe_title}")
    c.setAuthor("Certificate Generator")
    c.setSubject(safe_title)
    c.setPageCompression(1)

    margin = 20 * mm
    outer = 12 * mm

    # Outer border
    c.setStrokeColor(HexColor("#1f3a5f"))
    c.setLineWidth(1.4)
    c.rect(
        outer,
        outer,
        page_width - 2 * outer,
        page_height - 2 * outer,
        stroke=1,
        fill=0,
    )

    # Inner golden border
    c.setStrokeColor(HexColor("#c9a227"))
    c.setLineWidth(2.4)
    inner = outer + 3 * mm
    c.rect(
        inner,
        inner,
        page_width - 2 * inner,
        page_height - 2 * inner,
        stroke=1,
        fill=0,
    )

    # Date in top right
    c.setFillColor(HexColor("#222222"))
    c.setFont("Helvetica", 11)
    c.drawRightString(
        page_width - margin,
        page_height - margin,
        f"Date: {item['date_display']}",
    )

    # Header
    y = page_height - 42 * mm
    c.setFont("Helvetica-Bold", 30)
    c.setFillColor(HexColor("#1f3a5f"))
    c.drawCentredString(page_width / 2, y, "CERTIFICATE")

    y -= 8 * mm
    c.setFont("Helvetica", 11)
    c.setFillColor(HexColor("#777777"))
    c.drawCentredString(page_width / 2, y, "OF ACHIEVEMENT")

    # Title
    y -= 14 * mm
    title_font_size = 17
    title_max_width = page_width - 80 * mm

    title_lines = _wrap_text(
        safe_title,
        "Helvetica-Bold",
        title_font_size,
        title_max_width,
    )

    if len(title_lines) > 3:
        title_font_size = 13
        title_lines = _wrap_text(
            safe_title,
            "Helvetica-Bold",
            title_font_size,
            title_max_width,
        )

    if len(title_lines) > 4:
        title_lines = title_lines[:4]
        title_lines[-1] = title_lines[-1][:-1] + "..." if len(title_lines[-1]) > 1 else "..."

    c.setFont("Helvetica-Bold", title_font_size)
    c.setFillColor(HexColor("#111111"))

    title_line_height = title_font_size * 1.35
    for line in title_lines:
        c.drawCentredString(page_width / 2, y, line)
        y -= title_line_height

    # Certification text
    y -= 4 * mm
    c.setFont("Helvetica", 13)
    c.setFillColor(HexColor("#222222"))
    c.drawCentredString(page_width / 2, y, "This is to certify that")

    y -= 12 * mm

    # Candidate name
    c.setFont("Helvetica-Bold", 24)
    c.setFillColor(HexColor("#1f3a5f"))

    name_lines = _wrap_text(
        safe_name,
        "Helvetica-Bold",
        24,
        page_width - 70 * mm,
    )

    for line in name_lines[:2]:
        c.drawCentredString(page_width / 2, y, line)
        y -= 11 * mm

    y -= 2 * mm

    # Body
    c.setFont("Helvetica", 13)
    c.setFillColor(HexColor("#222222"))

    body = f'has successfully completed "{safe_title}" on {item["date_display"]}.'
    body_lines = _wrap_text(
        body,
        "Helvetica",
        13,
        page_width - 60 * mm,
    )

    for line in body_lines[:5]:
        c.drawCentredString(page_width / 2, y, line)
        y -= 6.5 * mm

    y -= 2 * mm

    congrats_lines = _wrap_text(
        "We congratulate with this achievement and wish continued success.",
        "Helvetica",
        13,
        page_width - 60 * mm,
    )

    for line in congrats_lines[:2]:
        c.drawCentredString(page_width / 2, y, line)
        y -= 6.5 * mm

    # Signatures
    sig_y = 32 * mm
    c.setStrokeColor(HexColor("#333333"))
    c.setLineWidth(0.8)

    c.line(35 * mm, sig_y, 90 * mm, sig_y)
    c.line(page_width - 90 * mm, sig_y, page_width - 35 * mm, sig_y)

    c.setFont("Helvetica", 10)
    c.setFillColor(HexColor("#333333"))

    c.drawCentredString((35 * mm + 90 * mm) / 2, sig_y - 5 * mm, "Signature")
    c.drawCentredString(
        page_width - (35 * mm + 90 * mm) / 2,
        sig_y - 5 * mm,
        "Authority",
    )

    # QR Code
    qr_url = _verification_url(item["certificate_id"])
    qr_image = _make_qr_png(qr_url)
    qr_size = 26 * mm

    c.drawImage(
        ImageReader(qr_image),
        page_width - margin - qr_size,
        margin - 2 * mm,
        width=qr_size,
        height=qr_size,
    )

    c.setFont("Helvetica", 6.5)
    c.drawRightString(
        page_width - margin,
        margin - 7 * mm,
        f"Certificate ID: {item['certificate_id']}",
    )

    c.showPage()
    c.save()

    return packet.getvalue()


def extract_certificate_id(value):
    if not value:
        return None

    value = str(value).strip().strip('"\'')
    candidates = []

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        path = parsed.path or ""

        if "/verify/" in path:
            candidates.append(path.split("/verify/")[-1].strip("/"))

        candidates.append(path.rstrip("/").split("/")[-1])
        candidates.append(parsed.query or "")
        candidates.append(parsed.fragment or "")
    else:
        if "/verify/" in value:
            candidates.append(value.split("/verify/")[-1].strip("/"))

        candidates.append(value.rstrip("/").split("/")[-1])
        candidates.append(value)

    for candidate in candidates:
        if not candidate:
            continue

        match = UUID_PATTERN.search(candidate)
        if match:
            return match.group(0)

    return None