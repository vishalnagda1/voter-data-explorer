# Voter Data Explorer

A private-by-design, browser-based dashboard for searching, filtering, analyzing, printing, and exporting Rajasthan municipal voter-list CSV files.

Live dashboard: **https://voter.vishalnagda.dev**

## Privacy

The dashboard runs entirely in the browser. Selected CSV files are not uploaded to a server, and voter PDFs, generated CSVs, validation reports, and local environments are excluded from this repository.

Browser preferences such as filters, column order, sorting, and print settings are saved locally. Voter records themselves are not saved in browser storage.

## Dashboard capabilities

- Load and combine one or multiple compatible voter CSV files.
- Search every field and filter by status, gender, part, relationship, source file, age, review status, and Hindi or English surname.
- Build additional rules against any available CSV field.
- View live demographics, age, status, part, surname, relationship, duplicate-ID, and data-quality analytics.
- Select, reorder, and persist screen and print columns.
- Export filtered results as CSV.
- Print filtered or selected rows on A4 paper or save them as PDF.

Surname analytics reports names exactly as recorded and must not be treated as verified caste identification.

## Local use

Open `voter_dashboard.html` directly in a modern browser and select the generated CSV files. No server or Python environment is required for the dashboard.

## PDF converter

The repository also includes `voter_pdf_to_csv.py`, an offline converter for the supported Rajasthan municipal voter-roll PDF layout. On macOS, run `setup_voter_converter.sh` once and then open `run_voter_converter.command` for the interactive workflow.

Every successful conversion produces a UTF-8 CSV and a validation report. English names are transliterations and review flags should be checked before official use.
