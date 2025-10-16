import os, csv, json, io, re, time, random
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
HEADERS = {"User-Agent": "logo-fetcher/3.2 (+contact: you@example.com)"}
TIMEOUT = 25

# Prioritetai ir elgsena
OFFICIAL_PRIORITY = True      # Pirma ieškome oficialiame domene
ENABLE_FALLBACKS  = True      # Jei oficialių nėra — bandome toliau
CLEARBIT_SIZE     = 1024      # Clearbit PNG dydis
PNG_CANVAS        = 1024      # Normalizuoto PNG drobė
SVG_PNG_TARGET    = 2048      # Kiek px generuoti iš SVG (kraštinei)

# Kuriuos oficialius puslapius skenuoti
BRAND_PATHS = [
    "brand", "brand-assets", "brandassets", "brand-resources",
    "press", "media", "media-kit", "newsroom", "about", "corporate", "design"
]

# ---------- Optional deps ----------
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

def is_jpg_bytes(b: bytes) -> bool:
    return b[:3] == b"\xff\xd8\xff"

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

def get_official_domain_from_entity(entity: dict | None) -> str | None:
    if not entity:
        return None
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

def get_social_profiles_from_entity(entity: dict | None) -> dict:
    out = {}
    if not entity:
        return out
    claims = entity.get("claims", {})
    # Facebook (P2013)
    try:
        if "P2013" in claims:
            fb = claims["P2013"][0]["mainsnak"]["datavalue"]["value"]
            if fb:
                out["facebook"] = f"https://www.facebook.com/{fb}"
    except Exception:
        pass
    # LinkedIn (P4264 arba P6634)
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
    # Fallback per sitelinks
    try:
        sitelinks = entity.get("sitelinks", {})
        for _, val in sitelinks.items():
            url = val.get("url", "")
            if "linkedin.com/company" in url:
                out.setdefault("linkedin", url)
    except Exception:
        pass
    return out


# ---------- Domain guessing (kai P856 nėra) ----------
TLDS = [
    "com","eu","net","org","io","co",
    "lt","pl","de","fr","it","es","nl","se","cz","sk","hu","dk","be","at","ie","fi","pt","ro","bg","hr","si","lv","ee","gr","cy","lu","mt"
]
BRAND_PREFIXES = ["", "get", "go", "my"]
BRAND_SUFFIXES = ["", "app", "group", "company"]
DIACRITIC_TABLE = str.maketrans({
    "ą":"a","č":"c","ę":"e","ė":"e","į":"i","š":"s","ų":"u","ū":"u","ž":"z",
    "Ą":"a","Č":"c","Ę":"e","Ė":"e","Į":"i","Š":"s","Ų":"u","Ū":"u","Ž":"z",
    "ł":"l","ś":"s","ń":"n","ż":"z","ź":"z","ó":"o","ę":"e","ć":"c","Ł":"l","Ś":"s","Ń":"n","Ż":"z","Ź":"z","Ó":"o","Ć":"c"
})

def normalize_brand_token(s: str) -> str:
    s = s.translate(DIACRITIC_TABLE)
    s = re.sub(r"[^a-zA-Z0-9]+", "", s)
    return s.lower()

def brand_tokens(brand: str):
    # brand žodžiai patikrai (naudojami homepage validacijoje)
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", brand.lower()) if len(w) >= 3]
    base = normalize_brand_token(brand)
    tokens = set([base, *words])
    for pre in BRAND_PREFIXES:
        for suf in BRAND_SUFFIXES:
            combo = f"{pre}{base}{suf}"
            if combo:
                tokens.add(combo)
    parts = re.findall(r"[a-z0-9]+", brand.lower())
    if len(parts) > 1:
        joined = "".join(normalize_brand_token(p) for p in parts)
        tokens.add(joined)
        tokens.add("-".join(p for p in parts))
    return list(tokens), words

def candidate_domains(brand: str):
    tokens, _ = brand_tokens(brand)
    cands = []
    for t in tokens:
        for tld in TLDS:
            cands.append(f"{t}.{tld}")
    seen, out = set(), []
    for d in cands:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out

def quick_domain_check(domain: str) -> bool:
    try:
        r = http_get(f"https://{domain}/", timeout=8)
        return r.status_code < 500
    except Exception:
        try:
            r = http_get(f"http://{domain}/", timeout=6)
            return r.status_code < 500
        except Exception:
            return False

def homepage_has_brand_word(domain: str, brand_words: list[str]) -> bool:
    """Patikrina ar title/body turi bent vieną brand žodį."""
    try:
        try:
            r = http_get(f"https://{domain}/", timeout=10)
        except Exception:
            r = http_get(f"http://{domain}/", timeout=10)
    except Exception:
        return False

    html = r.text[:200_000].lower()
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    try:
        title = (soup.title.text or "").strip().lower()
    except Exception:
        pass
    for w in brand_words:
        if w in title or w in html:
            return True
    return False

def brand_match_heuristic(domain: str, brand: str) -> bool:
    tokens, words = brand_tokens(brand)
    return homepage_has_brand_word(domain, words)

def discover_official_domain(brand: str, entity: dict | None) -> str | None:
    dom = get_official_domain_from_entity(entity)
    if dom:
        return dom
    tokens, words = brand_tokens(brand)
    for cand in candidate_domains(brand):
        if not quick_domain_check(cand):
            continue
        if homepage_has_brand_word(cand, words):
            return cand
    return None


# ---------- Official site crawling ----------
def find_official_asset_links(domain: str):
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
                if v.startswith("//"):
                    full = "https:" + v
                elif v.startswith("/"):
                    full = urljoin(base, v)
                else:
                    full = v
                if not is_same_or_subdomain(full, domain):
                    continue
                low = full.lower()
                if low.endswith(".svg") or low.endswith(".png"):
                    links.append(full)
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
        if "image/jpeg" in ctype or is_jpg_bytes(data):
            return ("jpg", data, r.url)
    except Exception:
        pass
    return (None, None, None)


# ---------- Social (FB/LinkedIn) ----------
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

def try_social_images(entity: dict | None):
    if not entity:
        return (None, None, None, None, None)
    social = get_social_profiles_from_entity(entity)
    # Facebook
    fb = social.get("facebook")
    if fb:
        img = get_og_image(fb)
        if img:
            fmt, blob, src = try_download(img)
            if fmt:
                return (fmt if fmt in ("svg","png") else "jpg", blob, src, True, "medium-high")
    # LinkedIn
    li = social.get("linkedin")
    if li:
        img = get_og_image(li)
        if img:
            fmt, blob, src = try_download(img)
            if fmt:
                return (fmt if fmt in ("svg","png") else "jpg", blob, src, True, "medium-high")
    return (None, None, None, None, None)


# ---------- Brandfetch (CDN + API) ----------
def try_brandfetch_cdn(domain: str):
    base = f"https://cdn.brandfetch.io/{domain}"
    candidates = [base, f"{base}?c={random.randint(10**7,10**9)}"]
    for u in candidates:
        try:
            r = http_get(u)
            data = r.content
            if is_svg_bytes(data):
                return ("svg", data, r.url, True, "high")
            if is_png_bytes(data):
                return ("png", data, r.url, True, "medium-high")
            if is_jpg_bytes(data):
                return ("jpg", data, r.url, True, "medium")
        except Exception:
            continue
    return (None, None, None, None, None)

def try_brandfetch_api(domain: str):
    key = os.getenv("BRANDFETCH_KEY")
    if not key:
        return (None, None, None, None, None)
    try:
        r = http_get(f"https://api.brandfetch.io/v2/brands/{domain}",
                     headers={"Authorization": f"Bearer {key}"})
        data = r.json()
        svgs, pngs, others = [], [], []
        for block in data.get("logos", []):
            for f in block.get("formats", []):
                src = f.get("src")
                if not src:
                    continue
                sl = src.lower()
                if sl.endswith(".svg"):
                    svgs.append(src)
                elif sl.endswith(".png"):
                    pngs.append(src)
                else:
                    others.append(src)
        for u in svgs + pngs + others:
            fmt, blob, src = try_download(u)
            if fmt:
                qual = "high" if fmt == "svg" else ("medium-high" if fmt == "png" else "medium")
                return (fmt, blob, src, True, qual)
    except Exception:
        pass
    return (None, None, None, None, None)


# ---------- Google Images (per CSE Image Search) ----------
def homepage_has_brand_word_for_host(host: str, brand: str) -> bool:
    _, words = brand_tokens(brand)
    domain = host
    return homepage_has_brand_word(domain, words)

def try_google_images(brand: str, domain: str | None):
    """
    Naudoja Google Custom Search JSON API su searchType=image.
    Filtruoja rezultatus:
      - prideda keyword 'logo'
      - paima 'image.contextLink' domeną
      - tikrina ar to domeno pagr. puslapis/titulas turi bent vieną brand žodį
    """
    cse_id  = os.getenv("GOOGLE_CSE_ID")
    cse_key = os.getenv("GOOGLE_CSE_KEY")
    if not cse_id or not cse_key:
        return (None, None, None, None, None)
    try:
        r = http_get("https://www.googleapis.com/customsearch/v1", params={
            "q": f"{brand} logo",
            "cx": cse_id,
            "key": cse_key,
            "searchType": "image",
            "num": 6,
            "safe": "off"
        })
        for item in r.json().get("items", []):
            # 'link' – tiesioginis vaizdo URL, 'image.contextLink' – puslapis, kuriame yra vaizdas
            img_url = item.get("link")
            context = item.get("image", {}).get("contextLink") or item.get("image", {}).get("contextLinkUrl")
            if not img_url or not context:
                continue
            host = urlparse(context).hostname or ""
            if not host:
                continue
            # Patikrinam ar hosto homepage/titule yra bent vienas brand žodis
            if not homepage_has_brand_word_for_host(host, brand):
                continue
            # Jei patikra praėjo – bandome parsisiųsti
            fmt, blob, src = try_download(img_url)
            if not fmt:
                continue
            official = (domain is not None) and (host == domain or host.endswith("." + domain))
            quality = "high" if fmt == "svg" else ("medium-high" if fmt == "png" else "medium")
            return (fmt, blob, src, official, quality)
    except Exception:
        pass
    return (None, None, None, None, None)


# ---------- Kiti fallback'ai ----------
def try_clearbit(domain: str):
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
    cse_id  = os.getenv("GOOGLE_CSE_ID")
    cse_key = os.getenv("GOOGLE_CSE_KEY")
    if not cse_id or not cse_key:
        return (None, None, None, None, None)
    try:
        query = f'{brand} logo filetype:svg'
        if domain:
            query += f' (site:{domain} OR site:*.{domain})'
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
    ext = "png" if fmt == "jpg" else fmt
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
        scale = min(size / w, size / h, 1.0)  # be prievartinio upscaling
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
        return jpg_bytes
    try:
        img = Image.open(io.BytesIO(jpg_bytes)).convert("RGBA")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return jpg_bytes


# ---------- Pipelines ----------
def pipeline_official_first(brand: str, entity: dict | None, domain: str | None):
    rec = {
        "brand": brand, "slug": slugify(brand),
        "domain": domain, "source_url": None,
        "official": False, "saved_svg": None, "saved_png": None, "notes": None
    }

    # 1) official domain assets
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

    # 2) social (FB/LinkedIn) — laikome official (oficialios paskyros)
    fmt, blob, src, off, _ = try_social_images(entity)
    if fmt:
        rec["source_url"] = src
        rec["official"] = True
        if fmt == "svg":
            rec["saved_svg"] = save_raw(brand, "svg", blob)
            png = svg_to_png(blob, SVG_PNG_TARGET)
            if png:
                png = normalize_png(png)
                (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
                rec["saved_png"] = f"logos/png/{rec['slug']}.png"
        elif fmt == "png":
            rec["saved_png"] = save_raw(brand, "png", blob)
            norm = normalize_png(blob)
            if norm != blob:
                (OUT / "png" / f"{rec['slug']}.png").write_bytes(norm)
        elif fmt == "jpg":
            png = jpg_to_png(blob)
            png = normalize_png(png)
            (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
            rec["saved_png"] = f"logos/png/{rec['slug']}.png"
        return rec

    rec["notes"] = "No official/site/social logo found."
    return rec


def pipeline_with_fallbacks(brand: str, entity: dict | None, domain: str | None):
    rec = pipeline_official_first(brand, entity, domain)
    if rec.get("source_url"):
        return rec

    # Fallback chain: Brandfetch CDN → Brandfetch API → Clearbit → Wikimedia → Google Images → Simple Icons → Google CSE
    fmt = blob = src = None
    official = False

    if ENABLE_FALLBACKS and domain:
        fmt, blob, src, official, _ = try_brandfetch_cdn(domain)
        if not fmt:
            fmt, blob, src, official, _ = try_brandfetch_api(domain)
        if not fmt:
            fmt, blob, src, official, _ = try_clearbit(domain)

    if ENABLE_FALLBACKS and not fmt:
        fmt, blob, src, official, _ = try_wikimedia(brand)

    if ENABLE_FALLBACKS and not fmt:
        fmt, blob, src, official, _ = try_google_images(brand, domain)

    if ENABLE_FALLBACKS and not fmt:
        fmt, blob, src, official, _ = try_simple_icons(brand)

    if ENABLE_FALLBACKS and not fmt:
        fmt, blob, src, official, _ = try_google_cse(brand, domain)

    if not fmt:
        return rec  # lieka "No official/site/social logo found."

    rec["source_url"] = src
    rec["official"]   = bool(official)

    if fmt == "svg":
        rec["saved_svg"] = save_raw(brand, "svg", blob)
        png = svg_to_png(blob, SVG_PNG_TARGET)
        if png:
            png = normalize_png(png)
            (OUT / "png" / f"{rec['slug']}.png").write_bytes(png)
            rec["saved_png"] = f"logos/png/{rec['slug']}.png"
    elif fmt == "png":
        rec["saved_png"] = save_raw(brand, "png", blob)
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
    discovered_domain = discover_official_domain(brand, entity)
    if OFFICIAL_PRIORITY:
        return pipeline_with_fallbacks(brand, entity, discovered_domain)
    else:
        return pipeline_with_fallbacks(brand, entity, discovered_domain)


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
