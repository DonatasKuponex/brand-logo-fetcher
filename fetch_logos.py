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
HEADERS = {"User-Agent": "logo-fetcher/1.2 (+contact: you@example.com)"}
TIMEOUT = 25

CLEARBIT_SIZE = 1024         # didesnis rastras iš Clearbit
PNG_CANVAS = 1024            # normalizuoto PNG drobė
SVG_PNG_TARGET = 2048        # kiek px generuoti iš SVG (kraštinei)

# optional deps (saugūs importai)
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
        # Surenkame visus galimus asset URL, pirmenybė SVG
        svgs, pngs = [], []
        for block in data.get("logos", []):
            for f in block.get("formats", []):
                src = f.get("src")
                if not src:
                    continue
                if src.lower().endswith(".svg"):
                    svgs.append(src)
                elif src.lower().endswith(".png"):
                    pngs.append(src)
        for u in svgs + pngs:
            fmt, blob, src = try_download(u)
            if fmt:
                return (fmt, blob, src)
    except Exception:
        pass
    return (None, None, None)

def try_clearbit(domain: str):
    # bandome PNG su nurodytu dydžiu
    try:
        r = http_get(f"https://logo.clearbit.com/{domain}?size={CLEARBIT_SIZE}")
        c = r.content
        if c[:4] == b"\x89PNG":
            return ("png", c, r.url)
        # kartais grįžta SVG per redirect
        head = c[:200].lower()
        if c[:5] == b"<?xml" or b"<svg" in head:
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
                    v_low = v.lower()
                    if any(v_low.endswith(ext) for ext in (".svg", ".png")):
                        if v.startswith("//"): v = "https:" + v
                        elif v.startswith("/"): v = f"https://{domain}{v}"
                        links.append(v)
        except Exception:
            continue
    # pirmenybė svg
    links = sorted(links, key=lambda x: (0 if x.lower().endswith(".svg") else 1, len(x)))
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

# ---------- Saving / rendering ----------
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
    """Pad center on 1024x1024 canvas without upscaling small rasters."""
    if not PIL_OK:
        return png_bytes
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        w, h = img.size
        # Jei PNG jau didelis, nekeičiam rezoliucijos, tik uždedam ant drobės (be resample)
        if max(w, h) >= PNG_CANVAS:
            canvas = Image.new("RGBA", (PNG_CANVAS, PNG_CANVAS), (0, 0, 0, 0))
            # sumažinam per vieną žingsnį tik jei reikia
            scale = min(PNG_CANVAS / w, PNG_CANVAS / h, 1.0)
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            cw, ch = img.size
            canvas.paste(img, ((PNG_CANVAS - cw) // 2, (PNG_CANVAS - ch) // 2), img)
            buff = io.BytesIO()
            canvas.save(buff, format="PNG")
            return buff.getvalue()
        # Jei mažas — neupskeilinam (kad neišplautų), tik padedam į centrą
        canvas = Image.new("RGBA", (PNG_CANVAS, PNG_CANVAS), (0, 0, 0, 0))
        canvas.paste(img, ((PNG_CANVAS - w) // 2, (PNG_CANVAS - h) // 2), img)
        buff = io.BytesIO()
        canvas.save(buff, format="PNG")
        return buff.getvalue()
    except Exception:
        return png_bytes

def svg_to_png(svg_bytes: bytes, target_px: int = SVG_PNG_TARGET) -> bytes | None:
    if not CAIRO_OK:
        return None
    try:
        return cairosvg.svg2png(bytestring=svg_bytes, output_width=target_px)
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
        trials.append(lambda: try_brandfetch(domain))    # dažnai duoda SVG
        trials.append(lambda: try_clearbit(domain))      # su ?size=1024
        for u in find_logo_links_in_brand_resources(domain)[:8]:
            trials.append(lambda u=u: try_download(u))

    trials.append(lambda: try_wikimedia(brand))          # dažnai SVG
    trials.append(lambda: try_simple_icons(brand))       # visada SVG (mono)

    fmt, blob, src = (None, None, None)
    for step in trials:
        fmt, blob, src = step()
        if fmt:
            rec["source_url"] = src
            break

    if not fmt:
        rec["notes"] = "Logo not found"
        return rec

    # 1) Visada išsaugome originalą
    path = save_raw(brand, fmt, blob)
    if path and path.endswith(".svg"):
        rec["saved_svg"] = path
        # 2) Jei turim SVG ir yra cairosvg — generuojam didelės raiškos PNG
        png_bytes = svg_to_png(blob, target_px=SVG_PNG_TARGET)
        if png_bytes:
            png_bytes = optional_normalize_png(png_bytes)
            p = OUT / "png" / f"{rec['slug']}.png"
            p.write_bytes(png_bytes)
            rec["saved_png"] = str(p)

    elif path and path.endswith(".png"):
        rec["saved_png"] = path
        # 3) Normalizacija (be privalomo mažinimo ar dirbtinio upscaling)
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
