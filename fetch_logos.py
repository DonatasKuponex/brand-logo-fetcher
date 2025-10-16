import csv, json, os, io, re, time, hashlib
from pathlib import Path
from urllib.parse import urlparse
import requests
from PIL import Image
from bs4 import BeautifulSoup

OUT = Path("logos")
(OUT/"svg").mkdir(parents=True, exist_ok=True)
(OUT/"png").mkdir(parents=True, exist_ok=True)

WIKIDATA_SEARCH = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY  = "https://www.wikidata.org/wiki/Special:EntityData/{}.json"
HEADERS = {"User-Agent":"logo-fetcher/1.0 (+contact: you@example.com)"}

def slugify(s:str)->str:
    import re
    s = re.sub(r"[^a-z0-9]+","-", s.lower())
    return re.sub(r"-+","-", s).strip("-")

def http_get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r

def get_official_domain(brand:str)->str|None:
    try:
        r = http_get(WIKIDATA_SEARCH, params={
            "action":"wbsearchentities","language":"en","format":"json","search":brand
        })
        for item in r.json().get("search", []):
            qid = item["id"]
            ent = http_get(WIKIDATA_ENTITY.format(qid)).json()
            claims = ent["entities"][qid].get("claims",{})
            if "P856" in claims:
                url = claims["P856"][0]["mainsnak"]["datavalue"]["value"]
                from urllib.parse import urlparse
                host = urlparse(url).hostname
                if host and "." in host:
                    return host.replace("www.","")
    except Exception:
        pass
    return None

def try_clearbit(domain:str):
    try:
        r = http_get(f"https://logo.clearbit.com/{domain}")
        c = r.content
        if c[:4] == b"\x89PNG":
            return ("png", c, r.url)
        if c[:5] == b"<?xml" or b"<svg" in c[:200].lower():
            return ("svg", c, r.url)
    except Exception:
        pass
    return (None,None,None)

def find_logo_links_in_brand_resources(domain:str):
    candidates = [f"https://{domain}/brand", f"https://{domain}/press",
                  f"https://{domain}/media", f"https://{domain}/brandassets",
                  f"https://{domain}/brand-resources", f"https://{domain}/newsroom"]
    links_all = []
    for u in candidates:
        try:
            html = http_get(u).text
            soup = BeautifulSoup(html, "lxml")
            for tag in soup.find_all(["a","img"]):
                for attr in ("href","src"):
                    v = tag.get(attr)
                    if not v: continue
                    if any(v.lower().endswith(ext) for ext in (".svg",".png",".zip",".eps",".ai",".pdf")):
                        if v.startswith("//"): v = "https:"+v
                        elif v.startswith("/"): v = f"https://{domain}{v}"
                        links_all.append(v)
        except Exception:
            continue
    return links_all

def try_download(url:str):
    try:
        r = http_get(url)
        ctype = r.headers.get("Content-Type","").lower()
        data = r.content
        if data[:4]==b"\x89PNG" or "image/png" in ctype:
            return ("png", data, url)
        if data[:5]==b"<?xml" or "<svg" in data[:200].decode("utf-8","ignore").lower() or "image/svg" in ctype:
            return ("svg", data, url)
    except Exception:
        pass
    return (None,None,None)

def try_wikimedia(brand:str):
    search = f"https://commons.wikimedia.org/w/index.php"
    try:
        html = http_get(search, params={"search": f"{brand} logo svg"}).text
        soup = BeautifulSoup(html, "lxml")
        for a in soup.select("a"):
            href = a.get("href") or ""
            if "File:" in href and ("svg" in href.lower() or "logo" in href.lower()):
                file_url = "https://commons.wikimedia.org"+href
                page = http_get(file_url).text
                s2 = BeautifulSoup(page,"lxml")
                orig = s2.select_one("a.internal")
                if orig and orig.get("href"):
                    u = "https:"+orig.get("href") if orig.get("href").startswith("//") else orig.get("href")
                    fmt, blob, src = try_download(u)
                    if fmt: return (fmt, blob, src)
    except Exception:
        pass
    return (None,None,None)

def try_simple_icons(brand:str):
    slug = slugify(brand)
    url = f"https://cdn.simpleicons.org/{slug}"
    try:
        r = http_get(url)
        data = r.content
        if data[:5]==b"<?xml" or b"<svg" in data[:200].lower():
            return ("svg", data, url)
    except Exception:
        pass
    return (None,None,None)

def sha256_bytes(b:bytes)->str:
    import hashlib
    return hashlib.sha256(b).hexdigest()

def pad_to_square(img:Image.Image, size=1024, margin=64):
    img = img.convert("RGBA")
    w,h = img.size
    max_side = size - 2*margin
    scale = min(max_side/w, max_side/h)
    nw, nh = max(1,int(w*scale)), max(1,int(h*scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    from PIL import Image as PILImage
    canvas = PILImage.new("RGBA",(size,size),(0,0,0,0))
    canvas.paste(img, ((size-nw)//2,(size-nh)//2), img)
    return canvas

def png_normalize(blob:bytes, size=1024, margin=64, remove_bg=False):
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(blob))
    if remove_bg:
        try:
            from rembg import remove
            blob = remove(blob)
            im = Image.open(io.BytesIO(blob))
        except Exception:
            pass
    im2 = pad_to_square(im, size=size, margin=64)
    bio = io.BytesIO()
    im2.save(bio, format="PNG")
    return bio.getvalue()

def save_logo(brand:str, fmt:str, blob:bytes):
    slug = slugify(brand)
    if fmt=="svg":
        p = OUT/"svg"/f"{slug}.svg"
        p.write_bytes(blob)
        return str(p)
    if fmt=="png":
        norm = png_normalize(blob, size=1024, margin=64, remove_bg=False)
        p = OUT/"png"/f"{slug}.png"
        Path(p).write_bytes(norm)
        return str(p)
    return None

def pipeline_for_brand(brand:str):
    record = {
        "brand": brand, "slug": slugify(brand),
        "domain": None, "source_url": None,
        "saved_svg": None, "saved_png": None,
        "hash": None, "notes": None
    }
    domain = get_official_domain(brand)
    record["domain"] = domain

    trials = []
    if domain:
        trials.append(lambda: try_clearbit(domain))
        links = find_logo_links_in_brand_resources(domain)
        for u in links[:6]:
            trials.append(lambda u=u: try_download(u))

    trials.append(lambda: try_wikimedia(brand))
    trials.append(lambda: try_simple_icons(brand))

    fmt, blob, src = (None,None,None)
    for step in trials:
        fmt, blob, src = step()
        if fmt:
            record["source_url"] = src
            break

    if not fmt:
        record["notes"] = "Logo not found in sources."
        return record

    path = save_logo(brand, fmt, blob)
    if path and path.endswith(".svg"):
        record["saved_svg"] = path
    elif path and path.endswith(".png"):
        record["saved_png"] = path

    if record["saved_svg"] and not record["saved_png"]:
        try:
            import cairosvg
            svg_bytes = Path(record["saved_svg"]).read_bytes()
            png_bytes = cairosvg.svg2png(bytestring=svg_bytes)
            norm = png_normalize(png_bytes, size=1024, margin=64, remove_bg=False)
            p = OUT/"png"/f"{record['slug']}.png"
            Path(p).write_bytes(norm)
            record["saved_png"] = str(p)
        except Exception:
            pass

    if record["saved_png"]:
        record["hash"] = sha256_bytes(Path(record["saved_png"]).read_bytes())

    return record

def main(brands_csv=os.getenv("CSV_PATH","brands.csv")):
    meta = []
    path = Path(brands_csv)
    if not path.exists():
        raise SystemExit(f"brands CSV not found: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            brand = row["brand"].strip()
            if not brand:
                continue
            print(">>>", brand)
            rec = pipeline_for_brand(brand)
            meta.append(rec)
            time.sleep(0.25)
    (OUT/"metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Done. See logos/svg, logos/png and logos/metadata.json")

if __name__ == "__main__":
    main()
