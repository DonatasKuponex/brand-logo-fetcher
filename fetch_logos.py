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
    runs-on: ubuntu-latest   # galima palikti latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Show workspace (debug)
        run: |
          ls -la
          test -f "${{ github.event.inputs.csv_path || 'brands.csv' }}" || (echo "CSV not found"; exit 1)
          wc -l "${{ github.event.inputs.csv_path || 'brands.csv' }}"

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      # JOKIO apt-get — nebereikia sisteminių paketų
      - name: Install Python deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run fetcher
        env:
          CSV_PATH: ${{ github.event.inputs.csv_path }}
          # BRANDFETCH_KEY: ${{ secrets.BRANDFETCH_KEY }}  # jei naudosi Brandfetch
        run: |
          echo "Using CSV: ${CSV_PATH:-brands.csv}"
          python fetch_logos.py

      - name: Upload logos artifact
        uses: actions/upload-artifact@v4
        with:
          name: logos
          path: logos
          if-no-files-found: error
