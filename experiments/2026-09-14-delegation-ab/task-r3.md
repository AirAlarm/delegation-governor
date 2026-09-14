In /Users/georgiy/Projects/rin-website there is an `assets/` folder for a beauty salon «Раз и навсегда» — a massage and visage (face/body aesthetics) co-working space in Moscow. Yandex Maps listing for reference info: https://yandex.ru/maps/org/raz_i_navsegda/4076441567/?ll=37.621139%2C55.855406&z=16

Build the salon's complete, production-ready website from these materials. It is a static site: plain HTML/CSS/JS with no framework, and it must work when served as plain files, with no build step needed to view it. You may write helper scripts that generate pages, but commit their output.

Scope:
1. **Russian site (at the root):** a modern but cozy landing page, an «Акции» page, a services and prices page (all services, grouped by category), a gallery page (photos and videos), a contacts page (address, how to get there, hours, phone, messengers, online booking link, embedded map), an FAQ page and a 404 page.
2. **Service pages under `uslugi/`:** one page per service in the price list xlsx. A service offered in several durations gets one page listing each option. Each page shows name, price, duration, description, a relevant photo where one exists, a booking call to action and links to the other services in its category.
3. **English version under `en/`:** a counterpart of every page, with service pages under `en/services/`. Add a language switch and `hreflang` links between counterparts.
4. **Booking request page, in Russian and English:** a form to pick a service (all services, showing price and duration), a preferred date and time, name and phone, with client-side validation. There is no backend. On submit, hand the request off through a channel the salon already uses (its online booking or a messenger), with the details pre-filled where the channel allows.
5. **SEO and sharing:** a unique title and meta description on every page, Open Graph tags, a `sitemap.xml` covering every page, `robots.txt`, and JSON-LD for the business and for each service page.
6. **Performance and accessibility:**
   - responsive, optimised images;
   - no autoplaying video over 2 MB;
   - each page's initial load under 2 MB;
   - one `<h1>` per page, alt text on images, keyboard-usable navigation, WCAG AA contrast.
7. **`tools/check.py`** (Python 3 standard library only): check that every internal link and asset resolves, and that every service's price and duration on the site match the xlsx. It must exit 0 on the finished site.

Content rules:
- **The xlsx is the source of truth** for service names, prices and durations. The price story images disagree with it in places: use the xlsx and list the conflicts as open questions.
- **Use only facts from `assets/` and the Yandex listing.** Don't invent promotion terms or rules, amenities, claims, reviews or quotes. If something is unknown, leave it out and list it as an open question.

Work autonomously to a finished, self-reviewed site. Don't ask me questions: make reasonable decisions and list open questions at the end. When done, commit the result in rin-website (git init it; keep `assets/` out of git) and finish with a short summary.
