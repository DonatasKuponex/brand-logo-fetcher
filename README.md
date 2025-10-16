# Brand Logo Fetcher (LT/PL/EU)

Fetches brand logos using a robust chain (Wikidata → Clearbit → Brand resources → Wikimedia → Simple Icons).
Outputs SVG (if available) and normalized 1024×1024 PNG with transparent background.

## Local
```bash
pip install -r requirements.txt
python fetch_logos.py
```

## Docker
```bash
docker build -t logo-fetcher .
docker run --rm -v $PWD/logos:/app/logos logo-fetcher
```

## GitHub Actions
Go to **Actions → Fetch Logos → Run workflow**, then download the **logos** artifact.
