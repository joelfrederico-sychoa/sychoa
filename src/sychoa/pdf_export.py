from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import fitz

from .models import AddressRecord, BoxRecord, normalize_address_type


MIN_BOX_SIZE = 3.0
HIGHLIGHT_WIDTH = 8.0
ExportProgressCallback = Callable[[int, int, str], None]


def safe_filename(label: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", label).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or fallback


def export_box_rect(box: BoxRecord, page: fitz.Page) -> fitz.Rect:
    rect = fitz.Rect(box.rect.left(), box.rect.top(), box.rect.right(), box.rect.bottom())
    if page.rotation:
        # Boxes are created in the rotated on-screen view, but PyMuPDF draws onto
        # the page in its native unrotated coordinate space.
        top_left = fitz.Point(rect.x0, rect.y0) * page.derotation_matrix
        bottom_right = fitz.Point(rect.x1, rect.y1) * page.derotation_matrix
        rect = fitz.Rect(top_left, bottom_right).normalize()
    return rect & fitz.Rect(page.cropbox)


def export_address_pdfs(
    pdf_sources,
    addresses: list[AddressRecord],
    out_dir: Path,
    extra_pages: dict[str, list[int]] | None = None,
    type_extra_pages: dict[str, dict[str, list[int]]] | None = None,
    progress_callback: ExportProgressCallback | None = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []
    used_names: set[str] = set()
    pdf_by_id = {pdf_source.id: pdf_source for pdf_source in pdf_sources}
    export_plans: list[tuple[int, AddressRecord, list[tuple[str, int]], Path]] = []
    for address_index, address in enumerate(addresses, start=1):
        output_pages = address_output_pages(address, pdf_sources, extra_pages or {}, type_extra_pages or {})
        if not output_pages:
            continue
        base = safe_filename(address.label, f"address-{address_index}")
        name = base
        suffix = 2
        while name.lower() in used_names:
            name = f"{base}-{suffix}"
            suffix += 1
        used_names.add(name.lower())
        export_plans.append((address_index, address, output_pages, out_dir / f"{name}.pdf"))

    total_pages = sum(len(output_pages) for _, _, output_pages, _ in export_plans)
    if progress_callback:
        progress_callback(0, total_pages, f"Preparing export for {len(export_plans)} address(es)")

    completed_pages = 0
    for _address_index, address, output_pages, out_path in export_plans:
        out = fitz.open()
        try:
            boxes_by_pdf_page: dict[tuple[str, int], list[BoxRecord]] = {}
            for box in address.boxes:
                pdf_source = pdf_by_id.get(box.pdf_id)
                if pdf_source and 0 <= box.page < pdf_source.doc.page_count:
                    boxes_by_pdf_page.setdefault((box.pdf_id, box.page), []).append(box)

            for page_number, (pdf_id, page_index) in enumerate(output_pages, start=1):
                pdf_source = pdf_by_id[pdf_id]
                out.insert_pdf(pdf_source.doc, from_page=page_index, to_page=page_index)
                target_page = out[-1]
                rects: list[fitz.Rect] = []
                for box in sorted(boxes_by_pdf_page.get((pdf_id, page_index), []), key=lambda item: (item.y0, item.x0)):
                    rect = export_box_rect(box, target_page)
                    if rect.is_empty or rect.width < MIN_BOX_SIZE or rect.height < MIN_BOX_SIZE:
                        continue
                    rects.append(rect)

                if rects:
                    shape = target_page.new_shape()
                    for rect in rects:
                        shape.draw_rect(rect)
                    shape.finish(color=(1, 0, 0), width=HIGHLIGHT_WIDTH)
                    shape.commit()
                completed_pages += 1
                if progress_callback:
                    progress_callback(
                        completed_pages,
                        total_pages,
                        f"Exporting {address.label}: page {page_number} of {len(output_pages)}",
                    )
            if out.page_count:
                out.save(out_path)
                exported.append(out_path)
        finally:
            out.close()
    if progress_callback:
        progress_callback(total_pages, total_pages, f"Exported {len(exported)} PDF(s)")
    return exported


def address_output_pages(
    address: AddressRecord,
    pdf_sources,
    extra_pages: dict[str, list[int]],
    type_extra_pages: dict[str, dict[str, list[int]]],
) -> list[tuple[str, int]]:
    pages_by_pdf: dict[str, set[int]] = {pdf_source.id: set() for pdf_source in pdf_sources}
    page_counts = {pdf_source.id: pdf_source.doc.page_count for pdf_source in pdf_sources}

    for box in address.boxes:
        if box.pdf_id in page_counts and 0 <= box.page < page_counts[box.pdf_id]:
            pages_by_pdf[box.pdf_id].add(box.page)

    for pdf_id, pages in extra_pages.items():
        if pdf_id not in page_counts:
            continue
        pages_by_pdf[pdf_id].update(page for page in pages if 0 <= page < page_counts[pdf_id])

    address_type = normalize_address_type(address.address_type)
    for pdf_id, pages in type_extra_pages.get(address_type, {}).items():
        if pdf_id not in page_counts:
            continue
        pages_by_pdf[pdf_id].update(page for page in pages if 0 <= page < page_counts[pdf_id])

    ordered: list[tuple[str, int]] = []
    for pdf_source in pdf_sources:
        ordered.extend((pdf_source.id, page) for page in sorted(pages_by_pdf[pdf_source.id]))
    return ordered
