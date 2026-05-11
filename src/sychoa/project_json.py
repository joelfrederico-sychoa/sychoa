from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .models import ADDRESS_TYPES, AddressRecord, page_map_from_json


APP_SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_for_json(path: Path, base_dir: Path | None = None) -> str:
    base_dir = base_dir or Path.cwd()
    try:
        return str(path.resolve().relative_to(base_dir.resolve()))
    except ValueError:
        return str(path.resolve())


def resolve_json_path(raw_path: str, project_path: Path | None) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    if project_path:
        candidate = (project_path.parent / path).resolve()
        if candidate.exists():
            return candidate
    return (Path.cwd() / path).resolve()


def project_to_json(
    pdf_sources,
    addresses: list[AddressRecord],
    created_at: str,
    project_path: Path | None = None,
    extra_pages: dict[str, list[int]] | None = None,
    type_extra_pages: dict[str, dict[str, list[int]]] | None = None,
) -> dict:
    pdfs = [pdf_source_to_json(pdf_source, project_path) for pdf_source in pdf_sources]
    return {
        "schema_version": APP_SCHEMA_VERSION,
        "created_at": created_at,
        "updated_at": now_iso(),
        "main_pdfs": pdfs,
        "main_pdf": pdfs[0] if pdfs else None,
        "extra_pages": sanitize_extra_pages(extra_pages or {}),
        "type_extra_pages": sanitize_type_extra_pages(type_extra_pages or {}),
        "addresses": [address.to_json() for address in addresses],
    }


def pdf_source_to_json(pdf_source, project_path: Path | None = None) -> dict:
    pages = [
        {"index": index, "width": round(page.rect.width, 3), "height": round(page.rect.height, 3)}
        for index, page in enumerate(pdf_source.doc)
    ]
    return {
        "id": pdf_source.id,
        "path": path_for_json(pdf_source.path, project_path.parent if project_path else None),
        "name": pdf_source.path.name,
        "sha256": file_sha256(pdf_source.path),
        "page_count": pdf_source.doc.page_count,
        "pages": pages,
    }


def read_project_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_project_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def pdf_records_from_json(data: dict) -> list[dict]:
    raw_pdfs = data.get("main_pdfs")
    if isinstance(raw_pdfs, list) and raw_pdfs:
        return [
            {
                "id": str(item.get("id") or f"pdf-{index + 1}"),
                "path": str(item["path"]),
            }
            for index, item in enumerate(raw_pdfs)
            if item.get("path")
        ]
    main_pdf = data.get("main_pdf")
    if isinstance(main_pdf, dict) and main_pdf.get("path"):
        return [{"id": str(main_pdf.get("id") or "pdf-1"), "path": str(main_pdf["path"])}]
    return []


def default_pdf_id_from_json(data: dict) -> str:
    records = pdf_records_from_json(data)
    return records[0]["id"] if records else ""


def addresses_from_json(data: dict) -> list[AddressRecord]:
    default_pdf_id = default_pdf_id_from_json(data)
    return [AddressRecord.from_json(item, default_pdf_id) for item in data.get("addresses", [])]


def extra_pages_from_json(data: dict) -> dict[str, list[int]]:
    default_pdf_id = default_pdf_id_from_json(data)
    raw_pages = data.get("extra_pages")
    if raw_pages is not None:
        return sanitize_extra_pages(raw_pages, default_pdf_id)
    merged_pages: dict[str, list[int]] = {}
    for item in data.get("addresses", []):
        legacy_pages = page_map_from_json(item.get("extra_pages", []), default_pdf_id)
        for pdf_id, pages in legacy_pages.items():
            merged_pages.setdefault(pdf_id, []).extend(pages)
    return sanitize_extra_pages(merged_pages)


def type_extra_pages_from_json(data: dict) -> dict[str, dict[str, list[int]]]:
    return sanitize_type_extra_pages(data.get("type_extra_pages", {}), default_pdf_id_from_json(data))


def sanitize_extra_pages(raw_pages: dict | list | None, default_pdf_id: str = "") -> dict[str, list[int]]:
    return page_map_from_json(raw_pages or {}, default_pdf_id)


def default_type_extra_pages() -> dict[str, dict[str, list[int]]]:
    return {address_type: {} for address_type in ADDRESS_TYPES}


def sanitize_type_extra_pages(raw_pages: dict | None, default_pdf_id: str = "") -> dict[str, dict[str, list[int]]]:
    pages = default_type_extra_pages()
    if not isinstance(raw_pages, dict):
        return pages
    for address_type in ADDRESS_TYPES:
        pages[address_type] = page_map_from_json(raw_pages.get(address_type, {}), default_pdf_id)
    return pages
