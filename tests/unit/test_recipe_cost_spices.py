"""Cost-estimation spice gating + per-ingredient cost-per-serving.

Pins the behavior added for detailed per-serving costs: every priced ingredient
exposes ``cost_per_serving`` (price / servings), spices are shown but excluded
from the recipe total by default, and ``include_spices=True`` folds them back in.
Owner: recipe cost analytics (analytics/recipe_scoring.py).
"""

from kroger_mcp.analytics import recipe_scoring


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Returns a price row keyed by product_id; unknown ids resolve to None."""

    def __init__(self, price_by_pid: dict[str, float]):
        self._price_by_pid = price_by_pid

    def execute(self, _sql, params):
        pid = params[0]
        price = self._price_by_pid.get(pid)
        if price is None:
            return _FakeCursor(None)
        return _FakeCursor(
            {
                "regular_price": price,
                "sale_price": None,
                "on_sale": 0,
                "product_description": f"desc {pid}",
                "brand": "x",
                "observed_at": "2026-01-01",
                "location_id": "loc",
            }
        )


def _recipe():
    return {
        "id": "r1",
        "servings": 2,
        "ingredients": [
            {"name": "chicken breast", "product_id": "chk"},
            {"name": "cumin", "product_id": "cmn"},
        ],
    }


def _conn():
    return _FakeConn({"chk": 8.0, "cmn": 4.0})


def test_per_ingredient_cost_per_serving_is_price_over_servings():
    result = recipe_scoring._estimate_cost_with_conn(_recipe(), "loc", _conn())
    by_name = {e["ingredient"]: e for e in result["breakdown"]}
    assert by_name["chicken breast"]["cost_per_serving"] == 4.0  # 8.0 / 2
    assert by_name["cumin"]["cost_per_serving"] == 2.0  # 4.0 / 2 (still shown)


def test_spices_shown_but_excluded_from_total_by_default():
    result = recipe_scoring._estimate_cost_with_conn(_recipe(), "loc", _conn())
    cumin = next(e for e in result["breakdown"] if e["ingredient"] == "cumin")
    assert cumin["is_spice"] is True
    assert cumin["excluded_from_total"] is True
    assert cumin["price"] == 4.0  # priced and visible…
    assert result["total_cost"] == 8.0  # …but not summed
    assert result["cost_per_serving"] == 4.0
    assert result["confidence"] == "high"  # the one countable item is priced
    assert "spice" in (result["note"] or "")


def test_include_spices_folds_spice_into_total():
    result = recipe_scoring._estimate_cost_with_conn(_recipe(), "loc", _conn(), include_spices=True)
    cumin = next(e for e in result["breakdown"] if e["ingredient"] == "cumin")
    assert cumin["is_spice"] is True
    assert cumin["excluded_from_total"] is False
    assert result["total_cost"] == 12.0  # 8 + 4
    assert result["cost_per_serving"] == 6.0


def test_spice_only_recipe_has_no_countable_total():
    recipe = {"id": "r2", "servings": 2, "ingredients": [{"name": "cumin", "product_id": "cmn"}]}
    result = recipe_scoring._estimate_cost_with_conn(recipe, "loc", _conn())
    assert result["total_cost"] is None
    assert result["cost_per_serving"] is None
    assert result["confidence"] == "low"


def test_ingredient_is_spice_honors_category_tag():
    assert recipe_scoring._ingredient_is_spice({"name": "house blend", "category": "spice"})
    assert not recipe_scoring._ingredient_is_spice({"name": "chicken breast"})
