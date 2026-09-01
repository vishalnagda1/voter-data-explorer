#!/usr/bin/env python3
"""Convert Rajasthan municipal voter-list PDFs to validated UTF-8 CSV files.

Designed for the three-column 2026 Rajasthan municipal roll layout used by files
such as ``WithPhoto_UDAIPUR NAGAR NIGAM-Ward No-080-Part No-006.pdf``.

The script deliberately uses two sources:

* the PDF text layer for exact serial numbers, voter IDs, age, gender, and status;
* Hindi OCR on a temporary image-free copy for readable names and addresses.

It writes a CSV only after serials, IDs, demographics, and the cover-page totals
reconcile. OCR-sensitive rows remain visible through confidence and review fields.
Hindi names are preserved; English columns use deterministic transliteration and
an editable preferred-spelling dictionary, with generated spellings flagged.

Examples:
    python3 voter_pdf_to_csv.py roll.pdf --output-dir outputs
    python3 voter_pdf_to_csv.py ./pdf_folder --output-dir csv --jobs 4
    python3 voter_pdf_to_csv.py roll.pdf --tessdata-dir ./tessdata --overwrite
    python3 voter_pdf_to_csv.py roll.pdf --fail-on-review
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - dependency failure path
    raise SystemExit("Missing Python dependency: run './setup_voter_converter.sh'.") from exc

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
    "first_name",
    "middle_name",
    "last_name",
    "name_split_needs_review",
    "name_english",
    "english_first_name",
    "english_middle_name",
    "english_last_name",
    "english_name_needs_review",
    "relative_name_hindi",
    "relative_first_name",
    "relative_middle_name",
    "relative_last_name",
    "relative_name_split_needs_review",
    "relative_name_english",
    "relative_english_first_name",
    "relative_english_middle_name",
    "relative_english_last_name",
    "relative_english_name_needs_review",
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
VOTER_ENGLISH_FIELDS = (
    "name_english",
    "english_first_name",
    "english_middle_name",
    "english_last_name",
    "english_name_needs_review",
)
RELATIVE_ENGLISH_FIELDS = (
    "relative_name_english",
    "relative_english_first_name",
    "relative_english_middle_name",
    "relative_english_last_name",
    "relative_english_name_needs_review",
)
ENGLISH_TRANSLITERATION_FIELDS = VOTER_ENGLISH_FIELDS + RELATIVE_ENGLISH_FIELDS


class ConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class NameParts:
    first: str
    middle: str
    last: str
    needs_review: bool


@dataclass(frozen=True)
class EnglishName:
    full: str
    first: str
    middle: str
    last: str
    needs_review: bool


DEVANAGARI_VOWELS = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u",
    "ऋ": "ri", "ॠ": "ri", "ऌ": "li", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
}
DEVANAGARI_MATRAS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "ॄ": "ri",
    "ॅ": "e", "े": "e", "ै": "ai", "ॉ": "o", "ो": "o", "ौ": "au", "ॆ": "e", "ॊ": "o",
}
DEVANAGARI_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "ळ": "l", "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
}
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
VIRAMA = "्"
NUKTA = "़"


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


def normalized_transliteration_source(text: str) -> str:
    """Normalize equivalent Devanagari spellings before dictionary lookup."""
    value = unicodedata.normalize("NFC", normalized(text))
    return value.replace("\u200c", "").replace("\u200d", "")


def split_name(full_name: str) -> NameParts:
    """Split a name without changing or discarding any of its words.

    One- and two-word names are inherently ambiguous. Three-word names use the
    common first/middle/last convention. Four-or-more-word, repeated-token, and
    abbreviated names are split deterministically but flagged for review.
    """
    words = normalized(full_name).split()
    if not words:
        return NameParts("", "", "", True)
    if len(words) == 1:
        parts = NameParts(words[0], "", "", True)
    elif len(words) == 2:
        parts = NameParts(words[0], "", words[1], True)
    else:
        parts = NameParts(words[0], " ".join(words[1:-1]), words[-1], len(words) > 3)

    normalized_tokens = [re.sub(r"[^\w\u0900-\u097f]", "", word).casefold() for word in words]
    repeated_token = len(set(normalized_tokens)) < len(normalized_tokens)
    abbreviated_token = any("." in word or "०" in word for word in words)
    if repeated_token or abbreviated_token:
        return NameParts(parts.first, parts.middle, parts.last, True)
    return parts


def load_transliteration_overrides(path: Path | None) -> dict[str, str]:
    """Load preferred spellings from a UTF-8 JSON object."""
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConversionError(f"Transliteration override file was not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConversionError(f"Invalid transliteration override JSON: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConversionError("Transliteration overrides must be a JSON object of Hindi-to-English strings.")
    overrides: dict[str, str] = {}
    for hindi, english in raw.items():
        if not isinstance(hindi, str) or not isinstance(english, str):
            raise ConversionError("Every transliteration override key and value must be a string.")
        key = normalized_transliteration_source(hindi)
        value = normalized(english)
        if not key or not value:
            raise ConversionError("Transliteration override keys and values cannot be blank.")
        if not re.search(r"[\u0900-\u097f]", key):
            raise ConversionError(f"Transliteration override key is not Devanagari: {key!r}")
        if not value.isascii() or not re.fullmatch(r"[A-Za-z0-9 .'-]+", value):
            raise ConversionError(
                f"Transliteration override value must use Roman letters: {value!r}"
            )
        if key in overrides and overrides[key] != value:
            raise ConversionError(f"Conflicting preferred spellings for {key!r}.")
        overrides[key] = value
    return overrides


def _fallback_transliterate_word(word: str) -> str:
    """Return a deterministic, common-spelling Roman form of a Hindi word.

    This intentionally uses the single-vowel spellings normally found in Indian
    names (Dangi, Sita, Poonam only when explicitly preferred) rather than a
    lossless Sanskrit romanization (Daangee, Seetaa). Preferred exceptions still
    belong in the override dictionary.
    """
    text = unicodedata.normalize("NFC", word).translate(DEVANAGARI_DIGITS)
    segments: list[tuple[str, bool]] = []
    index = 0
    while index < len(text):
        char = text[index]
        combined = char + NUKTA if index + 1 < len(text) and text[index + 1] == NUKTA else char
        consonant = DEVANAGARI_CONSONANTS.get(combined) or DEVANAGARI_CONSONANTS.get(char)
        if consonant:
            # व is pronounced between English v and w. Common Hindi-name
            # spellings use w after anusvara, in conjuncts such as स्व, श्व, and
            # द्व, and in the Rajasthani surname suffix -ावत (Kunwar, Swar,
            # Rajeshwari, Godawat), but v elsewhere (Vinod, Ravi).
            previous = text[index - 1] if index else ""
            rajasthani_awat_suffix = (
                char == "व"
                and previous == "ा"
                and text[index + 1:] == "त"
            )
            if char == "व" and (
                previous in {"ं", "ँ", VIRAMA} or rajasthani_awat_suffix
            ):
                consonant = "w"
            if combined != char:
                index += 1
            following = text[index + 1] if index + 1 < len(text) else ""
            if following == VIRAMA:
                segments.append((consonant, False))
                index += 2
                continue
            if following in DEVANAGARI_MATRAS:
                segments.append((consonant + DEVANAGARI_MATRAS[following], False))
                index += 2
                continue
            segments.append((consonant + "a", True))
        elif char in DEVANAGARI_VOWELS:
            segments.append((DEVANAGARI_VOWELS[char], False))
        elif char in {"ं", "ँ"}:
            # Anusvara assimilates to a following labial consonant. In common
            # English name spellings the other consonant classes use "n":
            # पंकज -> Pankaj, संजय -> Sanjay, संपत -> Sampat.
            following = text[index + 1] if index + 1 < len(text) else ""
            segments.append(("m" if following in "पफबभम" else "n", False))
        elif char == "ः":
            segments.append(("h", False))
        elif char == "ऽ":
            segments.append(("'", False))
        elif char in {".", "-", "'", "’", "(", ")"} or char.isascii() and char.isalnum():
            segments.append((char, False))
        elif char not in {NUKTA, VIRAMA} and not unicodedata.category(char).startswith("M"):
            segments.append((char, False))
        index += 1
    if segments and segments[-1][1] and segments[-1][0].endswith("a"):
        joined = "".join(value for value, _ in segments)
        # Hindi normally drops a final inherent schwa, but common conjunct-r
        # endings retain it: महेंद्र -> Mahendra, पवित्र -> Pavitra.
        if not joined.endswith(("dra", "tra")):
            segments[-1] = (segments[-1][0][:-1], False)
    result = "".join(value for value, _ in segments)
    return result[:1].upper() + result[1:] if result else ""


@lru_cache(maxsize=8192)
def _generated_transliterate_word(word: str) -> str:
    """Generate a stable Hindi-name spelling independent of package versions."""
    return _fallback_transliterate_word(word)


def transliterate_text(text: str, overrides: dict[str, str] | None = None) -> tuple[str, bool]:
    """Transliterate text word-by-word; generated spellings require review."""
    overrides = overrides or {}
    source = normalized_transliteration_source(text)
    if not source:
        return "", True
    if source in overrides:
        return overrides[source], False
    output: list[str] = []
    needs_review = False
    for word in source.split():
        if word in overrides:
            output.append(overrides[word])
        else:
            output.append(_generated_transliterate_word(word))
            needs_review = True
    return normalized(" ".join(output)), needs_review


def english_name(full_name: str, overrides: dict[str, str] | None = None) -> EnglishName:
    """Split the Hindi name and transliterate each component consistently."""
    overrides = overrides or {}
    source = normalized_transliteration_source(full_name)
    if source in overrides:
        full = overrides[source]
        english_parts = split_name(full)
        return EnglishName(
            full,
            english_parts.first,
            english_parts.middle,
            english_parts.last,
            False,
        )
    parts = split_name(full_name)
    first, first_review = transliterate_text(parts.first, overrides)
    middle, middle_review = transliterate_text(parts.middle, overrides) if parts.middle else ("", False)
    last, last_review = transliterate_text(parts.last, overrides) if parts.last else ("", False)
    full = normalized(" ".join([first, middle, last]))
    return EnglishName(full, first, middle, last, first_review or middle_review or last_review)


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
    municipality = re.search(
        r"^(?:WithPhoto[_ -]+)?(.+?)[-_ ]+Ward\s*No", stem, re.IGNORECASE
    )
    return {
        "ward_number": int(ward.group(1)) if ward else "",
        "part_number": int(part.group(1)) if part else "",
        "municipality": normalized(municipality.group(1).replace("_", " ")) if municipality else "",
    }


def grouped_ocr_lines(words: list[dict], *, max_top: int | None = None) -> list[list[dict]]:
    """Return OCR words grouped into visually ordered Tesseract lines."""
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for word in words:
        if max_top is not None and word["top"] > max_top:
            continue
        grouped[word["line"]].append(word)
    return sorted(
        (sorted(group, key=lambda word: word["left"]) for group in grouped.values()),
        key=lambda group: (min(word["top"] for word in group), group[0]["left"]),
    )


def ocr_line_text(words: list[dict]) -> str:
    return normalized(" ".join(word["text"] for word in words))


def value_after_ocr_colon(text: str) -> str:
    match = re.search(r"[:ः]\s*(.+)$", normalized(text))
    if not match:
        return ""
    return match.group(1).strip(" .,:;।-'\"")


def embedded_number_for_ocr_line(
    line: list[dict], embedded_items: list[dict], dpi: int
) -> int | None:
    """Use an OCR label to locate its exact number in the PDF text layer."""
    scale = dpi / 72
    line_x_pt = min(word["left"] for word in line) / scale
    centers = sorted(word["top"] + word["height"] / 2 for word in line)
    # OCR boxes use their visual center; PDF text coordinates use the baseline.
    line_y_pt = centers[len(centers) // 2] / scale + 6
    candidates: list[tuple[float, int]] = []
    for item in embedded_items:
        text = normalized(item["text"]).translate(DEVANAGARI_DIGITS)
        numbers = re.findall(r"\d+", text)
        if not numbers or abs(abs(item["y"]) - line_y_pt) > 10:
            continue
        score = abs(item["x"] - line_x_pt) + 3 * abs(abs(item["y"]) - line_y_pt)
        candidates.append((score, int(numbers[-1])))
    if candidates:
        return min(candidates)[1]

    ocr_numbers = re.findall(
        r"\d+", ocr_line_text(line).translate(DEVANAGARI_DIGITS)
    )
    return int(ocr_numbers[-1]) if ocr_numbers else None


def infer_document_metadata(
    header_words: list[dict],
    embedded_items: list[dict],
    dpi: int,
    transliteration_overrides: dict[str, str] | None = None,
) -> dict:
    """Infer administrative metadata from the PDF, independently of its name."""
    lines = grouped_ocr_lines(header_words, max_top=int(170 * dpi / 72))
    normalized_lines = [(line, ocr_line_text(line)) for line in lines]
    all_header_text = " ".join(text for _, text in normalized_lines)

    if re.search(r"ग्राम\s*पंचायत|ग्रामपंचायत|पंचायत\s+चुनाव", all_header_text):
        roll_type = "panchayat"
    elif re.search(r"नगर\s*निगम|नगरनिगम|नगर\s*परिषद|नगरपालिका", all_header_text):
        roll_type = "municipal"
    else:
        roll_type = "unknown"

    location_hindi = ""
    ward_number: int | str = ""
    part_number: int | str = ""
    for line, text in normalized_lines:
        if not location_hindi:
            is_panchayat_name = roll_type == "panchayat" and re.search(
                r"ग्राम\s*पंचायत|ग्रामपंचायत", text
            )
            is_municipal_name = roll_type == "municipal" and re.search(
                r"नगर\s*निगम|नगरनिगम|नगर\s*परिषद|नगरपरिषद|नगरपालिका", text
            ) and "नाम" in text
            if is_panchayat_name or is_municipal_name:
                location_hindi = value_after_ocr_colon(text)

        if "वार्ड" in text and re.search(r"संख्या|क्रमांक", text):
            ward_number = embedded_number_for_ocr_line(line, embedded_items, dpi) or ""
        elif "भाग" in text and "संख्या" in text:
            part_number = embedded_number_for_ocr_line(line, embedded_items, dpi) or ""

    location_english = ""
    if location_hindi:
        location_english, _ = transliterate_text(
            location_hindi, transliteration_overrides or {}
        )
    return {
        "roll_type": roll_type,
        "municipality_hindi": location_hindi,
        "municipality": location_english,
        "ward_number": ward_number,
        "part_number": part_number,
        "metadata_source": "pdf_header_ocr_and_text_layer",
    }


def filename_location_matches_document(filename_value: str, document_value: str) -> bool:
    """Allow a richer filename location only after the PDF confirms its identity."""
    document_key = re.sub(r"[^a-z]", "", document_value.casefold())
    filename_tokens = re.findall(r"[a-z]+", filename_value.casefold())
    if not document_key or not filename_tokens:
        return False
    return max(
        difflib.SequenceMatcher(None, document_key, token).ratio()
        for token in filename_tokens
    ) >= 0.78


def merge_detected_metadata(document: dict, filename: dict) -> dict:
    """Prefer PDF-derived metadata and use a verified filename only as enrichment."""
    merged = dict(document)
    filename_location = normalized(str(filename.get("municipality", "")))
    document_location = normalized(str(document.get("municipality", "")))
    if filename_location_matches_document(filename_location, document_location):
        merged["municipality"] = filename_location
        merged["metadata_source"] = "pdf_header_verified_filename_enrichment"
    elif not document_location and filename_location:
        merged["municipality"] = filename_location
        merged["metadata_source"] += "+filename_fallback"
    for field in ("ward_number", "part_number"):
        if merged.get(field, "") == "" and filename.get(field, "") != "":
            merged[field] = filename[field]
            merged["metadata_source"] += "+filename_fallback"
    return merged


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
        # Tesseract TSV is an unquoted, tab-delimited format. OCR output can
        # legitimately begin with a double quote (for example, a misread image
        # placeholder). Treating that quote as CSV syntax can merge several
        # physical TSV rows and silently discard words from later voter cells.
        for row in csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
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

    # Some supported rolls contain the literal text "Photo is Available" in
    # each voter cell. With Hindi-only OCR it becomes low-confidence garbage on
    # the same Tesseract line as the name. Keep the contiguous field segment at
    # the left of the cell and stop before a large horizontal gap. The gap is
    # DPI-scaled and remains wide enough to retain separated house numbers.
    max_field_gap = 90 * dpi / 300
    line_options: list[tuple[float, float, str]] = []
    for group in by_line.values():
        ordered = sorted((word for word, _ in group), key=lambda word: word["left"])
        field_segment = [ordered[0]]
        for word in ordered[1:]:
            previous = field_segment[-1]
            gap = word["left"] - (previous["left"] + previous["width"])
            if gap > max_field_gap:
                break
            field_segment.append(word)

        text = clean_ocr_field(" ".join(word["text"] for word in field_segment), field)
        if not text:
            continue
        center_ys = sorted(
            word["top"] + word["height"] / 2 for word in field_segment
        )
        distance = abs(center_ys[len(center_ys) // 2] - target_y)
        confidence = sum(max(0.0, word["conf"]) for word in field_segment) / len(
            field_segment
        )
        line_options.append((distance, -confidence, text))

    if not line_options:
        return "", 0.0

    # Tesseract occasionally divides one printed field into a label-only line
    # and a nearby value-only line. Discarding empty cleaned labels above lets
    # the value line win while the distance keeps normal layouts unchanged.
    _, negative_confidence, text = min(line_options, key=lambda option: option[:2])
    return text, round(-negative_confidence, 1)


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
) -> tuple[int, list[dict]]:
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

    # Sparse-text OCR reads table-contained header fields more reliably than
    # the dense page segmentation used for voter cards. Run it once on the
    # first record page so metadata never depends on a filename convention.
    header_page = min(pages)
    header_image = rendered_dir / f"page-{header_page:04d}.png"
    header_output_base = ocr_dir / f"header-page-{header_page:04d}"
    header_command = [tesseract, str(header_image), str(header_output_base), "-l", "hin"]
    if tessdata_dir:
        header_command.extend(["--tessdata-dir", str(tessdata_dir)])
    header_command.extend(["--psm", "11", "-c", "tessedit_create_tsv=1"])
    run(header_command)
    header_words = load_tsv(header_output_base.with_suffix(".tsv"))

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
    return header_page, header_words


def validate_records(
    records: list[Occurrence],
    summary: CoverSummary,
    extraction_errors: list[str],
    transliteration_overrides: dict[str, str] | None = None,
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
    name_split_review_count = sum(split_name(record.name).needs_review for record in records)
    relative_split_review_count = sum(
        split_name(record.relative_name).needs_review for record in records
    )
    english_name_review_count = sum(
        english_name(record.name, transliteration_overrides).needs_review for record in records
    )
    relative_english_name_review_count = sum(
        english_name(record.relative_name, transliteration_overrides).needs_review
        for record in records
    )
    if review_count:
        warnings.append(
            f"{review_count} rows have low OCR confidence and are marked needs_review=Yes."
        )
    if name_split_review_count or relative_split_review_count:
        warnings.append(
            f"Name splitting requires review for {name_split_review_count} voter names and "
            f"{relative_split_review_count} relative names."
        )
    if english_name_review_count or relative_english_name_review_count:
        warnings.append(
            f"English spelling requires review for {english_name_review_count} voter names and "
            f"{relative_english_name_review_count} relative names. Add preferred spellings to "
            "the transliteration override file."
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
            "name_split_needs_review": name_split_review_count,
            "relative_name_split_needs_review": relative_split_review_count,
            "english_name_needs_review": english_name_review_count,
            "relative_english_name_needs_review": relative_english_name_review_count,
        },
    }


def csv_row(
    record: Occurrence,
    metadata: dict,
    source: Path,
    transliteration_overrides: dict[str, str] | None = None,
) -> dict:
    code = record.deletion_code
    voter_name = split_name(record.name)
    relative_name = split_name(record.relative_name)
    voter_english = english_name(record.name, transliteration_overrides)
    relative_english = english_name(record.relative_name, transliteration_overrides)
    return {
        "serial_number": record.serial_number,
        "voter_id": record.voter_id,
        "name_hindi": record.name,
        "first_name": voter_name.first,
        "middle_name": voter_name.middle,
        "last_name": voter_name.last,
        "name_split_needs_review": "Yes" if voter_name.needs_review else "No",
        "name_english": voter_english.full,
        "english_first_name": voter_english.first,
        "english_middle_name": voter_english.middle,
        "english_last_name": voter_english.last,
        "english_name_needs_review": "Yes" if voter_english.needs_review else "No",
        "relative_name_hindi": record.relative_name,
        "relative_first_name": relative_name.first,
        "relative_middle_name": relative_name.middle,
        "relative_last_name": relative_name.last,
        "relative_name_split_needs_review": "Yes" if relative_name.needs_review else "No",
        "relative_name_english": relative_english.full,
        "relative_english_first_name": relative_english.first,
        "relative_english_middle_name": relative_english.middle,
        "relative_english_last_name": relative_english.last,
        "relative_english_name_needs_review": "Yes" if relative_english.needs_review else "No",
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
    transliteration_overrides: dict[str, str] | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(
            csv_row(record, metadata, source, transliteration_overrides) for record in records
        )
    os.replace(temporary, output)
    audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_written_csv(
    output: Path,
    records: list[Occurrence],
    transliteration_overrides: dict[str, str] | None = None,
) -> None:
    with output.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    if reader.fieldnames != CSV_FIELDS:
        raise ConversionError("CSV read-back headers do not match the expected schema.")
    if len(rows) != len(records):
        raise ConversionError(f"CSV read-back row count mismatch: {len(rows)} != {len(records)}")
    if [int(row["serial_number"]) for row in rows] != [record.serial_number for record in records]:
        raise ConversionError("CSV read-back serials do not match the extracted records.")
    if any(len(row) != len(CSV_FIELDS) for row in rows):
        raise ConversionError("CSV read-back found a malformed row or column mismatch.")
    for row, record in zip(rows, records):
        reconstructed_name = normalized(
            " ".join([row["first_name"], row["middle_name"], row["last_name"]])
        )
        reconstructed_relative = normalized(
            " ".join(
                [
                    row["relative_first_name"],
                    row["relative_middle_name"],
                    row["relative_last_name"],
                ]
            )
        )
        if reconstructed_name != normalized(row["name_hindi"]):
            raise ConversionError(
                f"Name components do not reconstruct serial {row['serial_number']}'s full name."
            )
        if reconstructed_relative != normalized(row["relative_name_hindi"]):
            raise ConversionError(
                f"Relative-name components do not reconstruct serial {row['serial_number']}'s full name."
            )
        if row["name_split_needs_review"] not in {"Yes", "No"}:
            raise ConversionError("Invalid voter-name split review flag in CSV read-back.")
        if row["relative_name_split_needs_review"] not in {"Yes", "No"}:
            raise ConversionError("Invalid relative-name split review flag in CSV read-back.")
        reconstructed_english_name = normalized(
            " ".join(
                [row["english_first_name"], row["english_middle_name"], row["english_last_name"]]
            )
        )
        reconstructed_relative_english = normalized(
            " ".join(
                [
                    row["relative_english_first_name"],
                    row["relative_english_middle_name"],
                    row["relative_english_last_name"],
                ]
            )
        )
        if reconstructed_english_name != normalized(row["name_english"]):
            raise ConversionError(
                f"English name components do not reconstruct serial {row['serial_number']}'s full name."
            )
        if reconstructed_relative_english != normalized(row["relative_name_english"]):
            raise ConversionError(
                f"English relative-name components do not reconstruct serial {row['serial_number']}'s full name."
            )
        if row["english_name_needs_review"] not in {"Yes", "No"}:
            raise ConversionError("Invalid English voter-name review flag in CSV read-back.")
        if row["relative_english_name_needs_review"] not in {"Yes", "No"}:
            raise ConversionError("Invalid English relative-name review flag in CSV read-back.")
        expected_voter_english = english_name(record.name, transliteration_overrides)
        expected_relative_english = english_name(
            record.relative_name, transliteration_overrides
        )
        actual_voter_english = (
            row["name_english"],
            row["english_first_name"],
            row["english_middle_name"],
            row["english_last_name"],
            row["english_name_needs_review"],
        )
        actual_relative_english = (
            row["relative_name_english"],
            row["relative_english_first_name"],
            row["relative_english_middle_name"],
            row["relative_english_last_name"],
            row["relative_english_name_needs_review"],
        )
        if actual_voter_english != (
            expected_voter_english.full,
            expected_voter_english.first,
            expected_voter_english.middle,
            expected_voter_english.last,
            "Yes" if expected_voter_english.needs_review else "No",
        ):
            raise ConversionError(
                f"English voter-name values changed during CSV write at serial {row['serial_number']}."
            )
        if actual_relative_english != (
            expected_relative_english.full,
            expected_relative_english.first,
            expected_relative_english.middle,
            expected_relative_english.last,
            "Yes" if expected_relative_english.needs_review else "No",
        ):
            raise ConversionError(
                f"English relative-name values changed during CSV write at serial {row['serial_number']}."
            )


def _set_english_fields(row: dict[str, str], result: EnglishName, *, relative: bool) -> None:
    prefix = "relative_" if relative else ""
    row[f"{prefix}name_english"] = result.full
    row[f"{prefix}english_first_name"] = result.first
    row[f"{prefix}english_middle_name"] = result.middle
    row[f"{prefix}english_last_name"] = result.last
    row[f"{prefix}english_name_needs_review"] = "Yes" if result.needs_review else "No"


def retransliterate_csv(
    source: Path,
    output_dir: Path,
    transliteration_overrides: dict[str, str] | None = None,
    *,
    force: bool = False,
    overwrite: bool = False,
) -> tuple[Path, dict]:
    """Refresh existing English-name columns without rerunning PDF extraction or OCR."""
    output = output_dir / source.name
    audit_output = output_dir / f"{source.stem}.transliteration.validation.json"
    if output.exists() and not overwrite:
        raise ConversionError(f"Output already exists (choose overwrite when prompted): {output}")

    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = list(reader)
    if not fieldnames:
        raise ConversionError(f"CSV has no header row: {source}")
    required = {"name_hindi", "relative_name_hindi", *ENGLISH_TRANSLITERATION_FIELDS}
    missing = [field for field in CSV_FIELDS if field in required and field not in fieldnames]
    if missing:
        raise ConversionError(
            f"CSV is missing required transliteration columns: {', '.join(missing)}"
        )
    if any(None in row for row in rows):
        raise ConversionError("CSV contains a row with more values than its header.")

    original_rows = [dict(row) for row in rows]
    voter_updates = 0
    relative_updates = 0
    for row_number, row in enumerate(rows, 2):
        voter_hindi = normalized(row["name_hindi"])
        relative_hindi = normalized(row["relative_name_hindi"])
        if not voter_hindi or not relative_hindi:
            raise ConversionError(f"Blank Hindi voter or relative name at CSV row {row_number}.")

        update_voter = (
            force
            or not normalized(row["name_english"])
            or row["english_name_needs_review"].strip().casefold() == "yes"
        )
        update_relative = (
            force
            or not normalized(row["relative_name_english"])
            or row["relative_english_name_needs_review"].strip().casefold() == "yes"
        )
        if update_voter:
            _set_english_fields(
                row,
                english_name(voter_hindi, transliteration_overrides),
                relative=False,
            )
            voter_updates += 1
        if update_relative:
            _set_english_fields(
                row,
                english_name(relative_hindi, transliteration_overrides),
                relative=True,
            )
            relative_updates += 1

    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    with temporary.open(encoding="utf-8-sig", newline="") as handle:
        written_reader = csv.DictReader(handle)
        written_rows = list(written_reader)
    if written_reader.fieldnames != fieldnames or written_rows != rows:
        raise ConversionError("CSV changed unexpectedly during transliteration write-back.")
    untouched_fields = [field for field in fieldnames if field not in ENGLISH_TRANSLITERATION_FIELDS]
    for before, after in zip(original_rows, written_rows):
        if any(before[field] != after[field] for field in untouched_fields):
            raise ConversionError("A non-English CSV value changed during retransliteration.")
    os.replace(temporary, output)

    audit = {
        "result": "PASS",
        "operation": "CSV retransliteration only; PDF extraction and OCR were skipped",
        "source_csv": str(source.resolve()),
        "output_csv": str(output.resolve()),
        "row_count": len(rows),
        "voter_names_updated": voter_updates,
        "relative_names_updated": relative_updates,
        "force_retransliterate": force,
        "preserved_columns": untouched_fields,
    }
    audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, audit


def convert_one(pdf: Path, args, tools: dict[str, str]) -> tuple[Path, dict]:
    output = args.output_dir / f"{pdf.stem}.csv"
    audit_output = args.output_dir / f"{pdf.stem}.validation.json"
    if output.exists() and not args.overwrite:
        raise ConversionError(f"Output already exists (use --overwrite): {output}")
    transliteration_overrides = load_transliteration_overrides(args.transliteration_overrides)
    reader = PdfReader(pdf)
    if reader.is_encrypted:
        raise ConversionError("Encrypted PDFs are not supported.")
    occurrences, summary, roll_year = extract_occurrences(reader)
    records, extraction_errors = choose_primary_records(occurrences, summary)
    filename_metadata = infer_filename_metadata(pdf)

    with tempfile.TemporaryDirectory(prefix="voter_pdf_") as temp_name:
        header_page, header_words = ocr_records(
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
        document_metadata = infer_document_metadata(
            header_words,
            page_items(reader.pages[header_page - 1]),
            args.dpi,
            transliteration_overrides,
        )
    metadata = merge_detected_metadata(document_metadata, filename_metadata)
    metadata["roll_year"] = args.roll_year or roll_year or ""
    if args.municipality:
        metadata["municipality"] = args.municipality
        metadata["metadata_source"] = "explicit_cli_override"
    if args.ward_number is not None:
        metadata["ward_number"] = args.ward_number
        metadata["metadata_source"] = "explicit_cli_override"
    if args.part_number is not None:
        metadata["part_number"] = args.part_number
        metadata["metadata_source"] = "explicit_cli_override"
    audit = validate_records(records, summary, extraction_errors, transliteration_overrides)
    if metadata["roll_type"] == "unknown":
        audit["warnings"].append(
            "The roll type could not be identified from the PDF header; voter-card "
            "structure and totals still validated."
        )
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
            "transliteration": {
                "method": (
                    "preferred-spelling overrides, then deterministic Hindi-name rules"
                ),
                "override_file": str(args.transliteration_overrides.resolve())
                if args.transliteration_overrides
                else None,
                "override_count": len(transliteration_overrides),
                "legal_spelling_authoritative": False,
            },
        }
    )
    if audit["errors"]:
        audit_output.parent.mkdir(parents=True, exist_ok=True)
        audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ConversionError("Validation failed; CSV was not written. See " + str(audit_output))
    write_outputs(
        output, audit_output, records, metadata, pdf, audit, transliteration_overrides
    )
    verify_written_csv(output, records, transliteration_overrides)
    total_review = sum(
        audit["derived_summary"][key]
        for key in (
            "needs_review",
            "name_split_needs_review",
            "relative_name_split_needs_review",
            "english_name_needs_review",
            "relative_english_name_needs_review",
        )
    )
    if args.fail_on_review and total_review:
        raise ConversionError(
            "CSV was written, but OCR, name-split, or English-spelling review is required; "
            "see the validation report."
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


def collect_csvs(inputs: list[Path], recursive: bool) -> list[Path]:
    csvs: list[Path] = []
    for value in inputs:
        path = value.expanduser()
        if path.is_file() and path.suffix.lower() == ".csv":
            csvs.append(path.resolve())
        elif path.is_dir():
            candidates = path.rglob("*") if recursive else path.iterdir()
            csvs.extend(item.resolve() for item in candidates if item.is_file() and item.suffix.lower() == ".csv")
        else:
            raise ConversionError(f"Input is not a CSV file or directory: {value}")
    unique = sorted(set(csvs))
    if not unique:
        raise ConversionError("No CSV files were found.")
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
    print("│        Rajasthan Voter Data Converter       │")
    print("└──────────────────────────────────────────────┘")
    print("Hindi OCR • English transliteration • CSV refresh • Batch mode\n")

    print("What would you like to do?")
    print("  1. Extract voter PDFs and create CSV files")
    print("  2. Update English transliteration in existing CSV files")
    while True:
        mode = ask("Choose 1 or 2", "1")
        if mode in {"1", "2"}:
            break
        print("  Please enter 1 or 2.\n")

    is_csv_mode = mode == "2"
    file_label = "CSV" if is_csv_mode else "PDF"
    while True:
        folder_text = ask(f"Folder containing voter {file_label} files", str(Path.cwd()))
        folder = Path(folder_text).expanduser().resolve()
        if not folder.is_dir():
            print("  That folder does not exist. Please try again.\n")
            continue
        recursive = ask(f"Include {file_label} files in subfolders? (y/N)", "n").lower().startswith("y")
        files = sorted(
            item
            for item in (folder.rglob("*") if recursive else folder.iterdir())
            if item.is_file() and item.suffix.lower() == f".{file_label.lower()}"
        )
        if not files:
            print(f"  No {file_label} files were found there. Please try again.\n")
            continue
        break

    print(f"\nFound {len(files)} {file_label} file(s):")
    for index, item in enumerate(files, 1):
        label = str(item.relative_to(folder)) if recursive else item.name
        print(f"  {index:>3}. {label}")
    while True:
        try:
            selection = parse_selection(
                ask("Select files (all, 1,3, 2-5)", "all"), len(files)
            )
            break
        except (ValueError, TypeError):
            print("  Invalid selection. Use 'all', a number, commas, or a range.\n")

    chosen = [files[index] for index in selection]
    if is_csv_mode:
        output_dir = Path(
            ask("Output folder", str(folder / "retransliterated_csv"))
        ).expanduser().resolve()
        force = ask(
            "Regenerate all English names, including reviewed ones? (y/N)", "n"
        ).lower().startswith("y")
        overwrite = ask("Overwrite existing output files? (y/N)", "n").lower().startswith("y")
        print("\nReady:")
        print(f"  CSV files:   {len(chosen)}")
        print(f"  Output:      {output_dir}")
        print("  PDF/OCR:     skipped")
        print(f"  Update mode: {'all English names' if force else 'blank or review-needed names only'}")
        if not ask("Start CSV transliteration update? (Y/n)", "y").lower().startswith("y"):
            raise SystemExit("Cancelled.")
        argv = [str(item) for item in chosen] + [
            "--retransliterate-csv",
            "--output-dir",
            str(output_dir),
        ]
        if force:
            argv.append("--force-retransliterate")
        if overwrite:
            argv.append("--overwrite")
        print()
        return argv

    output_dir = Path(ask("Output folder", str(folder / "voter_csv"))).expanduser().resolve()
    fail_on_review = ask(
        "Treat any OCR, name-split, or English-spelling review as a failed run? (y/N)", "n"
    ).lower().startswith("y")
    jobs = ask("Parallel OCR jobs", str(min(4, os.cpu_count() or 1)))
    overwrite = ask("Overwrite existing CSV files? (y/N)", "n").lower().startswith("y")

    print("\nReady:")
    print(f"  PDFs:        {len(chosen)}")
    print(f"  Output:      {output_dir}")
    print(f"  OCR jobs:    {jobs}")
    print(f"  Review mode: {'fail when review is needed' if fail_on_review else 'flag rows in CSV'}")
    if not ask("Start conversion? (Y/n)", "y").lower().startswith("y"):
        raise SystemExit("Cancelled.")
    argv = [str(item) for item in chosen] + ["--output-dir", str(output_dir), "--jobs", jobs]
    if fail_on_review:
        argv.append("--fail-on-review")
    if overwrite:
        argv.append("--overwrite")
    print()
    return argv


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description=(
            "Convert Rajasthan municipal voter-list PDFs or refresh transliteration "
            "in existing converter CSV files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Dependencies:
  Python packages: see requirements-voter-converter.txt
  Commands: Ghostscript (gs), Tesseract OCR, and the Tesseract Hindi model (hin)

macOS with Homebrew:
  brew install uv ghostscript tesseract tesseract-lang
  ./setup_voter_converter.sh

Ubuntu/Debian:
  sudo apt-get install ghostscript tesseract-ocr tesseract-ocr-hin
  Install uv, then run ./setup_voter_converter.sh

Each successful PDF produces a .csv file and a .validation.json audit report.
CSV retransliteration mode skips PDF extraction and OCR and updates only the
existing English-name columns.
Rows with uncertain Hindi OCR are retained and marked needs_review=Yes.
English spellings are transliterations, not legal translations. Unknown spellings
are retained and marked english_name_needs_review=Yes. Preferred spellings can be
added to transliteration_overrides.json.
Use --fail-on-review when a nonzero exit status is required for those rows.
""",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="PDF/CSV file(s), or directories containing the selected file type",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/voter_csv"))
    parser.add_argument("--recursive", action="store_true", help="Search input directories recursively")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--retransliterate-csv",
        action="store_true",
        help="Update English transliteration in compatible CSV files without OCR",
    )
    parser.add_argument(
        "--force-retransliterate",
        action="store_true",
        help="In CSV mode, regenerate reviewed English names as well",
    )
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--tessdata-dir", type=Path)
    parser.add_argument("--name-confidence", type=float, default=75.0)
    parser.add_argument("--relative-confidence", type=float, default=70.0)
    parser.add_argument("--fail-on-review", action="store_true")
    parser.add_argument(
        "--transliteration-overrides",
        type=Path,
        help="UTF-8 JSON object containing preferred Hindi-to-English spellings",
    )
    parser.add_argument("--municipality", help="Override municipality metadata")
    parser.add_argument("--ward-number", type=int, help="Override ward metadata")
    parser.add_argument("--part-number", type=int, help="Override part metadata")
    parser.add_argument("--roll-year", type=int, help="Override roll-year metadata")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if not 200 <= args.dpi <= 600:
        parser.error("--dpi must be between 200 and 600")
    if args.force_retransliterate and not args.retransliterate_csv:
        parser.error("--force-retransliterate requires --retransliterate-csv")
    if args.tessdata_dir:
        args.tessdata_dir = args.tessdata_dir.expanduser().resolve()
    else:
        bundled_tessdata = Path(__file__).resolve().with_name("tessdata")
        if (bundled_tessdata / "hin.traineddata").is_file():
            args.tessdata_dir = bundled_tessdata
    if args.transliteration_overrides:
        args.transliteration_overrides = args.transliteration_overrides.expanduser().resolve()
    else:
        bundled_overrides = Path(__file__).resolve().with_name("transliteration_overrides.json")
        args.transliteration_overrides = bundled_overrides if bundled_overrides.is_file() else None
    args.output_dir = args.output_dir.expanduser().resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) == 1:
        argv = interactive_argv()
    args = parse_args(argv)
    if args.retransliterate_csv:
        try:
            csvs = collect_csvs(args.inputs, args.recursive)
            transliteration_overrides = load_transliteration_overrides(
                args.transliteration_overrides
            )
        except ConversionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

        failures = 0
        for index, source in enumerate(csvs, 1):
            print(f"[{index}/{len(csvs)}] {source.name}")
            try:
                output, audit = retransliterate_csv(
                    source,
                    args.output_dir,
                    transliteration_overrides,
                    force=args.force_retransliterate,
                    overwrite=args.overwrite,
                )
                print(
                    f"  PASS: {output} | rows={audit['row_count']} "
                    f"voter_names_updated={audit['voter_names_updated']} "
                    f"relative_names_updated={audit['relative_names_updated']}"
                )
            except (ConversionError, OSError, ValueError) as exc:
                failures += 1
                print(f"  FAILED: {exc}", file=sys.stderr)
        return 1 if failures else 0

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
            review_total = sum(
                summary[key]
                for key in (
                    "needs_review",
                    "name_split_needs_review",
                    "relative_name_split_needs_review",
                    "english_name_needs_review",
                    "relative_english_name_needs_review",
                )
            )
            print(
                f"  PASS: {output} | rows={summary['record_count']} "
                f"active={summary['active_count']} deleted={summary['deleted_count']} "
                f"review_flags={review_total}"
            )
        except (ConversionError, OSError, ValueError) as exc:
            failures += 1
            print(f"  FAILED: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
