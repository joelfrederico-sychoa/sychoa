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

The app opens a main PDF, lets you create a separate set of thick red highlight
boxes for each address, saves the PDF and box data as JSON, reloads that JSON
later, and exports one marked-up PDF per address.
