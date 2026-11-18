import uuid

from app.modules.publishing import impact
from app.modules.websites.resolution import SiteRef

SITE = SiteRef(
    id=uuid.UUID(int=1),
    key="s",
    name="S",
    country_code="DE",
    default_locale="de",
    locales=("de", "en"),
    content_version=1,
    primary_host=None,
)
PID = uuid.UUID(int=2)


def test_product_change_covers_page_listing_and_fallback_locales() -> None:
    change = impact.product(SITE, PID, {"de": "stuhl"}, featured=False)
    assert change.paths == {"/products/stuhl", "/en/products/stuhl", "/products", "/en/products"}
    assert f"site:{SITE.id}:product:{PID}" in change.tags
    assert not any(":home:" in t for t in change.tags)
    assert not change.full_site


def test_featured_product_change_includes_home() -> None:
    change = impact.product(SITE, PID, {"de": "stuhl", "en": "chair"}, featured=True)
    assert {f"site:{SITE.id}:home:de", f"site:{SITE.id}:home:en"} <= change.tags
    assert {"/", "/en", "/en/products/chair"} <= change.paths


def test_page_changes() -> None:
    assert impact.content_page(SITE, "home", "en").paths == {"/en"}
    assert impact.content_page(SITE, "products", "en").full_site  # nav label
    assert impact.seo(SITE, "products", "de").tags == {f"site:{SITE.id}:products:de"}
    assert impact.whole_site(SITE, routing=True).tags == {f"site:{SITE.id}", "sites"}


def test_merge() -> None:
    merged = impact.home(SITE, ["de"]).merge(impact.whole_site(SITE))
    assert merged.full_site
    assert merged.paths == {"/"}
