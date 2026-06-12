"""Tests for the price-per-unit helper that powers the linking UI's $/oz line.

Owner: web/routes/api/products.py. Failure here means the comparison surface
in the recipe-linking dropdown loses its primary value signal.
"""

from kroger_mcp.web.routes.api._product_extract import (
    _compute_price_per_unit,
    _pick_image_url,
)


class TestComputePricePerUnit:
    def test_simple_ounces(self):
        result = _compute_price_per_unit("16 oz", 4.00)
        assert result == {"amount": 0.25, "unit": "oz", "label": "$0.25/oz"}

    def test_pounds_normalized_to_oz(self):
        result = _compute_price_per_unit("1 lb", 8.00)
        assert result["unit"] == "oz"
        assert result["amount"] == 0.5
        assert result["label"] == "$0.50/oz"

    def test_decimal_pounds(self):
        result = _compute_price_per_unit("1.5 lb", 12.00)
        assert result["unit"] == "oz"
        assert abs(result["amount"] - 0.5) < 0.01

    def test_gallon_to_fl_oz(self):
        # $12.80 / 128 fl oz = exactly $0.10/fl oz — uses dollar formatting at the threshold
        result = _compute_price_per_unit("1 gal", 12.80)
        assert result["unit"] == "fl oz"
        assert result["amount"] == 0.1
        assert result["label"] == "$0.10/fl oz"

    def test_count_each(self):
        result = _compute_price_per_unit("12 ct", 6.00)
        assert result == {"amount": 0.5, "unit": "each", "label": "$0.50/each"}

    def test_multipack_multiplies(self):
        # 6 × 12 oz = 72 oz; $9 / 72 = $0.125/oz
        result = _compute_price_per_unit("6 x 12 oz", 9.00)
        assert result["unit"] == "oz"
        assert abs(result["amount"] - 0.125) < 0.001

    def test_milliliters_convert_to_fl_oz(self):
        # 750 ml ≈ 25.36 fl oz; $10 / 25.36 ≈ $0.394
        result = _compute_price_per_unit("750 ml", 10.00)
        assert result["unit"] == "fl oz"
        assert abs(result["amount"] - 0.394) < 0.01

    def test_unparseable_size_returns_none(self):
        assert _compute_price_per_unit("medium", 3.00) is None
        assert _compute_price_per_unit("variety pack", 5.00) is None

    def test_missing_inputs_return_none(self):
        assert _compute_price_per_unit(None, 3.00) is None
        assert _compute_price_per_unit("16 oz", None) is None
        assert _compute_price_per_unit("16 oz", 0.0) is None
        assert _compute_price_per_unit("16 oz", -1.0) is None

    def test_zero_quantity_returns_none(self):
        assert _compute_price_per_unit("0 oz", 5.00) is None

    def test_cents_label_under_ten_cents(self):
        # 128 fl oz at $5 → $0.039/fl oz → 3.9¢/fl oz
        result = _compute_price_per_unit("1 gal", 5.00)
        assert "¢" in result["label"]


class TestPickImageUrl:
    def test_picks_front_medium(self):
        images = [
            {"perspective": "back", "sizes": [{"size": "large", "url": "back-large"}]},
            {"perspective": "front", "sizes": [{"size": "medium", "url": "front-medium"}]},
        ]
        assert _pick_image_url(images) == "front-medium"

    def test_prefers_medium_over_large(self):
        images = [
            {"perspective": "front", "sizes": [{"size": "large", "url": "large"}]},
            {"perspective": "front", "sizes": [{"size": "medium", "url": "medium"}]},
        ]
        assert _pick_image_url(images) == "medium"

    def test_empty_returns_none(self):
        assert _pick_image_url(None) is None
        assert _pick_image_url([]) is None

    def test_skips_non_front_perspective(self):
        images = [{"perspective": "back", "sizes": [{"size": "medium", "url": "back-medium"}]}]
        assert _pick_image_url(images) is None

    def test_allows_unmarked_perspective(self):
        # When perspective is missing, treat as candidate (some payloads omit it).
        images = [{"sizes": [{"size": "medium", "url": "no-perspective"}]}]
        assert _pick_image_url(images) == "no-perspective"
