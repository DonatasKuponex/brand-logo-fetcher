name: Fetch Logos

on:
  workflow_dispatch:
    inputs:
      csv_path:
        description: "Path to CSV in repo (default: brands.csv)"
        required: false
        default: "brands.csv"

jobs:
  fetch-logos:
    # Pin to 22.04 to avoid apt package-name changes on 24.04
    runs-on: ubuntu-22.04

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Show workspace & confirm CSV
        run: |
          pwd
          ls -la
          test -f "${{ github.event.inputs.csv_path || 'brands.csv' }}" && echo "CSV OK" || (echo "CSV MISSING" && exit 1)
          wc -l ${{ github.event.inputs.csv_path || 'brands.csv' }}

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install system deps for CairoSVG
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends \
            libcairo2 \
            libpango-1.0-0 \
            libgdk-pixbuf-2.0-0 \
            libffi8 \
            libjpeg-turbo8 \
            libpng16-16 \
            zlib1g

      - name: Install Python deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run fetcher
        env:
          CSV_PATH: ${{ github.event.inputs.csv_path }}
        run: |
          echo "Using CSV: ${CSV_PATH:-brands.csv}"
          python fetch_logos.py

      - name: Upload logos artifact
        uses: actions/upload-artifact@v4
        with:
          name: logos
          path: logos
          if-no-files-found: error
