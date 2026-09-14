"""Objective checks for a round 3 site (task-r3.md). Uses no Claude tokens.

Usage: python3 check_r3.py <site-dir>
"""
from __future__ import annotations

import html as htmllib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import openpyxl

site = Path(sys.argv[1]).resolve()
XLSX = Path("/Users/georgiy/Projects/rin-website/assets/download_price_list.xlsx")
SKIP = {"assets", ".git", ".cc-delegate", "node_modules", "tools"}
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def text_of(h: str) -> str:
    h = re.sub(r"<(script|style)\b.*?</\1>", " ", h, flags=re.S | re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", h))).replace(" ", " ")


def norm(s: str) -> str:
    s = re.sub(r"\(.*?\)", " ", s.lower()).replace("ё", "е").replace("+", " и ")
    return " ".join(re.findall(r"[a-zа-я0-9]+", s))


def has_price(t: str, price: int) -> bool:
    pat = rf"{price // 1000}\s?{price % 1000:03d}" if price >= 1000 else str(price)  # 3 500 or 3500
    return re.search(rf"(?<!\d){pat}(?!\d)", t) is not None


def h1_text(h: str) -> str:
    m = re.search(r"<h1\b.*?</h1>", h, re.S | re.I)
    return text_of(m.group(0)) if m else ""


def has_duration(t: str, minutes: int) -> bool:
    return re.search(rf"(?<!\d){minutes}\s*(мин|min)", t, re.I) is not None


pages = sorted(p for p in site.rglob("*.html") if not SKIP & set(p.relative_to(site).parts))
src = {p: p.read_text("utf-8", errors="replace") for p in pages}
txt = {p: text_of(h) for p, h in src.items()}
rel = lambda p: str(p.relative_to(site))
ru = [p for p in pages if not rel(p).startswith("en/")]
en = [p for p in pages if rel(p).startswith("en/")]
ru_svc = [p for p in ru if rel(p).startswith("uslugi/")]
en_svc = [p for p in en if rel(p).startswith("en/services/")]

rows = list(openpyxl.load_workbook(XLSX, data_only=True).active.iter_rows(values_only=True))[1:]
services = [(r[1], r[2], int(r[7]), int(r[8]) if r[8] else None) for r in rows if r[2]]
names = sorted({n for _, n, _, _ in services})

# 1. structure
root = {rel(p) for p in ru if "/" not in rel(p)}
check("RU core pages (>= 8 at root incl. 404)", len(root) >= 8 and "404.html" in root, ", ".join(sorted(root)))
check(f"RU service pages (>= {len(names)} distinct services)", len(ru_svc) >= len(names), str(len(ru_svc)))
check("EN mirror (en/ pages >= RU pages)", len(en) >= len(ru) * 0.95, f"{len(en)} en vs {len(ru)} ru")
check(f"EN service pages (>= {len(names)})", len(en_svc) >= len(names), str(len(en_svc)))

# 2. content: every xlsx row on its RU service page, price and duration on some EN service page
miss = []
for cat, name, price, dur in services:
    toks = set(norm(name).split())
    hit = [p for p in ru_svc if toks <= set(norm(h1_text(src[p])).split())]
    if not any(has_price(txt[p], price) and (dur is None or has_duration(txt[p], dur)) for p in hit):
        miss.append(f"{name} {price}/{dur}")
check("RU service pages: name in h1, xlsx price and duration", not miss, f"{len(services) - len(miss)}/{len(services)} " + "; ".join(miss[:6]))
miss_en = [f"{n} {pr}/{d}" for _, n, pr, d in services
           if not any(has_price(txt[p], pr) and (d is None or has_duration(txt[p], d)) for p in en_svc)]
check("EN service pages: xlsx price and duration present", not miss_en, f"{len(services) - len(miss_en)}/{len(services)} " + "; ".join(miss_en[:6]))
listing = [p for p in ru if "/" not in rel(p) and sum(norm(n) in norm(txt[p]) for n in names) >= len(names) * 0.9]
check("RU price list page lists (nearly) all services", bool(listing), ", ".join(rel(p) for p in listing))
promos = ["Доброе утро", "Твой день", "Поделись заботой", "После заката", "Спасибо, мама"]
check("all 5 promos named", all(any(p.split(",")[0] in txt[q] for q in ru) for p in promos))
allsrc = " ".join(src.values())
facts = {"phone": "437-80-16", "address": "Олонецкая", "telegram": "t.me/Raz1Navsegda", "booking": "yclients"}
check("contacts present", all(v in allsrc for v in facts.values()), ", ".join(k for k, v in facts.items() if v not in allsrc))

# 3. booking form
forms = [p for p in pages if "<form" in src[p].lower()]
opts = {p: len(re.findall(r"<option\b", src[p], re.I)) + len(re.findall(r'type="radio"', src[p], re.I)) for p in forms}
check("booking form in RU and EN listing all services",
      any(opts[p] >= len(names) for p in forms if p in ru) and any(opts[p] >= len(names) for p in forms if p in en),
      ", ".join(f"{rel(p)}={n}" for p, n in opts.items()))

# 4. SEO
titles = {p: (re.search(r"<title>(.*?)</title>", src[p], re.S | re.I) or [None, ""])[1].strip() for p in pages}
dups = len(titles) - len(set(titles.values()))
check("unique non-empty <title> on every page", all(titles.values()) and dups == 0, f"{dups} duplicates")
check("meta description on every page", all('name="description"' in src[p] for p in pages),
      ", ".join(rel(p) for p in pages if 'name="description"' not in src[p])[:200])
check("Open Graph tags on every page", all('property="og:' in src[p] for p in pages))
paired = [p for p in pages if p.name != "404.html"]
check("hreflang on every page", all('hreflang=' in src[p] for p in paired),
      f"{sum('hreflang=' in src[p] for p in paired)}/{len(paired)}")
sm = site / "sitemap.xml"
locs = {urlparse(u).path.lstrip("/") for u in re.findall(r"<loc>(.*?)</loc>", sm.read_text("utf-8"))} if sm.exists() else set()
covered = [p for p in paired if rel(p) in locs or (p.name == "index.html" and rel(p)[:-len("index.html")] in locs)]
check("sitemap.xml covers every page", sm.exists() and len(covered) >= len(paired), f"{len(covered)}/{len(paired)}")
check("robots.txt present", (site / "robots.txt").exists())


def ld(p: Path) -> list:
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', src[p], re.S | re.I):
        try:
            out.append(json.loads(block))
        except ValueError:
            pass
    return out


home = site / "index.html"
check("JSON-LD for the business on the home page", home in src and any("Олонецкая" in json.dumps(x, ensure_ascii=False) for x in ld(home)))
check("parseable JSON-LD on every service page", all(ld(p) for p in ru_svc + en_svc),
      f"{sum(bool(ld(p)) for p in ru_svc + en_svc)}/{len(ru_svc + en_svc)}")

# 5. basics, links, weight
check("one <h1> per page", all(len(re.findall(r"<h1\b", src[p], re.I)) == 1 for p in pages),
      ", ".join(rel(p) for p in pages if len(re.findall(r"<h1\b", src[p], re.I)) != 1)[:200])
check("lang + viewport on every page", all(re.search(r"<html[^>]+lang=", src[p], re.I) and 'name="viewport"' in src[p] for p in pages))
check("every <img> has alt", all(re.search(r"<img(?![^>]*\balt=)[^>]*>", src[p], re.I) is None for p in pages))


def local(p: Path, ref: str) -> Path | None:
    u = urlparse(ref)
    if u.scheme or u.netloc or ref.startswith(("#", "mailto:", "tel:", "data:", "javascript:")) or not u.path:
        return None
    path = unquote(u.path)
    t = (site / path.lstrip("/")) if path.startswith("/") else (p.parent / path)
    return t / "index.html" if path.endswith("/") else t


missing, heavy = [], []
for p in pages:
    refs = re.findall(r'(?:src|href|poster)="([^"]+)"', src[p]) + \
        [c.strip().split()[0] for s in re.findall(r'srcset="([^"]+)"', src[p]) for c in s.split(",") if c.strip()]
    for r in refs:
        t = local(p, r)
        if t and not t.exists():
            missing.append(f"{rel(p)}:{r}")
    size = len(src[p].encode())
    autoplay = [r for v in re.findall(r"<video\b[^>]*autoplay[^>]*>.*?</video>", src[p], re.S | re.I)
                for r in re.findall(r'src="([^"]+)"', v)]
    size += sum(t.stat().st_size for r in autoplay if (t := local(p, r)) and t.is_file())
    for tag in re.findall(r"<(?:link|script|img)\b[^>]*>", src[p], re.I):  # non-autoplay video loads on demand
        if re.search(r'loading="lazy"|preload="none"', tag) or ("<link" in tag.lower() and "stylesheet" not in tag):
            continue
        m = re.search(r'(?:src|href)="([^"]+)"', tag)
        t = local(p, m.group(1)) if m else None
        if t and t.is_file() and t.suffix.lower() not in (".html",):
            size += t.stat().st_size
    if size > 2e6:
        heavy.append(f"{rel(p)}={size / 1e6:.1f}MB")
check("all local links/assets resolve", not missing, f"{len(missing)} missing: " + ", ".join(missing[:8]))
check("initial load under 2 MB per page (eager img/css/js/video)", not heavy, ", ".join(heavy[:8]))
big_auto = [f"{rel(p)}" for p in pages for v in re.findall(r"<video\b[^>]*autoplay[^>]*>.*?</video>", src[p], re.S | re.I)
            for r in re.findall(r'src="([^"]+)"', v) if (t := local(p, r)) and t.is_file() and t.stat().st_size > 2e6]
check("no autoplay video over 2 MB", not big_auto, ", ".join(big_auto[:5]))

# 6. the builder's own checker, and git hygiene
tool = site / "tools" / "check.py"
rc = subprocess.run([sys.executable, str(tool)], cwd=site, capture_output=True, text=True, timeout=300).returncode if tool.exists() else None
check("tools/check.py exists and exits 0", rc == 0, f"exit {rc}")
tracked = subprocess.run(["git", "-C", str(site), "ls-files"], capture_output=True, text=True).stdout.splitlines()
check("committed, assets/ not tracked", bool(tracked) and not any(f.startswith("assets/") for f in tracked), f"{len(tracked)} files")

for name, ok, detail in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
print(json.dumps({"passed": sum(ok for _, ok, _ in results), "total": len(results)}))
