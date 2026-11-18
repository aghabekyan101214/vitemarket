"""Model registry: import every module's models so Alembic autogenerate and the mapper see them."""

from app.core.models import Base
from app.modules.audit.models import AuditLog
from app.modules.auth.models import AdminRole, AdminUser, AdminUserWebsite
from app.modules.catalog.models import (
    Product,
    ProductImage,
    ProductStatus,
    ProductTranslation,
    WebsiteProduct,
)
from app.modules.content.models import PageType, SeoMetadata, WebsiteContent
from app.modules.media.models import DeletedMedia
from app.modules.publishing.models import JobStatus, PublishJob
from app.modules.websites.models import (
    DomainStatus,
    Website,
    WebsiteDomain,
    WebsiteStatus,
    WebsiteTheme,
)

__all__ = [
    "AdminRole",
    "AdminUser",
    "AdminUserWebsite",
    "AuditLog",
    "Base",
    "DeletedMedia",
    "DomainStatus",
    "JobStatus",
    "PageType",
    "Product",
    "ProductImage",
    "ProductStatus",
    "ProductTranslation",
    "PublishJob",
    "SeoMetadata",
    "Website",
    "WebsiteContent",
    "WebsiteDomain",
    "WebsiteProduct",
    "WebsiteStatus",
    "WebsiteTheme",
]
