#!/usr/bin/env python3
"""Convert Rajasthan municipal voter-list PDFs to validated UTF-8 CSV files.

Designed for the three-column 2026 Rajasthan municipal roll layout used by files
such as ``WithPhoto_UDAIPUR NAGAR NIGAM-Ward No-080-Part No-006.pdf``.

The script deliberately uses two sources:

* the PDF text layer for exact serial numbers, voter IDs, age, gender, and status;
* Hindi OCR on a temporary image-free copy for readable names and addresses.

It writes a CSV only after serials, IDs, demographics, and the cover-page totals
reconcile. OCR-sensitive rows remain visible through confidence and review fields.

Examples:
    python3 voter_pdf_to_csv.py roll.pdf --output-dir outputs
    python3 voter_pdf_to_csv.py ./pdf_folder --output-dir csv --jobs 4
    python3 voter_pdf_to_csv.py roll.pdf --tessdata-dir ./tessdata --overwrite
    python3 voter_pdf_to_csv.py roll.pdf --fail-on-review
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - dependency failure path
    raise SystemExit("Missing Python dependency: install it with 'python3 -m pip install pypdf'.") from exc


ID_RE = re.compile(r"^(?:[A-Z]{3}\d{7}|RJ/\d{2}/\d{3}/\d+)$")
SIMPLE_HOUSE_RE = re.compile(r"[0-9०-९A-Za-z/,.\- ]*")
DELETION_REASONS = {
    "": "",
    "E": "Deleted - Death",
    "S": "Deleted - Shifted",
    "R": "Deleted - Repetition",
}
CSV_FIELDS = [
    "serial_number",
    "voter_id",
    "name_hindi",
    "relative_name_hindi",
    "relation_type",
    "house_number_or_address",
    "age",
    "gender",
    "status",
    "deletion_code",
    "deletion_reason",
    "municipality",
    "ward_number",
    "part_number",
    "roll_year",
    "source_pdf",
    "source_pdf_page",
    "name_ocr_confidence",
    "relative_name_ocr_confidence",
    "needs_review",
]


class ConversionError(RuntimeError):
    pass


@dataclass
class CoverSummary:
    first_serial: int
    last_serial: int
    active_men: int
    active_women: int
    active_third_gender: int
    active_total: int


@dataclass
class Occurrence:
    serial_number: int
    voter_id: str
    name_raw: str
    relative_name_raw: str
    relation_type: str
    house_raw: str
    age: int | None
    gender: str
    deletion_code: str
    page: int
    order: int
    cell_left_pt: float
    id_y_pt: float
    name_y_pt: float
    relative_y_pt: float
    house_y_pt: float
    name: str = ""
    relative_name: str = ""
    house_number: str = ""
    name_ocr_confidence: float = 0.0
    relative_ocr_confidence: float = 0.0
    house_ocr_confidence: float = 0.0
    needs_review: bool = False


def normalized(text: str) -> str:
    return " ".join((text or "").replace("\n", " ").split())


def normalize_voter_id(text: str) -> str:
    value = re.sub(r"\s+", "", text or "").upper()
    return value if ID_RE.fullmatch(value) else ""


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ConversionError(f"Required command was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        raise ConversionError(f"Command failed: {' '.join(command)}\n{detail}") from exc


def find_executable(names: Iterable[str]) -> str:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    raise ConversionError(f"None of these required commands was found: {', '.join(names)}")


def page_items(page) -> list[dict]:
    items: list[dict] = []

    def visitor(text, cm, tm, font, size):
        if text and text.strip() and float(tm[5]) < 0:
            items.append(
                {
                    "text": text,
                    "x": float(tm[4]),
                    "y": float(tm[5]),
                    "order": len(items),
                }
            )

    page.extract_text(visitor_text=visitor)
    return items


def parse_cover_summary(items: list[dict]) -> CoverSummary:
    by_y: dict[float, list[tuple[float, int]]] = defaultdict(list)
    for item in items:
        text = normalized(item["text"])
        if re.fullmatch(r"\d+", text):
            by_y[round(item["y"], 1)].append((item["x"], int(text)))
    candidates = []
    for values in by_y.values():
        values.sort()
        if len(values) >= 6 and values[-1][0] > 450:
            candidates.append(values)
    if not candidates:
        raise ConversionError("Could not locate the official voter totals on the cover page.")
    values = max(candidates, key=lambda row: row[-1][0] - row[0][0])
    numbers = [number for _, number in values[-6:]]
    summary = CoverSummary(*numbers)
    if summary.active_men + summary.active_women + summary.active_third_gender != summary.active_total:
        raise ConversionError(f"The cover-page totals do not add up: {asdict(summary)}")
    return summary


def infer_year(items: list[dict]) -> int | None:
    values = []
    for item in items:
        values.extend(int(x) for x in re.findall(r"\b20\d{2}\b", normalized(item["text"])))
    return values[0] if values else None


def infer_filename_metadata(path: Path) -> dict:
    stem = path.stem
    ward = re.search(r"Ward\s*No[-_ ]*(\d+)", stem, re.IGNORECASE)
    part = re.search(r"Part\s*No[-_ ]*(\d+)", stem, re.IGNORECASE)
    municipality = re.search(r"WithPhoto[_ -]+(.+?)[-_ ]+Ward\s*No", stem, re.IGNORECASE)
    return {
        "ward_number": int(ward.group(1)) if ward else "",
        "part_number": int(part.group(1)) if part else "",
        "municipality": normalized(municipality.group(1).replace("_", " ")) if municipality else "",
    }


def cell_left(x: float) -> float:
    if x < 190:
        return 37.6
    if x < 365:
        return 211.2
    return 384.6


def line_parts(items: list[dict], left: float, target_y: float, tolerance: float = 1.5) -> list[dict]:
    right = left + 173.0
    return [
        item
        for item in items
        if left - 2 <= item["x"] < right and abs(item["y"] - target_y) <= tolerance
    ]


def value_after_colon(parts: list[dict]) -> tuple[str, str]:
    label: list[str] = []
    output: list[str] = []
    seen_colon = False
    for item in parts:
        text = item["text"]
        if not seen_colon:
            label.append(text)
            if ":" in text:
                seen_colon = True
                tail = text.split(":", 1)[1]
                if tail.strip():
                    output.append(tail)
        elif text.strip():
            output.append(text)
    return normalized("".join(label)), normalized(" ".join(output))


def nearest_field_line(
    items: list[dict], left: float, anchor_y: float, expected_delta: float
) -> tuple[float, str, str]:
    expected = anchor_y - expected_delta
    candidates: list[tuple[float, float, str, str]] = []
    ys = sorted(
        {
            round(item["y"], 1)
            for item in items
            if left - 2 <= item["x"] < left + 173.0 and anchor_y - 68 <= item["y"] <= anchor_y - 8
        }
    )
    for y in ys:
        parts = line_parts(items, left, y)
        joined = normalized("".join(item["text"] for item in parts))
        if ":" not in joined:
            continue
        label, value = value_after_colon(parts)
        candidates.append((abs(y - expected), y, label, value))
    if not candidates:
        return expected, "", ""
    _, y, label, value = min(candidates, key=lambda value: value[0])
    return y, label, value


def parse_serial_text(text: str) -> tuple[int, str] | None:
    match = re.fullmatch(r"([ESR])?\s*(\d{1,6})\s*([ESR])?", normalized(text))
    if not match:
        return None
    return int(match.group(2)), match.group(1) or match.group(3) or ""


def gender_from_raw(raw: str) -> str:
    if "पचरष" in raw or "पपरष" in raw:
        return "पुरुष"
    if "सल" in raw:
        return "महिला"
    if "तततलज" in raw or "ततलज" in raw:
        return "तृतीय लिंग"
    return normalized(raw)


def relation_from_label(label: str) -> str:
    if "पनत" in label:
        return "पति"
    if "नपतर" in label:
        return "पिता"
    if "मरतर" in label:
        return "माता"
    return normalized(label.split("कर", 1)[0]) or "अन्य"


def parse_occurrence(
    items: list[dict], serial_item: dict, serial: int, deletion_code: str, page_number: int
) -> Occurrence:
    left = cell_left(serial_item["x"])
    anchor_y = serial_item["y"]
    same_row = [
        item
        for item in items
        if left - 2 <= item["x"] < left + 173.0 and abs(item["y"] - anchor_y) <= 2
    ]
    voter_id = next(
        (normalize_voter_id(item["text"]) for item in same_row if normalize_voter_id(item["text"])), ""
    )
    name_y, _, name_raw = nearest_field_line(items, left, anchor_y, 14.3)
    relative_y, relation_label, relative_raw = nearest_field_line(items, left, anchor_y, 27.2)
    house_y, _, house_raw = nearest_field_line(items, left, anchor_y, 41.9)
    _, _, age_gender_raw = nearest_field_line(items, left, anchor_y, 53.5)
    age_match = re.search(r"\b(\d{2,3})\b", age_gender_raw)
    age = int(age_match.group(1)) if age_match else None
    gender_raw = re.sub(r"\b\d{2,3}\b", "", age_gender_raw).strip()
    return Occurrence(
        serial_number=serial,
        voter_id=voter_id,
        name_raw=name_raw,
        relative_name_raw=relative_raw,
        relation_type=relation_from_label(relation_label),
        house_raw=house_raw,
        age=age,
        gender=gender_from_raw(gender_raw),
        deletion_code=deletion_code,
        page=page_number,
        order=serial_item["order"],
        cell_left_pt=left,
        id_y_pt=anchor_y,
        name_y_pt=name_y,
        relative_y_pt=relative_y,
        house_y_pt=house_y,
    )


def extract_occurrences(reader: PdfReader) -> tuple[list[Occurrence], CoverSummary, int | None]:
    first_page_items = page_items(reader.pages[0])
    summary = parse_cover_summary(first_page_items)
    roll_year = infer_year(first_page_items)
    occurrences: list[Occurrence] = []
    for page_number, page in enumerate(reader.pages, 1):
        items = page_items(page)
        for item in items:
            parsed = parse_serial_text(item["text"])
            if not parsed:
                continue
            serial, code = parsed
            left = cell_left(item["x"])
            if not 4 <= item["x"] - left <= 30:
                continue
            name_y, label, _ = nearest_field_line(items, left, item["y"], 14.3)
            if not label or abs(name_y - (item["y"] - 14.3)) > 4:
                continue
            occurrences.append(parse_occurrence(items, item, serial, code, page_number))
    if not occurrences:
        raise ConversionError("No voter-card records were detected in the PDF.")
    return occurrences, summary, roll_year


def choose_primary_records(
    occurrences: list[Occurrence], summary: CoverSummary
) -> tuple[list[Occurrence], list[str]]:
    by_serial: dict[int, list[Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        if summary.first_serial <= occurrence.serial_number <= summary.last_serial:
            by_serial[occurrence.serial_number].append(occurrence)
    expected = list(range(summary.first_serial, summary.last_serial + 1))
    missing = [serial for serial in expected if serial not in by_serial]
    if missing:
        raise ConversionError(f"Missing voter serials: {missing[:30]}{' ...' if len(missing) > 30 else ''}")

    errors: list[str] = []
    records: list[Occurrence] = []
    for serial in expected:
        candidates = sorted(by_serial[serial], key=lambda item: (item.page, item.order))
        voter_ids = {item.voter_id for item in candidates if item.voter_id}
        if len(voter_ids) > 1:
            errors.append(f"serial {serial} has conflicting voter IDs: {sorted(voter_ids)}")
        primary = candidates[0]
        status_codes = {item.deletion_code for item in candidates if item.deletion_code}
        if len(status_codes) > 1:
            errors.append(f"serial {serial} has conflicting deletion codes: {sorted(status_codes)}")
        if status_codes:
            primary.deletion_code = sorted(status_codes)[0]
        records.append(primary)
    return records, errors


def ensure_hindi_model(tesseract: str, tessdata_dir: Path | None) -> None:
    command = [tesseract, "--list-langs"]
    if tessdata_dir:
        command.extend(["--tessdata-dir", str(tessdata_dir)])
    result = run(command, capture=True)
    languages = {line.strip() for line in (result.stdout or "").splitlines()}
    if "hin" not in languages:
        location = f" in {tessdata_dir}" if tessdata_dir else ""
        raise ConversionError(
            "Hindi Tesseract data ('hin') was not found"
            f"{location}. Install the Hindi language pack or pass --tessdata-dir."
        )


def load_tsv(path: Path) -> list[dict]:
    words: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("level") != "5" or not row.get("text", "").strip():
                continue
            try:
                words.append(
                    {
                        "text": row["text"].strip(),
                        "left": int(row["left"]),
                        "top": int(row["top"]),
                        "width": int(row["width"]),
                        "height": int(row["height"]),
                        "conf": float(row["conf"]),
                        "line": (row["block_num"], row["par_num"], row["line_num"]),
                    }
                )
            except (KeyError, ValueError):
                continue
    return words


def clean_ocr_field(text: str, field: str) -> str:
    value = normalized(text)
    if field in {"name", "relative"}:
        value = re.sub(r"^.*?[नत]ाम\s*[:ः]?\s*", "", value, count=1)
    elif field == "house":
        value = re.sub(r"^.*?(?:संख्या|सख्या|संखया)\s*[:ः]?\s*", "", value, count=1)
    value = re.sub(r"[|_~^`]+", " ", value).replace("\u200d", "")
    value = re.sub(r"^ः+", "", value)
    value = re.sub(r"^पी0\s*", "पी० ", value)
    value = re.sub(r"^मो\s*(?:0|[.?>]+)\s*", "मो० ", value)
    value = value.replace("ि०0ं", "िं")
    return re.sub(r"\s+", " ", value).strip(" .,:;।-'\"")


def extract_ocr_line(
    words: list[dict], left_pt: float, y_pt: float, field: str, dpi: int
) -> tuple[str, float]:
    scale = dpi / 72
    left_px = left_pt * scale - 8
    right_px = (left_pt + 127) * scale
    target_y = abs(y_pt) * scale - (19 * dpi / 300)
    candidates = []
    for word in words:
        center_x = word["left"] + word["width"] / 2
        center_y = word["top"] + word["height"] / 2
        if left_px <= center_x <= right_px and abs(center_y - target_y) <= (34 * dpi / 300):
            candidates.append((word, center_y))
    if not candidates:
        return "", 0.0
    by_line: dict[tuple, list[tuple[dict, float]]] = defaultdict(list)
    for word, center_y in candidates:
        by_line[word["line"]].append((word, center_y))
    selected = min(
        by_line.values(),
        key=lambda group: abs(sorted(center_y for _, center_y in group)[len(group) // 2] - target_y),
    )
    ordered = sorted((word for word, _ in selected), key=lambda word: word["left"])
    text = clean_ocr_field(" ".join(word["text"] for word in ordered), field)
    confidence = sum(max(0.0, word["conf"]) for word in ordered) / len(ordered)
    return text, round(confidence, 1)


def choose_house(raw: str, ocr: str) -> str:
    if SIMPLE_HOUSE_RE.fullmatch(raw or ""):
        return (raw or "").strip()
    raw_number = re.match(r"^\s*(\d+(?:[/.\-]\d+)?)", raw or "")
    if raw_number and ocr:
        ocr = re.sub(
            r"^\s*[+]?\s*[0-9०-९]+(?:[/.#\-][0-9०-९]+)?",
            raw_number.group(1),
            ocr,
            count=1,
        )
    return ocr or raw


def ocr_records(
    pdf: Path,
    records: list[Occurrence],
    workdir: Path,
    ghostscript: str,
    tesseract: str,
    tessdata_dir: Path | None,
    dpi: int,
    jobs: int,
    name_threshold: float,
    relative_threshold: float,
) -> None:
    clean_pdf = workdir / "no-images.pdf"
    rendered_dir = workdir / "pages"
    ocr_dir = workdir / "ocr"
    rendered_dir.mkdir()
    ocr_dir.mkdir()
    run(
        [
            ghostscript,
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=pdfwrite",
            "-dFILTERIMAGE",
            f"-sOutputFile={clean_pdf}",
            str(pdf),
        ]
    )
    run(
        [
            ghostscript,
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=pnggray",
            f"-r{dpi}",
            f"-sOutputFile={rendered_dir / 'page-%04d.png'}",
            str(clean_pdf),
        ]
    )
    pages = sorted({record.page for record in records})

    def recognize(page: int) -> tuple[int, Path]:
        image = rendered_dir / f"page-{page:04d}.png"
        output_base = ocr_dir / f"page-{page:04d}"
        command = [tesseract, str(image), str(output_base), "-l", "hin"]
        if tessdata_dir:
            command.extend(["--tessdata-dir", str(tessdata_dir)])
        command.extend(["--psm", "6", "-c", "tessedit_create_tsv=1"])
        run(command)
        return page, output_base.with_suffix(".tsv")

    page_words: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {executor.submit(recognize, page): page for page in pages}
        for future in as_completed(futures):
            page, tsv = future.result()
            page_words[page] = load_tsv(tsv)

    for record in records:
        words = page_words[record.page]
        name, name_conf = extract_ocr_line(words, record.cell_left_pt, record.name_y_pt, "name", dpi)
        relative, relative_conf = extract_ocr_line(
            words, record.cell_left_pt, record.relative_y_pt, "relative", dpi
        )
        house, house_conf = extract_ocr_line(words, record.cell_left_pt, record.house_y_pt, "house", dpi)
        record.name = name
        record.relative_name = relative
        record.house_number = choose_house(record.house_raw, house)
        record.name_ocr_confidence = name_conf
        record.relative_ocr_confidence = relative_conf
        record.house_ocr_confidence = house_conf
        record.needs_review = (
            not name
            or not relative
            or name_conf < name_threshold
            or relative_conf < relative_threshold
        )


def validate_records(
    records: list[Occurrence], summary: CoverSummary, extraction_errors: list[str]
) -> dict:
    errors = list(extraction_errors)
    warnings: list[str] = []
    serials = [record.serial_number for record in records]
    expected_serials = list(range(summary.first_serial, summary.last_serial + 1))
    if serials != expected_serials:
        errors.append("Serial numbers are not complete and contiguous.")
    voter_ids = [record.voter_id for record in records if record.voter_id]
    if len(voter_ids) != len(set(voter_ids)):
        errors.append("One or more nonblank voter IDs are duplicated.")
    invalid_ids = [value for value in voter_ids if not ID_RE.fullmatch(value)]
    if invalid_ids:
        errors.append(f"Invalid voter ID format: {invalid_ids[:10]}")
    if any(record.age is None or not 18 <= record.age <= 120 for record in records):
        bad = [record.serial_number for record in records if record.age is None or not 18 <= record.age <= 120]
        errors.append(f"Missing or out-of-range ages at serials: {bad[:20]}")
    allowed_genders = {"पुरुष", "महिला", "तृतीय लिंग"}
    bad_genders = [record.serial_number for record in records if record.gender not in allowed_genders]
    if bad_genders:
        errors.append(f"Unrecognized gender values at serials: {bad_genders[:20]}")
    if any(not record.name or not record.relative_name for record in records):
        errors.append("One or more names or relative names are blank after OCR.")
    active = [record for record in records if not record.deletion_code]
    derived = {
        "active_men": sum(record.gender == "पुरुष" for record in active),
        "active_women": sum(record.gender == "महिला" for record in active),
        "active_third_gender": sum(record.gender == "तृतीय लिंग" for record in active),
        "active_total": len(active),
    }
    official = {
        "active_men": summary.active_men,
        "active_women": summary.active_women,
        "active_third_gender": summary.active_third_gender,
        "active_total": summary.active_total,
    }
    if derived != official:
        errors.append(f"Derived active-voter totals do not match the cover page: {derived} != {official}")
    review_count = sum(record.needs_review for record in records)
    if review_count:
        warnings.append(
            f"{review_count} rows have low OCR confidence and are marked needs_review=Yes."
        )
    return {
        "result": "FAIL" if errors else ("PASS_WITH_REVIEW" if warnings else "PASS"),
        "errors": errors,
        "warnings": warnings,
        "official_summary": asdict(summary),
        "derived_summary": {
            "record_count": len(records),
            "active_count": len(active),
            "deleted_count": len(records) - len(active),
            **derived,
            "unique_nonblank_voter_ids": len(set(voter_ids)),
            "blank_voter_ids": len(records) - len(voter_ids),
            "needs_review": review_count,
        },
    }


def csv_row(record: Occurrence, metadata: dict, source: Path) -> dict:
    code = record.deletion_code
    return {
        "serial_number": record.serial_number,
        "voter_id": record.voter_id,
        "name_hindi": record.name,
        "relative_name_hindi": record.relative_name,
        "relation_type": record.relation_type,
        "house_number_or_address": record.house_number,
        "age": record.age,
        "gender": record.gender,
        "status": "Active" if not code else "Deleted",
        "deletion_code": code,
        "deletion_reason": DELETION_REASONS.get(code, "Deleted - Other"),
        "municipality": metadata.get("municipality", ""),
        "ward_number": metadata.get("ward_number", ""),
        "part_number": metadata.get("part_number", ""),
        "roll_year": metadata.get("roll_year", ""),
        "source_pdf": source.name,
        "source_pdf_page": record.page,
        "name_ocr_confidence": record.name_ocr_confidence,
        "relative_name_ocr_confidence": record.relative_ocr_confidence,
        "needs_review": "Yes" if record.needs_review else "No",
    }


def write_outputs(
    output: Path,
    audit_output: Path,
    records: list[Occurrence],
    metadata: dict,
    source: Path,
    audit: dict,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(csv_row(record, metadata, source) for record in records)
    os.replace(temporary, output)
    audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_written_csv(output: Path, records: list[Occurrence]) -> None:
    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != len(records):
        raise ConversionError(f"CSV read-back row count mismatch: {len(rows)} != {len(records)}")
    if [int(row["serial_number"]) for row in rows] != [record.serial_number for record in records]:
        raise ConversionError("CSV read-back serials do not match the extracted records.")
    if any(len(row) != len(CSV_FIELDS) for row in rows):
        raise ConversionError("CSV read-back found a malformed row or column mismatch.")


def convert_one(pdf: Path, args, tools: dict[str, str]) -> tuple[Path, dict]:
    output = args.output_dir / f"{pdf.stem}.csv"
    audit_output = args.output_dir / f"{pdf.stem}.validation.json"
    if output.exists() and not args.overwrite:
        raise ConversionError(f"Output already exists (use --overwrite): {output}")
    reader = PdfReader(pdf)
    if reader.is_encrypted:
        raise ConversionError("Encrypted PDFs are not supported.")
    occurrences, summary, roll_year = extract_occurrences(reader)
    records, extraction_errors = choose_primary_records(occurrences, summary)
    metadata = infer_filename_metadata(pdf)
    metadata["roll_year"] = args.roll_year or roll_year or ""
    if args.municipality:
        metadata["municipality"] = args.municipality
    if args.ward_number is not None:
        metadata["ward_number"] = args.ward_number
    if args.part_number is not None:
        metadata["part_number"] = args.part_number

    with tempfile.TemporaryDirectory(prefix="voter_pdf_") as temp_name:
        ocr_records(
            pdf,
            records,
            Path(temp_name),
            tools["ghostscript"],
            tools["tesseract"],
            args.tessdata_dir,
            args.dpi,
            args.jobs,
            args.name_confidence,
            args.relative_confidence,
        )
    audit = validate_records(records, summary, extraction_errors)
    audit.update(
        {
            "source_pdf": str(pdf.resolve()),
            "output_csv": str(output.resolve()),
            "metadata": metadata,
            "ocr": {
                "language": "hin",
                "dpi": args.dpi,
                "name_review_threshold": args.name_confidence,
                "relative_review_threshold": args.relative_confidence,
            },
        }
    )
    if audit["errors"]:
        audit_output.parent.mkdir(parents=True, exist_ok=True)
        audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ConversionError("Validation failed; CSV was not written. See " + str(audit_output))
    write_outputs(output, audit_output, records, metadata, pdf, audit)
    verify_written_csv(output, records)
    if args.fail_on_review and audit["derived_summary"]["needs_review"]:
        raise ConversionError(
            f"CSV was written, but {audit['derived_summary']['needs_review']} OCR rows require review."
        )
    return output, audit


def collect_pdfs(inputs: list[Path], recursive: bool) -> list[Path]:
    pdfs: list[Path] = []
    for value in inputs:
        path = value.expanduser()
        if path.is_file() and path.suffix.lower() == ".pdf":
            pdfs.append(path.resolve())
        elif path.is_dir():
            pdfs.extend((path.rglob("*.pdf") if recursive else path.glob("*.pdf")))
        else:
            raise ConversionError(f"Input is not a PDF file or directory: {value}")
    unique = sorted({path.resolve() for path in pdfs})
    if not unique:
        raise ConversionError("No PDF files were found.")
    return unique


def parse_selection(value: str, count: int) -> list[int]:
    value = value.strip().lower()
    if value in {"", "a", "all"}:
        return list(range(count))
    selected: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            selected.update(range(start - 1, end))
        else:
            selected.add(int(part) - 1)
    if not selected or min(selected) < 0 or max(selected) >= count:
        raise ValueError("Selection contains a number outside the displayed list.")
    return sorted(selected)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def interactive_argv() -> list[str]:
    print("\n┌──────────────────────────────────────────────┐")
    print("│     Rajasthan Voter PDF → CSV Converter     │")
    print("└──────────────────────────────────────────────┘")
    print("Strict totals validation • Hindi OCR • Batch mode\n")
    while True:
        folder_text = ask("Folder containing voter PDFs", str(Path.cwd()))
        folder = Path(folder_text).expanduser().resolve()
        if not folder.is_dir():
            print("  That folder does not exist. Please try again.\n")
            continue
        recursive = ask("Include PDFs in subfolders? (y/N)", "n").lower().startswith("y")
        pdfs = sorted(folder.rglob("*.pdf") if recursive else folder.glob("*.pdf"))
        if not pdfs:
            print("  No PDF files were found there. Please try again.\n")
            continue
        break

    print(f"\nFound {len(pdfs)} PDF file(s):")
    for index, pdf in enumerate(pdfs, 1):
        label = str(pdf.relative_to(folder)) if recursive else pdf.name
        print(f"  {index:>3}. {label}")
    while True:
        try:
            selection = parse_selection(
                ask("Select files (all, 1,3, 2-5)", "all"), len(pdfs)
            )
            break
        except (ValueError, TypeError):
            print("  Invalid selection. Use 'all', a number, commas, or a range.\n")

    output_dir = Path(ask("Output folder", str(folder / "voter_csv"))).expanduser().resolve()
    fail_on_review = ask("Treat low-confidence OCR as a failed run? (y/N)", "n").lower().startswith("y")
    jobs = ask("Parallel OCR jobs", str(min(4, os.cpu_count() or 1)))
    overwrite = ask("Overwrite existing CSV files? (y/N)", "n").lower().startswith("y")

    chosen = [pdfs[index] for index in selection]
    print("\nReady:")
    print(f"  PDFs:        {len(chosen)}")
    print(f"  Output:      {output_dir}")
    print(f"  OCR jobs:    {jobs}")
    print(f"  Review mode: {'fail when review is needed' if fail_on_review else 'flag rows in CSV'}")
    if not ask("Start conversion? (Y/n)", "y").lower().startswith("y"):
        raise SystemExit("Cancelled.")
    argv = [str(pdf) for pdf in chosen] + ["--output-dir", str(output_dir), "--jobs", jobs]
    if fail_on_review:
        argv.append("--fail-on-review")
    if overwrite:
        argv.append("--overwrite")
    print()
    return argv


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Convert Rajasthan municipal voter-list PDFs to strictly validated CSV files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Dependencies:
  Python package: pypdf
  Commands: Ghostscript (gs), Tesseract OCR, and the Tesseract Hindi model (hin)

macOS with Homebrew:
  brew install ghostscript tesseract tesseract-lang
  python3 -m pip install pypdf

Ubuntu/Debian:
  sudo apt-get install ghostscript tesseract-ocr tesseract-ocr-hin
  python3 -m pip install pypdf

Each successful PDF produces a .csv file and a .validation.json audit report.
Rows with uncertain Hindi OCR are retained and marked needs_review=Yes.
Use --fail-on-review when a nonzero exit status is required for those rows.
""",
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="PDF file(s) or directories containing PDFs")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/voter_csv"))
    parser.add_argument("--recursive", action="store_true", help="Search input directories recursively")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--tessdata-dir", type=Path)
    parser.add_argument("--name-confidence", type=float, default=75.0)
    parser.add_argument("--relative-confidence", type=float, default=70.0)
    parser.add_argument("--fail-on-review", action="store_true")
    parser.add_argument("--municipality", help="Override municipality metadata")
    parser.add_argument("--ward-number", type=int, help="Override ward metadata")
    parser.add_argument("--part-number", type=int, help="Override part metadata")
    parser.add_argument("--roll-year", type=int, help="Override roll-year metadata")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if not 200 <= args.dpi <= 600:
        parser.error("--dpi must be between 200 and 600")
    if args.tessdata_dir:
        args.tessdata_dir = args.tessdata_dir.expanduser().resolve()
    else:
        bundled_tessdata = Path(__file__).resolve().with_name("tessdata")
        if (bundled_tessdata / "hin.traineddata").is_file():
            args.tessdata_dir = bundled_tessdata
    args.output_dir = args.output_dir.expanduser().resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) == 1:
        argv = interactive_argv()
    args = parse_args(argv)
    try:
        pdfs = collect_pdfs(args.inputs, args.recursive)
        tools = {
            "ghostscript": find_executable(["gs", "gswin64c", "gswin32c"]),
            "tesseract": find_executable(["tesseract"]),
        }
        ensure_hindi_model(tools["tesseract"], args.tessdata_dir)
    except ConversionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    failures = 0
    for index, pdf in enumerate(pdfs, 1):
        print(f"[{index}/{len(pdfs)}] {pdf.name}")
        try:
            output, audit = convert_one(pdf, args, tools)
            summary = audit["derived_summary"]
            print(
                f"  PASS: {output} | rows={summary['record_count']} "
                f"active={summary['active_count']} deleted={summary['deleted_count']} "
                f"review={summary['needs_review']}"
            )
        except (ConversionError, OSError, ValueError) as exc:
            failures += 1
            print(f"  FAILED: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
