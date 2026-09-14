"""Objective quality checks for one arm's site. Uses no Claude tokens.

Usage: python3 check_site.py <site-dir>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import openpyxl

site = Path(sys.argv[1])
assets = Path("/Users/georgiy/Projects/rin-website/assets")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


pages = sorted(p for p in site.rglob("*.html") if "assets" not in p.parts and ".cc-delegate" not in p.parts)
html = {p: p.read_text("utf-8", errors="replace") for p in pages}
text_all = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(html.values()))).replace("&nbsp;", " ")
check("at least 4 pages", len(pages) >= 4, ", ".join(str(p.relative_to(site)) for p in pages))
check("Акции page present", any("Акци" in h for h in html.values()))

missing = []
for p, h in html.items():
    for ref in re.findall(r'(?:src|href|poster)="([^"#?:]+)"', h):
        if not (p.parent / ref).exists():
            missing.append(f"{p.name}:{ref}")
check("all local links/images resolve", not missing, ", ".join(missing[:10]))
check("one <h1> per page", all(h.count("<h1") == 1 for h in html.values()),
      ", ".join(f"{p.name}={h.count('<h1')}" for p, h in html.items()))
check("viewport meta on every page", all('name="viewport"' in h for h in html.values()))
check("every <img> has alt", all(re.search(r"<img(?![^>]*\balt=)[^>]*>", h) is None for h in html.values()))

rows = list(openpyxl.load_workbook(assets / "download_price_list.xlsx", data_only=True).active.iter_rows(values_only=True))[1:]
names = [(r[2], int(r[7])) for r in rows if r[2]]
# Prices may be rendered by JS from a data file (run 1, arm B), so read .js/.json too.
data_files = [p for p in site.rglob("*") if p.suffix in (".js", ".json") and p.is_file()
              and not {"assets", ".git", ".cc-delegate", "node_modules"} & set(p.parts)]
data_text = " ".join(re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)),
                            p.read_text("utf-8", errors="replace")) for p in data_files)
price_text = (text_all + " " + data_text).replace("\u00a0", " ").replace("\u202f", " ")
priced = [n for n, pr in names if n in price_text and re.search(rf"{pr // 1000}[  ]?{pr % 1000:03d}|{pr}", price_text)]
check("price list: services with name and price present", len(priced) >= len(names) - 2, f"{len(priced)}/{len(names)}")

promos = ["Доброе утро", "Твой день", "Поделись заботой", "После заката", "Спасибо, мама"]
check("all 5 promos named", all(p.split(",")[0] in text_all for p in promos),
      ", ".join(p for p in promos if p.split(",")[0] not in text_all))
facts = {"phone": "437-80-16", "address": "Олонецкая", "telegram": "t.me/Raz1Navsegda", "booking": "yclients"}
check("contacts present", all(v in " ".join(html.values()) for v in facts.values()),
      ", ".join(k for k, v in facts.items() if v not in " ".join(html.values())))

size = sum(f.stat().st_size for f in site.rglob("*") if f.is_file() and "assets" not in f.parts and ".git" not in f.parts and ".cc-delegate" not in f.parts)
check("site weight under 25 MB (no raw PSD/video dumps)", size < 25e6, f"{size / 1e6:.1f} MB")

for name, ok, detail in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
print(json.dumps({"passed": sum(ok for _, ok, _ in results), "total": len(results)}))
