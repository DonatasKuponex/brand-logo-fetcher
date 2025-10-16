# fetch_logos.py — official-priority with fallbacks
import os, csv, json, io, re, time
from pathlib import Path
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup

# -------- Settings --------
OUT = Path("logos")
(OUT / "svg").mkdir(parents=True, exist_ok=True)
(OUT / "png").mkdir(parents=True, exist_ok=True)

WIKIDATA_SEARCH = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY  = "https://www.wikidata.org/wiki/Special:EntityData/{}.json"
HEADERS = {"User-Agent": "logo-fetcher/2.2 (+contact: you@example.com)"}
TIMEOUT = 25

BRAND_PATHS = [
    "brand", "brand-assets", "brandassets", "brand-resources",
    "press", "media", "media-kit", "newsroom", "about", "corporate", "design"
]

# Behavior toggles
OFFICIAL_PRIORITY = True         # pirma ieškome tik oficialiame domene
ENABLE_FALLBACKS  = True         # jei oficialių nėra — bandome žemiau nurodytus
CLEARBIT_SIZE     = 1024         # Clearbit PNG dydis
PNG_CANVAS        = 1024         # normalizuotos PNG drobė
SVG_PNG_TARGET    = 2048         # kiek px generuoti iš SVG

# Optional deps
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


# ---------- Utils ----------
def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower())
    return re.sub(r"-+", "-", s).strip("-")

def http_get(url: str, **kw):
    r = requests.get(url, headers=HEADERS, timeout=kw.pop("timeout", TIMEOUT), **kw)
    r.raise_for_status()
    return r

def is_svg_bytes(b: bytes) -> bool:
    head = b[:200].decode("utf-8", "ignore").lower()
    return (b[:5] == b"<?xml") or ("<svg" in head)

def is_png_bytes(b: bytes) -> bool:
    return b[:4] == b"\x89PNG"

def is_same_or_subdomain(url: str, domain: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host == domain or host.endswith("." + domain)
    except Exception:
        return False


# ---------- Sources (official) ----------
def get_official_domain(brand: str) -> str | None:
    """Use Wikidata P856 to find official website domain."""
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


def find_official_asset_links(domain: str):
    """Scan common official pages for SVG/PNG links."""
    links = []
    for path in BRAND_PATHS:
        base = f"https://{domain}/{path}/"
        try:
            html = http_get(base).text
        except Exception:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["a", "img"]):
            for attr in ("href", "src"):
                v = tag.get(attr)
                if not v:
                    continue
                # absolutize
                if v.startswith("//"):
                    full = "https:" + v
                elif v.startswith("/"):
                    full = urljoin(base, v)
                else:
                    full = v
                # keep only official domain (incl. subdomains)
                if not is_same_or_subdomain(full, domain):
                    continue
                low = full.lower()
                if low.endswith(".svg") or low.endswith(".png"):
                    links.append(full)
    # Prioritize SVG
    return sorted(set(links), key=lambda u: (0 if u.lower().endswith(".svg") else 1, len(u)))


def try_download(url: str):
    try:
        r = http_get(url)
        data = r.content
        ctype = r.headers.get("Content-Type", "").lower()
        if is_svg_bytes(data) or "image/svg" in ctype:
            return ("svg", data, r.url)
        if is_png_bytes(data) or "image/png" in ctype:
            return ("png", data, r.url)
    except Exception:
        pass
    return (None, None, None)


# ---------- Fallback sources ----------
def try_brandfetch(domain: str):
    """Brandfetch API (optional). Prioritize SVG, then PNG. Treat as official."""
    key = os.getenv("BRANDFETCH_KEY")
    if not key:
        return (None, None, None, None, None)
    try:
        r = http_get(f"https://api.brandfetch.io/v2/brands/{domain}",
                     headers={"Authorization": f"Bearer {key}"})
        data = r.json()
        svgs, pngs = [], []
        for block in data.get("logos", []):
            for f in block.get("formats", []):
                src = f.get("src")
                if not src:
                    continue
                (svgs if src.lower().endswith(".svg") else pngs).append(src)
        for u in svgs + pngs:
            fmt, blob, src = try_download(u)
            if fmt:
                return (fmt, blob, src, True, "high" if fmt == "svg" else "medium-high")
    except Exception:
        pass
    return (None, None, None, None, None)


def try_clearbit(domain: str):
    """Clearbit Logo API — request larger size; treat as official by domain match."""
    try:
        r = http_get(f"https://logo.clearbit.com/{domain}?size={CLEARBIT_SIZE}")
        c = r.content
        if is_png_bytes(c):
            return ("png", c, r.url, True, "medium-high")
        if is_svg_bytes(c):
            return ("svg", c, r.url, True, "high")
    except Exception:
        pass
    return (None, None, None, None, None)


def try_wikimedia(brand: str):
    """Wikimedia Commons — often SVG; not official."""
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
                        return (fmt, blob, src, False, "high" if fmt == "svg" else "medium")
    except Exception:
        pass
    return (None, None, None, None, None)


def try_simple_icons(brand: str):
    """Simple Icons — SVG (monochrome), not official."""
    slug = slugify(brand)
    url = f"https://cdn.simpleicons.org/{slug}"
    try:
        r = http_get(url)
        data = r.content
        if is_svg_bytes(data):
            return ("svg", data, r.url, False, "medium")
    except Exception:
        pass
    return (None, None, None, None, None)


# ---------- Save & render ----------
def save_raw(brand: str, fmt: str, blob: bytes) -> str:
    slug = slugify(brand)
    p = OUT / fmt / f"{slug}.{fmt}"
    p.write_bytes(blob)
    return str(p)

def svg_to_png(svg_bytes: bytes, px: int = SVG_PNG_TARGET) -> bytes | None:
    if not CAIRO_OK:
        return None
    try:
        return cairosvg.svg2png(bytestring=svg_bytes, output_width=px)
    except Exception:
        return None

def normalize_png(png_bytes: bytes, size=PNG_CANVAS) -> bytes:
    if not PIL_OK:
        return png_bytes
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        w, h = img.size
        scale = min(size / w, size / h, 1.0)  # no hard upscaling
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            w, h = img.size
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(img, ((size - w)//2, (size - h)//2), img)
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return png_bytes


# ---------- Pipeline ----------
def pipeline_official_only(brand: str, domain: str):
    """Return first official SVG/PNG found on common asset pages."""
    rec = {
        "brand": brand, "slug": slugify(brand),
        "domain": domain, "source_url": None,
        "official": False, "saved_svg": None, "saved_png": None, "notes": None
    }

    links = find_official_asset_links(domain)
    if not links:
        rec["notes"] = "No logo links on official pages."
        return rec

    for u in links:
        fmt, blob, src = try_download(u)
        if not fmt:
            continue
        rec["source_url"] = src
        rec["official"] = is_same_or_subdomain(src, domain)
        path = save_raw(brand, fmt, blob)
        if fmt == "svg":
            rec["saved_svg"] = path
            png = svg_to_png(blob, SVG_PNG_TARGET)
            if png:
                png = normalize_png(png)
                p = OUT / "png" / f"{rec['slug']}.png"
                p.write_bytes(png)
                rec["saved_png"] = str(p)
        elif fmt == "png":
            rec["saved_png"] = path
            norm = normalize_png(blob)
            if norm != blob:
                (OUT / "png" / f"{rec['slug']}.png").write_bytes(norm)
        return rec

    rec["notes"] = "No downloadable SVG/PNG on official pages."
    return rec


def pipeline_with_fallbacks(brand: str, domain: str | None):
    """Official first; if none — fallbacks in order."""
    rec = {
        "brand": brand, "slug": slugify(brand),
        "domain": domain, "source_url": None,
        "official": False, "saved_svg": None, "saved_png": None, "notes": None
    }

    # 1) Try official
    if domain:
        r = pipeline_official_only(brand, domain)
        if r.get("source_url"):
            return r

    # 2) Fallbacks (Brandfetch → Clearbit → Wikimedia → Simple Icons)
    fmt = blob = src = None
    official = False
    quality  = None

    if domain:
        fmt, blob, src, official, quality = try_brandfetch(domain)
        if not fmt:
            fmt, blob, src, official, quality = try_clearbit(domain)

    if not fmt:
        fmt, blob, src, official, quality = try_wikimedia(brand)

    if not fmt:
        fmt, blob, src, official, quality = try_simple_icons(brand)

    if not fmt:
        rec["notes"] = "No logo found (official nor fallbacks)."
        return rec

    rec["source_url"] = src
    rec["official"]   = bool(official)

    path = save_raw(brand, fmt, blob)
    if fmt == "svg":
        rec["saved_svg"] = path
        png = svg_to_png(blob, SVG_PNG_TARGET)
        if png:
            png = normalize_png(png)
            (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
            rec["saved_png"] = f"logos/png/{rec['slug']}.png"
    else:
        rec["saved_png"] = path
        norm = normalize_png(blob)
        if norm != blob:
            (OUT / "png" / f"{rec['slug']}.png").write_bytes(norm)
    return rec


def process_brand(brand: str):
    domain = get_official_domain(brand)
    if OFFICIAL_PRIORITY and not ENABLE_FALLBACKS:
        return pipeline_official_only(brand, domain) if domain else {
            "brand": brand, "slug": slugify(brand), "domain": None,
            "source_url": None, "official": False,
            "saved_svg": None, "saved_png": None,
            "notes": "No official domain found."
        }
    # default path: official first, then fallbacks
    return pipeline_with_fallbacks(brand, domain)


# ---------- Main ----------
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
            rec = process_brand(brand)
            meta.append(rec)
            time.sleep(0.2)

    (OUT / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✅ Done. See logos/svg, logos/png and logos/metadata.json")


if __name__ == "__main__":
    main()
