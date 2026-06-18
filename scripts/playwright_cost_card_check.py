"""Browser test for the recipe cost card (per-ingredient cost-per-serving +
spice exclusion). Drives the running web server with an injected session cookie,
verifies default (spices excluded) and ?include_spices=1 (spices folded in)
states, and saves screenshots. Run with the web server up on the given base URL.
"""

import re
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8099"
with open("/tmp/ss_token.txt") as _f:
    TOKEN = _f.read().strip()
RECIPE_ID = "af6523bb"  # Coconut Curry Chicken — 11 spices + 6 priced non-spices
OUT = "output/playwright-cost-card"


def _money(text: str):
    m = re.search(r"\$([0-9]+\.[0-9]{2})", text)
    return float(m.group(1)) if m else None


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context()
        ctx.add_cookies(
            [
                {
                    "name": "kroger_session",
                    "value": TOKEN,
                    "domain": "127.0.0.1",
                    "path": "/",
                }
            ]
        )
        page = ctx.new_page()
        failures = []

        def _dismiss_overlays():
            # A consent-gate modal (unrelated to this feature) can intercept
            # clicks; hide any modal overlays so the card is interactable/visible.
            page.evaluate(
                "() => document.querySelectorAll('.ss-modal-overlay').forEach(e => e.style.display='none')"
            )

        # ---- Default state: spices shown but excluded ----
        page.goto(f"{BASE}/recipes/{RECIPE_ID}", wait_until="networkidle")
        _dismiss_overlays()
        card = page.locator("div.ss-card").filter(has=page.get_by_role("heading", name="Cost"))
        if card.count() == 0:
            print("FAIL: cost card not found on page")
            return 1
        card = card.first
        card.scroll_into_view_if_needed()
        page.screenshot(path=f"{OUT}/01-default-excluded.png", full_page=True)
        card.screenshot(path=f"{OUT}/01-default-card.png")

        summary = card.locator("xpath=./div[1]").inner_text()
        default_per_serving = _money(summary.split("/ serving")[0])
        default_total = _money(summary.split("·")[1]) if "·" in summary else None
        print(f"default summary: {summary!r} -> per_serving={default_per_serving} total={default_total}")

        n_excluded = card.get_by_text("spice, not counted").count()
        print(f"default: {n_excluded} rows marked 'spice, not counted'")
        if n_excluded == 0:
            failures.append("expected spice rows marked 'not counted' in default state, found 0")

        n_per_srv = card.get_by_text(re.compile(r"/srv")).count()
        print(f"default: {n_per_srv} rows show per-ingredient $/srv")
        if n_per_srv == 0:
            failures.append("expected per-ingredient '$/srv' values, found 0")

        # spice rows should render muted (var(--text-3)) vs normal rows (var(--text-1))
        norm_row = card.locator("xpath=.//div[contains(@style,'--text-1')]").first
        spice_row = card.locator(
            "xpath=.//div[contains(@style,'--text-3') and contains(., 'not counted')]"
        ).first
        norm_color = norm_row.evaluate("el => getComputedStyle(el).color") if norm_row.count() else None
        spice_color = spice_row.evaluate("el => getComputedStyle(el).color") if spice_row.count() else None
        print(f"normal row color={norm_color}  spice row color={spice_color}")
        if norm_color and spice_color and norm_color == spice_color:
            failures.append("spice row color is NOT visually distinct from normal rows")

        # ---- Toggle: include spices (the toggle is a plain link; assert its
        # href wires the right URL, then follow it server-side) ----
        toggle = card.get_by_role("link", name="Include spices in total")
        if toggle.count() == 0:
            failures.append("'Include spices in total' toggle link missing in default state")
        else:
            href = toggle.first.get_attribute("href")
            print(f"toggle href: {href}")
            if "include_spices=1" not in (href or ""):
                failures.append(f"toggle href does not enable spices: {href}")
            page.goto(f"{BASE}{href}", wait_until="networkidle")
            _dismiss_overlays()

        card2 = page.locator("div.ss-card").filter(
            has=page.get_by_role("heading", name="Cost")
        ).first
        card2.scroll_into_view_if_needed()
        page.screenshot(path=f"{OUT}/02-included.png", full_page=True)
        card2.screenshot(path=f"{OUT}/02-included-card.png")

        summary2 = card2.locator("xpath=./div[1]").inner_text()
        incl_total = _money(summary2.split("·")[1]) if "·" in summary2 else None
        incl_per_serving = _money(summary2.split("/ serving")[0])
        print(f"included summary: {summary2!r} -> per_serving={incl_per_serving} total={incl_total}")

        n_excluded2 = card2.get_by_text("spice, not counted").count()
        print(f"included: {n_excluded2} rows marked 'spice, not counted' (expect 0)")
        if n_excluded2 != 0:
            failures.append(f"expected 0 'not counted' rows when spices included, found {n_excluded2}")

        if default_total is not None and incl_total is not None and not (incl_total > default_total):
            failures.append(
                f"including spices should raise total: default={default_total} included={incl_total}"
            )

        back = card2.get_by_role("link", name="Exclude spices from total")
        if back.count() == 0:
            failures.append("'Exclude spices from total' toggle missing in included state")

        # ---- List card: rendered $/srv pill reflects spice-excluded cost ----
        page.goto(f"{BASE}/recipes", wait_until="networkidle")
        _dismiss_overlays()
        name = "Coconut Curry Chicken"
        # The card is the anchor linking to this recipe's detail page.
        card_list = page.locator(f'a[href="/recipes/{RECIPE_ID}"]')
        card_list.first.scroll_into_view_if_needed()
        list_text = card_list.first.inner_text() if card_list.count() else ""
        m = re.search(r"\$([0-9]+\.[0-9]{2})/srv", list_text)
        list_cost = float(m.group(1)) if m else None
        print(f"list card rendered cost pill for {name!r}: {list_cost}")
        page.screenshot(path=f"{OUT}/03-list-card.png", full_page=False)
        if list_cost is None:
            failures.append("list card: no $/srv pill rendered for the recipe")
        elif abs(list_cost - 8.82) < 0.01:
            failures.append("list card shows spices-INCLUDED $8.82/srv; expected spice-excluded")
        elif abs(list_cost - 7.08) >= 0.01:
            failures.append(f"list card $/srv {list_cost} != expected spice-excluded 7.08")

        browser.close()

        print("\n=== RESULT ===")
        if failures:
            for f in failures:
                print("FAIL:", f)
            return 1
        print("PASS: cost card renders per-ingredient $/srv, mutes excluded spices,")
        print(f"      toggle folds spices in (total {default_total} -> {incl_total}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
