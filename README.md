# Santa Ynez Canyon HOA

A common location for public files.

## PDF address box app

Install the local Qt app:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

Run it:

```bash
sychoa
```

The app opens one or more main PDFs, lets you create a separate set of thick red
highlight boxes for each address, saves the PDF and box data as JSON, reloads
that JSON later, and exports one marked-up PDF per address.

Global extra pages are applied to every address, and type-specific extra pages
are applied to every address of that type.

Use **Replace PDF** when a source PDF has been revised. The app keeps the
existing PDF id, matches old pages to new pages by extracted page text, scales
box coordinates to the new page size, remaps global/type page references, and
reports low-confidence page matches for review.

Use **Undo** or `Cmd+Z`/`Ctrl+Z` to reverse project edits such as box changes,
address changes, extra page changes, added/replaced PDFs, and remapped boxes.
