"""Safety domain models: status enums and the per-product status dataclass."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..ingredients import SafetyResult


class SafetyStatus(str, Enum):
    """Overall safety status for a product."""

    SAFE = "safe"  # On safe list (explicitly approved)
    EXCELLENT = "excellent"  # Score 90-100: premium quality markers
    GOOD = "good"  # Score 75-89: clean product with bonuses
    ACCEPTABLE = "acceptable"  # Score 60-74: no concerns detected
    POOR = "poor"  # Score 45-59: watch-level concerns
    AVOID = "avoid"  # Score 0-44: critical/warning ingredients
    BLOCKED = "blocked"  # On blocked list (explicitly blocked)


class BlockMode(str, Enum):
    """How to handle flagged products."""

    SOFT = "soft"  # Warn but allow with confirmation
    HARD = "hard"  # Hide from search, block cart additions
    WARN_ONLY = "warn_only"  # Just show warnings, no blocking


@dataclass
class ProductSafetyStatus:
    """Complete safety status for a product."""

    product_id: str
    status: SafetyStatus
    is_safe_listed: bool
    is_blocked: bool
    blocked_reason: str | None
    safety_result: SafetyResult | None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "product_id": self.product_id,
            "safety_status": self.status.value,
            "is_safe_listed": self.is_safe_listed,
            "is_blocked": self.is_blocked,
        }
        if self.is_blocked and self.blocked_reason:
            result["blocked_reason"] = self.blocked_reason
        if self.safety_result:
            sr = self.safety_result.to_dict()
            result["safety_score"] = sr["score"]
            result["safety_grade"] = sr["grade"]
            result["positive_attributes"] = sr["positive_attributes"]
            result["flagged_ingredients"] = sr["flagged_ingredients"]
        else:
            result["safety_score"] = None
            result["safety_grade"] = None
            result["positive_attributes"] = []
            result["flagged_ingredients"] = []
        return result
