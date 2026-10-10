"""Business rule lookup with the defaults from Exhibit #4.

Every threshold the system applies is read from here, so the System
Administrator can change it without a code change (FR-52, NFR-20).
"""
from decimal import Decimal

# key: (name, default, unit, min, max, description)
DEFAULT_RULES = {
    "near_expiry_days": ("Near-expiry window", 180, "days", 30, 730,
                         "A batch with this many days or less to expiry is Near-Expiry."),
    "price_band_min_pct": ("Clearance price band floor", 65, "% of original", 1, 100,
                           "Lowest clearance price as a share of the original sale price."),
    "price_band_max_pct": ("Clearance price band ceiling", 70, "% of original", 1, 100,
                           "Highest clearance price, also the proposed default."),
    "min_sellable_days": ("Minimum sellable shelf life", 30, "days", 0, 365,
                          "Listings are withdrawn and allocation stops at this shelf life."),
    "min_receiving_months": ("Minimum receiving shelf life warning", 12, "months", 0, 60,
                             "QA is warned when a received batch has less shelf life."),
    "escalation_days": ("Unactioned near-expiry escalation", 14, "days", 1, 90,
                        "Near-expiry batches with no action are escalated to the GM."),
    "variance_tolerance_pct": ("Count variance tolerance", 2, "% of batch qty", 0, 100,
                               "Count variances above this are flagged for reconciliation."),
    "adjustment_threshold_egp": ("Adjustment approval threshold", 10000, "EGP at cost", 0,
                                 10000000,
                                 "Adjustments at or above this value need GM approval."),
    "reservation_hold_hours": ("Platform order reservation hold", 48, "hours", 1, 240,
                               "Unpaid or unapproved orders are cancelled after this hold."),
    "coverage_months": ("Purchase plan coverage period", 3, "months", 1, 24,
                        "Months of demand a purchase plan must cover after lead time."),
    "safety_stock_days": ("Safety stock", 15, "days of demand", 0, 180,
                          "Safety stock added to the recommended order quantity."),
    "mape_low_confidence_pct": ("Low-confidence forecast MAPE", 30, "%", 1, 100,
                                "Forecasts with a higher MAPE are flagged low confidence."),
    "registration_alert_days": ("Registration expiry alert", 60, "days", 1, 365,
                                "Regulatory Affairs is alerted this long before expiry."),
    "licence_alert_days": ("Buyer licence expiry alert", 30, "days", 1, 365,
                           "Regulatory Affairs is alerted this long before a licence expires."),
    "rejected_doc_retention_days": ("Rejected document retention", 90, "days", 1, 365,
                                    "Documents of rejected applications are deleted after this."),
    "max_failed_logins": ("Failed logins before lock", 5, "attempts", 3, 10,
                          "Accounts lock after this many consecutive failures."),
}


def rule(key):
    """Return the current value of a business rule as a Decimal."""
    from .models import BusinessRule

    try:
        return BusinessRule.objects.only("value").get(key=key).value
    except BusinessRule.DoesNotExist:
        return Decimal(DEFAULT_RULES[key][1])


def rule_int(key):
    return int(rule(key))


def ensure_rules():
    """Create any missing business rules with their default values."""
    from .models import BusinessRule

    for key, (name, default, unit, lo, hi, desc) in DEFAULT_RULES.items():
        BusinessRule.objects.get_or_create(
            key=key,
            defaults=dict(name=name, value=Decimal(default), unit=unit, min_value=lo,
                          max_value=hi, description=desc),
        )
