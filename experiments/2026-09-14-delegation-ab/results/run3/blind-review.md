# Blind review, round 3 (reviewer report, condensed)

- **Setup:** a separate agent got the round 3 brief, `assets/`, the Yandex listing and two copies named `site-1` / `site-2`. The mapping was random (`SystemRandom`).
- **Stripped from both copies:** `.git`, `.cc-delegate`, `.gitignore`, `README.md`, `docs/`, `__pycache__`. Also removed: a `".cc-delegate"` skip entry in `tools/check.py`, which would have revealed the arm.
- **Added:** `assets/` symlinked into each copy, so each `tools/check.py` can find the xlsx.
- **Mapping, revealed after the report:** site-1 = **arm A3** (delegated), site-2 = **arm B3** (Claude only).
- **Checked against the archives after the reveal:** both of A3's BLOCKING items. `data/site.json:3` has `site_url: https://raz-i-navsegda.ru`, introduced in A3's own "Foundation" commit. The xlsx «…без окрашивания» row is described «с окрашиванием».
- **Not seen by the reviewer:** A3's final summary listed both as open questions (placeholder domain; contradictory xlsx descriptions).

## Shared results

| Location | A3 | B3 |
|---|---|---|
| RU service pages | 37/37 | 37/37 |
| EN service pages | 37/37 | 37/37 |
| RU / EN price list | 37/37 | 37/37 |
| Booking dropdown (RU/EN) | 37 correct | 37 correct |
| Service JSON-LD prices | 68/68 | 68/68 |
| ₽ amounts not in the xlsx | none | none |

- **Story-image conflicts:** resolved in favour of the xlsx on both sites.
- **Hreflang pairs:** correct on both, including all 34 service pages.
- **Promos:** all five exact in RU, and the EN versions keep the meaning.
- **Contacts, hours, amenities and FAQ facts:** match Yandex on both.

## Site 1 = A3 (delegated): 6.5/10, wouldn't ship as-is

- **BLOCKING:**
  1. `site_url` `https://raz-i-navsegda.ru` appears in 87 files (canonical, og, hreflang, sitemap, robots, JSON-LD). The reviewer found that domain is live and belongs to an unrelated pirate-movie site, and that nothing in the sources supports it.
  2. The brow lamination pages «без окрашивания» and «без коррекции» are described as the opposite («с окрашиванием», «…и коррекцию») in RU, EN and JSON-LD. The descriptions were copied from contradictory xlsx cells.
- **MAJOR:**
  - `tools/check.py` only checks prices that have data attributes; a stray «2 900 ₽» paragraph still passed.
  - The EN booking message is English-only, so the administrator gets no Russian service name.
- **MINOR:**
  - Hard-coded Yandex rating.
  - Empty «Другие услуги категории» section on the depilation page.
  - Rough price list: duplicate rows, no descriptions.
  - Awkward EN labels ("Lash removal", "Hardware massage").
  - Fewer photos than available.
  - Menu flashes before JS loads.
  - Tiny header logo.
- **Strengths:**
  - RU/EN booking works: validation, Telegram deep link with every field, clipboard copy, pre-selection from service pages.
  - Promo story images shown next to their text.
  - Lightbox with focus return.
  - No console errors or broken links.
  - AA contrast.
  - Reproducible build.

## Site 2 = B3 (Claude only): 8/10, would ship once the domain is set

- **BLOCKING:** none.
- **MAJOR:**
  - Placeholder domain `raz-i-navsegda.example`, clearly marked "replace and rebuild".
  - The two contradictory brow pages have no description at all. The contradiction isn't published, but the brief asked for a description.
- **MINOR:**
  - «Время визита подтвердит администратор» is not in the sources.
  - The EN copy keeps the brand in Cyrillic.
  - FAQ answers «Какие есть акции?» with «Да: …».
  - The service page's «Записаться» always pre-selects 60 min.
  - `check.py` needs `assets/`, so it fails in a clean clone.
  - The lightbox doesn't return focus.
  - Mobile logo sits 3 px from the top edge.
- **Strengths:**
  - Booking hides start times that would run past 22:00.
  - The EN Telegram message is bilingual.
  - Both xlsx descriptions are shown per duration.
  - Better EN labels.
  - The strongest `check.py`: it catches stray prices.
  - More polished design.
  - The map loads only on click.
  - Lighter pages (heaviest 0.56 MB).

## Head-to-head

| | A3 | B3 |
|---|---|---|
| Prices and durations (all locations) | 37/37 everywhere | 37/37 everywhere |
| Offer terms and contacts | exact | exact |
| Invented / unsafe | domain of an unrelated live site | "administrator confirms"; placeholder domain |
| xlsx self-contradiction | published | withheld |
| Booking | works, EN message English-only | works, bilingual, aware of closing time |
| `check.py` depth | misses stray prices | catches stray prices |
| Design | decent | more polished |
| **Score** | **6.5/10** | **8/10** |
