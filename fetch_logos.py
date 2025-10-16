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
HEADERS = {"User-Agent": "logo-fetcher/2.3 (+contact: you@example.com)"}
TIMEOUT = 25

BRAND_PATHS = [
    "brand", "brand-assets", "brandassets", "brand-resources",
    "press", "media", "media-kit", "newsroom", "about", "corporate", "design"
]

# Behavior toggles
OFFICIAL_PRIORITY = True         # pirma ieškome oficialiame domene
ENABLE_FALLBACKS  = True         # jei oficialių nėra — bandome nurodytus fallback
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


# ---------- Wikidata helpers ----------
def get_wikidata_entity_for_brand(brand: str) -> dict | None:
    try:
        r = http_get(WIKIDATA_SEARCH, params={
            "action": "wbsearchentities", "language": "en", "format": "json", "search": brand
        })
        for item in r.json().get("search", []):
            qid = item.get("id")
            if not qid:
                continue
            ent = http_get(WIKIDATA_ENTITY.format(qid)).json()
            return ent["entities"].get(qid, {})
    except Exception:
        pass
    return None

def get_official_domain_from_entity(entity: dict) -> str | None:
    try:
        claims = entity.get("claims", {})
        if "P856" in claims:
            url = claims["P856"][0]["mainsnak"]["datavalue"]["value"]
            host = urlparse(url).hostname
            if host and "." in host:
                return host.replace("www.", "")
    except Exception:
        pass
    return None

def get_social_profiles_from_entity(entity: dict) -> dict:
    """
    Grąžina dict su 'facebook' ir 'linkedin' nuorodomis, jei yra:
      - Facebook ID:   P2013 (page/username)
      - LinkedIn org:  P4264 (org numeric string) arba P6634 (org ID)
      - LinkedIn URL:  P856 — kartais būna papildomas
    """
    out = {}
    claims = entity.get("claims", {})
    # Facebook (P2013)
    try:
        if "P2013" in claims:
            fb = claims["P2013"][0]["mainsnak"]["datavalue"]["value"]
            if fb:
                out["facebook"] = f"https://www.facebook.com/{fb}"
    except Exception:
        pass
    # LinkedIn (P4264 or P6634 or via sitelinks)
    try:
        if "P4264" in claims:
            li = claims["P4264"][0]["mainsnak"]["datavalue"]["value"]
            if li:
                out["linkedin"] = f"https://www.linkedin.com/company/{li}"
        elif "P6634" in claims:
            li = claims["P6634"][0]["mainsnak"]["datavalue"]["value"]
            if li:
                out["linkedin"] = f"https://www.linkedin.com/company/{li}"
    except Exception:
        pass
    # Fallback: per sitelinks (retai)
    try:
        sitelinks = entity.get("sitelinks", {})
        for key, val in sitelinks.items():
            url = val.get("url", "")
            if "linkedin.com/company" in url:
                out.setdefault("linkedin", url)
    except Exception:
        pass
    return out


# ---------- Official domain: assets crawl ----------
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
        # Accept large JPGs from social as PNG later
        if "image/jpeg" in ctype or data[:3] == b"\xff\xd8\xff":
            return ("jpg", data, r.url)
    except Exception:
        pass
    return (None, None, None)


# ---------- Social (Facebook / LinkedIn) ----------
def get_og_image(url: str) -> str | None:
    try:
        html = http_get(url).text
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
        if tag and tag.get("content"):
            return tag["content"]
    except Exception:
        pass
    return None

def try_social_images(social: dict):
    """
    Bando paimti og:image iš Facebook / LinkedIn.
    Grąžina (fmt, blob, src, official=True, quality).
    """
    # Facebook
    fb = social.get("facebook")
    if fb:
        img = get_og_image(fb)
        if img:
            fmt, blob, src = try_download(img)
            if fmt:
                return (fmt if fmt in ("png", "svg") else "jpg", blob, src, True, "medium-high")
    # LinkedIn
    li = social.get("linkedin")
    if li:
        img = get_og_image(li)
        if img:
            fmt, blob, src = try_download(img)
            if fmt:
                return (fmt if fmt in ("png", "svg") else "jpg", blob, src, True, "medium-high")
    return (None, None, None, None, None)


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


def try_google_cse(brand: str, domain: str | None):
    """Optional Google CSE fallback. Filters by official domain if provided."""
    cse_id  = os.getenv("GOOGLE_CSE_ID")
    cse_key = os.getenv("GOOGLE_CSE_KEY")
    if not cse_id or not cse_key:
        return (None, None, None, None, None)
    try:
        query = f'{brand} logo filetype:svg' + (f' site:{domain}' if domain else '')
        r = http_get("https://www.googleapis.com/customsearch/v1",
                     params={"q": query, "cx": cse_id, "key": cse_key, "num": 5})
        for item in r.json().get("items", []):
            link = item.get("link")
            if not link:
                continue
            fmt, blob, src = try_download(link)
            if fmt:
                official = is_same_or_subdomain(src, domain) if domain else False
                quality = "high" if (fmt == "svg") else "medium-high"
                return (fmt, blob, src, official, quality)
    except Exception:
        pass
    return (None, None, None, None, None)


# ---------- Save & render ----------
def save_raw(brand: str, fmt: str, blob: bytes) -> str:
    slug = slugify(brand)
    ext = "png" if fmt == "jpg" else fmt  # jpg konvertuosime į png failų medyje
    p = OUT / ext / f"{slug}.{ext}"
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

def jpg_to_png(jpg_bytes: bytes) -> bytes:
    if not PIL_OK:
        return jpg_bytes  # kaip yra
    try:
        img = Image.open(io.BytesIO(jpg_bytes)).convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return jpg_bytes


# ---------- Pipelines ----------
def pipeline_official_first(brand: str, entity: dict | None):
    rec = {
        "brand": brand, "slug": slugify(brand),
        "domain": None, "source_url": None,
        "official": False, "saved_svg": None, "saved_png": None, "notes": None
    }
    domain = get_official_domain_from_entity(entity) if entity else None
    rec["domain"] = domain

    # 1) Official site assets
    if domain:
        links = find_official_asset_links(domain)
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
                    (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
                    rec["saved_png"] = f"logos/png/{rec['slug']}.png"
            elif fmt == "png":
                rec["saved_png"] = path
                norm = normalize_png(blob)
                if norm != blob:
                    (OUT / "png" / f"{rec['slug']}.png").write_bytes(norm)
            return rec

    # 2) Facebook / LinkedIn (from Wikidata)
    if entity:
        social = get_social_profiles_from_entity(entity)
        fmt, blob, src, official, quality = try_social_images(social)
        if fmt:
            rec["source_url"] = src
            rec["official"] = True  # oficialios paskyros
            # social images dažniausiai JPG -> konvertuojam į PNG
            if fmt == "jpg":
                png = jpg_to_png(blob)
                png = normalize_png(png)
                (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
                rec["saved_png"] = f"logos/png/{rec['slug']}.png"
            elif fmt == "png":
                path = save_raw(brand, "png", blob)
                rec["saved_png"] = path
                norm = normalize_png(blob)
                if norm != blob:
                    (OUT / "png" / f"{rec['slug']}.png").write_bytes(norm)
            elif fmt == "svg":
                path = save_raw(brand, "svg", blob)
                rec["saved_svg"] = path
                png = svg_to_png(blob, SVG_PNG_TARGET)
                if png:
                    png = normalize_png(png)
                    (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
                    rec["saved_png"] = f"logos/png/{rec['slug']}.png"
            return rec

    rec["notes"] = "No official/site/social logo found."
    return rec


def pipeline_with_fallbacks(brand: str, entity: dict | None):
    """Official first → Social → Fallback chain."""
    rec = pipeline_official_first(brand, entity)
    if rec.get("source_url"):
        return rec  # jau radome

    # Fallbacks
    domain = rec.get("domain")
    fmt = blob = src = None
    official = False

    if ENABLE_FALLBACKS and domain:
        fmt, blob, src, official, _ = try_brandfetch(domain)
        if not fmt:
            fmt, blob, src, official, _ = try_clearbit(domain)

    if ENABLE_FALLBACKS and not fmt:
        fmt, blob, src, official, _ = try_wikimedia(brand)

    if ENABLE_FALLBACKS and not fmt:
        fmt, blob, src, official, _ = try_simple_icons(brand)

    if ENABLE_FALLBACKS and not fmt:
        fmt, blob, src, official, _ = try_google_cse(brand, domain)

    if not fmt:
        return rec  # lieka "No official/site/social logo found."

    rec["source_url"] = src
    rec["official"]   = bool(official)

    if fmt == "svg":
        path = save_raw(brand, "svg", blob)
        rec["saved_svg"] = path
        png = svg_to_png(blob, SVG_PNG_TARGET)
        if png:
            png = normalize_png(png)
            (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
            rec["saved_png"] = f"logos/png/{rec['slug']}.png"
    elif fmt == "png":
        path = save_raw(brand, "png", blob)
        rec["saved_png"] = path
        norm = normalize_png(blob)
        if norm != blob:
            (OUT / "png" / f"{rec['slug']}.png").write_bytes(norm)
    elif fmt == "jpg":
        png = jpg_to_png(blob)
        png = normalize_png(png)
        (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
        rec["saved_png"] = f"logos/png/{rec['slug']}.png"

    return rec


def process_brand(brand: str):
    entity = get_wikidata_entity_for_brand(brand)
    if OFFICIAL_PRIORITY:
        return pipeline_with_fallbacks(brand, entity)
    # jei kada norėtum be prioriteto:
    return pipeline_with_fallbacks(brand, entity)


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
