"""Advanced analytics subsystem for ShopCRM"""

def build_analytics_router(**kwargs):
    # Lazy import keeps the query engine independently testable
    from .routes import build_analytics_router as factory
    return factory(**kwargs)

__all__ = ["build_analytics_router"]
