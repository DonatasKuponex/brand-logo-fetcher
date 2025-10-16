import os, csv, json, io, re, time
from pathlib import Path
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

# -------- Settings --------
OUT = Path("logos")
(OUT / "svg").mkdir(parents=True, exist_ok=True)
(OUT / "png").mkdir(parents=True, exist_ok=True)

WIKIDATA_SEARCH = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY  = "https://www.wikidata.org/wiki/Special:EntityData/{}.json"
HEADERS = {"User-Agent": "logo-fetcher/1.1 (+contact: you@example.com)"}
TIMEOUT = 25

# optional deps
try:
    from PIL import Image  # type: ignore
    PIL_OK = True
except Exception:
    PIL_OK = False

try:
    import cairosvg  # type: ignore
    CAIRO_OK = True
except Exception:
    CAIRO_OK = False

def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower())
    return re.sub(r"-+", "-", s).strip("-")

def http_get(url: str, **kw):
    r = requests.get(url, headers=HEADERS, timeout=kw.pop("timeout", TIMEOUT), **kw)
    r.raise_for_status()
    return r

# ---------- Sources ----------
def get_official_domain(brand: str) -> str | None:
    try:
        r = http_get(WIKIDATA_SEARCH, params={
            "action": "wbsearchentities", "language": "en", "format": "json", "search": brand
        })
        for item in r.json().get("search", []):
            qid = item.get("id")
            if not qid:
                continue
            ent = http_get(WIKIDATA_ENTITY.format(qid)).json()
            claims = ent["entities"][qid].get("claims", {})
            if "P856" in claims:
                url = claims["P856"][0]["mainsnak"]["datavalue"]["value"]
                host = urlparse(url).hostname
                if host and "." in host:
                    return host.replace("www.", "")
    except Exception:
        pass
    return None

def try_brandfetch(domain: str):
    key = os.getenv("BRANDFETCH_KEY")
    if not key:
        return (None, None, None)
    try:
        r = http_get(f"https://api.brandfetch.io/v2/brands/{domain}",
                     headers={"Authorization": f"Bearer {key}"})
        data = r.json()
        assets = []
        for block in data.get("logos", []):
            for f in block.get("formats", []):
                assets.append(f.get("src"))
        for u in assets:
            if not u:
                continue
            fmt, blob, src = try_download(u)
            if fmt:
                return (fmt, blob, src)
    except Exception:
        pass
    return (None, None, None)

def try_clearbit(domain: str):
    try:
        r = http_get(f"https://logo.clearbit.com/{domain}")
        c = r.content
        if c[:4] == b"\x89PNG":
            return ("png", c, r.url)
        if c[:5] == b"<?xml" or b"<svg" in c[:200].lower():
            return ("svg", c, r.url)
    except Exception:
        pass
    return (None, None, None)

def find_logo_links_in_brand_resources(domain: str):
    candidates = [
        f"https://{domain}/brand", f"https://{domain}/press", f"https://{domain}/media",
        f"https://{domain}/brandassets", f"https://{domain}/brand-resources", f"https://{domain}/newsroom"
    ]
    links = []
    for u in candidates:
        try:
            html = http_get(u).text
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.find_all(["a", "img"]):
                for attr in ("href", "src"):
                    v = tag.get(attr)
                    if not v:
                        continue
                    if any(v.lower().endswith(ext) for ext in (".svg", ".png", ".zip", ".eps", ".ai", ".pdf")):
                        if v.startswith("//"): v = "https:" + v
                        elif v.startswith("/"): v = f"https://{domain}{v}"
                        links.append(v)
        except Exception:
            continue
    return links

def try_download(url: str):
    try:
        r = http_get(url)
        ctype = r.headers.get("Content-Type", "").lower()
        data = r.content
        if data[:4] == b"\x89PNG" or "image/png" in ctype:
            return ("png", data, r.url)
        head = data[:200].decode("utf-8", "ignore").lower()
        if data[:5] == b"<?xml" or "<svg" in head or "image/svg" in ctype:
            return ("svg", data, r.url)
    except Exception:
        pass
    return (None, None, None)

def try_wikimedia(brand: str):
    try:
        html = http_get("https://commons.wikimedia.org/w/index.php",
                        params={"search": f"{brand} logo svg"}).text
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a"):
            href = a.get("href") or ""
            if "File:" in href and ("svg" in href.lower() or "logo" in href.lower()):
                file_url = "https://commons.wikimedia.org" + href
                page = http_get(file_url).text
                s2 = BeautifulSoup(page, "html.parser")
                orig = s2.select_one("a.internal")
                if orig and orig.get("href"):
                    u = "https:" + orig["href"] if orig["href"].startswith("//") else orig["href"]
                    fmt, blob, src = try_download(u)
                    if fmt:
                        return (fmt, blob, src)
    except Exception:
        pass
    return (None, None, None)

def try_simple_icons(brand: str):
    slug = slugify(brand)
    url = f"https://cdn.simpleicons.org/{slug}"
    try:
        r = http_get(url)
        data = r.content
        if data[:5] == b"<?xml" or b"<svg" in data[:200].lower():
            return ("svg", data, r.url)
    except Exception:
        pass
    return (None, None, None)

# ---------- Saving ----------
def save_raw(brand: str, fmt: str, blob: bytes) -> str | None:
    slug = slugify(brand)
    if fmt == "svg":
        p = OUT / "svg" / f"{slug}.svg"
        p.write_bytes(blob)
        return str(p)
    if fmt == "png":
        p = OUT / "png" / f"{slug}.png"
        p.write_bytes(blob)
        return str(p)
    return None

def optional_normalize_png(png_bytes: bytes) -> bytes:
    if not PIL_OK:
        return png_bytes
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        size, margin = 1024, 64
        w, h = img.size
        max_side = size - 2 * margin
        scale = min(max_side / w, max_side / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        img = img.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(img, ((size - nw) // 2, (size - nh) // 2), img)
        buff = io.BytesIO()
        canvas.save(buff, format="PNG")
        return buff.getvalue()
    except Exception:
        return png_bytes

def optional_svg_to_png(svg_bytes: bytes) -> bytes | None:
    if not CAIRO_OK:
        return None
    try:
        return cairosvg.svg2png(bytestring=svg_bytes)
    except Exception:
        return None

# ---------- Pipeline ----------
def pipeline_for_brand(brand: str):
    rec = {
        "brand": brand, "slug": slugify(brand),
        "domain": None, "source_url": None,
        "saved_svg": None, "saved_png": None,
        "notes": None
    }

    domain = get_official_domain(brand)
    rec["domain"] = domain

    trials = []
    if domain:
        trials.append(lambda: try_brandfetch(domain))
        trials.append(lambda: try_clearbit(domain))
        for u in find_logo_links_in_brand_resources(domain)[:6]:
            trials.append(lambda u=u: try_download(u))

    trials.append(lambda: try_wikimedia(brand))
    trials.append(lambda: try_simple_icons(brand))

    fmt, blob, src = (None, None, None)
    for step in trials:
        fmt, blob, src = step()
        if fmt:
            rec["source_url"] = src
            break

    if not fmt:
        rec["notes"] = "Logo not found"
        return rec

    path = save_raw(brand, fmt, blob)
    if path and path.endswith(".svg"):
        rec["saved_svg"] = path
        png_bytes = optional_svg_to_png(blob)
        if png_bytes:
            png_bytes = optional_normalize_png(png_bytes)
            p = OUT / "png" / f"{rec['slug']}.png"
            p.write_bytes(png_bytes)
            rec["saved_png"] = str(p)
    elif path and path.endswith(".png"):
        rec["saved_png"] = path
        norm = optional_normalize_png(blob)
        if norm != blob:
            p = OUT / "png" / f"{rec['slug']}.png"
            p.write_bytes(norm)
            rec["saved_png"] = str(p)
    return rec

def main(brands_csv=os.getenv("CSV_PATH", "brands.csv")):
    meta = []
    p = Path(brands_csv)
    if not p.exists():
        raise SystemExit(f"brands CSV not found: {p}")

    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            brand = (row.get("brand") or "").strip()
            if not brand:
                continue
            print(">>>", brand)
            rec = pipeline_for_brand(brand)
            meta.append(rec)
            time.sleep(0.2)

    (OUT / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Done. See logos/svg, logos/png and logos/metadata.json")

if __name__ == "__main__":
    main()
