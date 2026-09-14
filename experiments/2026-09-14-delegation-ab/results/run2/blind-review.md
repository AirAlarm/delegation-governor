# Blind review, run 2 (verbatim reviewer report)

- **Setup:** a separate agent was given the brief, `assets/`, the Yandex URL and two copies named `site-1` / `site-2`. The mapping was random (`SystemRandom`), and the reviewer was told not to open the archives, git history or `~/.claude`.
- **Stripped from both copies:** `.git`, `.cc-delegate`, `.gitignore`, `BRIEF.md`, `README.md`, `tools/`. These are dev-only files, and some of them would reveal which arm built the site.
- **Mapping, revealed after the report:** site-1 = **arm B2** (Claude only), site-2 = **arm A2r** (delegated).
- **Checked against sources after the reveal:** both of site-2's BLOCKING items (`akcii.html:167`; the `uslugi.html` durations against xlsx rows) and site-1's missing yclients link and «отдельных кабинетах» claim (`uslugi.html:40`).

---

## Site 1 (arm B2): 8/10, would show the client

- **Prices:** 37/37 correct (name, price, duration). The 10 «популярное» badges match the xlsx flags. Depilation shows «от 500 ₽» where the xlsx says 500; its description says the price depends on the zone.
- **Promos:** 5/5 exact, nothing invented.
- **Contacts:** correct. The reviews section is Yandex's live widget.
- **BLOCKING:** none.
- **MAJOR:**
  - No online booking. Every «Записаться» button is a `tel:` link (`index.html:41,58,74`), though yclients `n2288691` exists.
  - «Все процедуры проходят в отдельных кабинетах» (`uslugi.html:40`, `index.html:135`) is unsupported, and the site's own photo 7215 shows shared workstations.
- **MINOR:**
  - «Лучше всего работают курсом» (`uslugi.html:79`) is an unsupported claim.
  - Eyebrow labels fail contrast (3.98:1 and 4.20:1).
  - The mobile menu icon doesn't change to a close state.
  - The home page autoplays a 1.9 MB video, about 2.5 MB total.
  - «м. Отрадное» is 1.5 km away.
  - The 10:00 opening time is unconfirmed.
- **Strengths:**
  - Every xlsx row is exact.
  - WebP thumbnails plus 1600 px lightbox images, self-hosted fonts, JSON-LD.
  - 30 interior photos, 19 service photos, 4 videos.
  - Yandex map and live reviews embedded.
  - No overflow at 375 px, no console errors.

## Site 2 (arm A2r): 6/10, wouldn't show the client until the blockers are fixed

- **Prices:** 37/37 correct, but only 28/37 rows fully correct.
  - **Wrong durations:**
    - Массаж стоп 30 мин / 1800 (xlsx 20 / 1800; the story says 30 / 2500)
    - Перкуссионный 60 мин / 3500 (xlsx 45 / 3500; the story says 60 / 2800)
    - Баночный 60 мин (xlsx 45; matches the story)
  - **Missing durations (6 rows):** Первый массаж, Массаж ног, Массаж рук, Медовый, Эндосфера + проблемные зоны, Миостимуляция.
- **Promos:** 5/5 terms exact, **plus one invented rule.**
- **Contacts:** correct, including the yclients link, the metro stations and the amenities.
- **BLOCKING:**
  1. «Привилегии не суммируются между собой» (`akcii.html:167`) is shown to visitors. It isn't in any story. The HTML comment at `:166` admits it is an unconfirmed assumption.
  2. Duration and price pairs that exist in no source: стоп 30/1800, перкуссионный 60/3500.
- **MAJOR:**
  - An altered Yandex review quote (`index.html:291`).
  - Six missing durations.
  - The gallery serves full 1000–1250 px JPGs as thumbnails (about 3 MB); the home page carries about 1.8 MB of images plus a 0.94 MB autoplay video.
- **MINOR:**
  - The discount pill has 2.15:1 contrast.
  - «вход слева» is a guess.
  - Fonts load from Google Fonts (152-ФЗ).
  - The home page shows only 3 of 5 promos.
  - The brows card uses an entrance-door photo, and depilation uses the shower photo.
  - The gallery uses only 9 of about 40 interior photos.
  - The hero video frame is dark.
- **Strengths:**
  - The most polished visuals (editorial hero, collage, amenity icons).
  - Correct booking link.
  - Good ARIA and focus handling, reduced-motion support.
  - No overflow at 375 px, no console errors.

## Head-to-head (reviewer)

| | Site 1 (B2) | Site 2 (A2r) |
|---|---|---|
| Rows fully correct vs xlsx | **37/37** | 28/37 |
| Promo terms | 5/5 | 5/5, plus an invented rule |
| Booking link | phone only | **yclients** |
| Invented or altered claims | «отдельные кабинеты» | «не суммируются», altered review quote |
| Weight | lighter | heavier |
| Visual polish | good, cozy | slightly more premium |

**The reviewer would ship Site 1.** It needs two small fixes: yclients links, and removing «отдельные кабинеты».

**Source conflicts to raise with the client:** the price story images disagree with the xlsx in 7 places, all 13 brows/lashes/depilation rows are marked «в наличии: нет», and the opening time is unconfirmed.
