"""Websites, domains, theme and publishing."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import select

from app.api.admin.deps import Ctx, IfMatch, TxtResolverDep, version_etag
from app.core.errors import Conflict, NotFound, PreconditionRequired
from app.modules.publishing.models import JobStatus, PublishJob
from app.modules.publishing.service import request_publish, retry_failed
from app.modules.websites import service
from app.modules.websites.models import Website, WebsiteDomain, WebsiteStatus
from app.modules.websites.schemas import (
    DomainCreate,
    DomainOut,
    DomainUpdate,
    PublishJobOut,
    ThemeOut,
    ThemeUpdate,
    WebsiteCreate,
    WebsiteOut,
    WebsiteUpdate,
)

router = APIRouter()
websites = APIRouter(prefix="/websites", tags=["admin: websites"])
domains = APIRouter(tags=["admin: domains"])
theme = APIRouter(prefix="/websites/{website_id}/theme", tags=["admin: theme"])


# --- websites -------------------------------------------------------------------------


@websites.get("", response_model=list[WebsiteOut], summary="Websites visible to the caller")
async def list_websites(ctx: Ctx) -> list[WebsiteOut]:
    return await service.list_websites(ctx)


@websites.post(
    "", response_model=WebsiteOut, status_code=status.HTTP_201_CREATED, summary="Create a website"
)
async def create_website(ctx: Ctx, body: WebsiteCreate) -> WebsiteOut:
    """Created as `draft` with a default theme. Add and verify a primary domain, then
    enable it."""
    website = await service.create_website(ctx, body)
    await ctx.commit()
    return await service.website_out(ctx.session, website)


@websites.get("/{website_id}", response_model=WebsiteOut)
async def get_website(ctx: Ctx, website_id: uuid.UUID) -> WebsiteOut:
    ctx.principal.require_site_read(website_id)
    website = await service.get_website(ctx.session, website_id)
    return await service.website_out(ctx.session, website)


@websites.patch("/{website_id}", response_model=WebsiteOut, summary="Update website settings")
async def update_website(ctx: Ctx, website_id: uuid.UUID, body: WebsiteUpdate) -> WebsiteOut:
    website = await service.get_website(ctx.session, website_id)
    await service.update_website(ctx, website, body)
    await ctx.commit()
    return await service.website_out(ctx.session, website)


@websites.post("/{website_id}/enable", response_model=WebsiteOut, summary="Make the site public")
async def enable_website(ctx: Ctx, website_id: uuid.UUID) -> WebsiteOut:
    website = await service.get_website(ctx.session, website_id)
    await service.set_website_status(ctx, website, WebsiteStatus.ACTIVE)
    await ctx.commit()
    return await service.website_out(ctx.session, website)


@websites.post("/{website_id}/disable", response_model=WebsiteOut, summary="Take the site down")
async def disable_website(ctx: Ctx, website_id: uuid.UUID) -> WebsiteOut:
    website = await service.get_website(ctx.session, website_id)
    await service.set_website_status(ctx, website, WebsiteStatus.DISABLED)
    await ctx.commit()
    return await service.website_out(ctx.session, website)


@websites.post(
    "/{website_id}/publish",
    response_model=PublishJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Regenerate the whole site now",
)
async def publish(ctx: Ctx, website_id: uuid.UUID) -> PublishJob:
    """Needed for sites with `auto_publish=false`; merges into a pending job if any."""
    ctx.principal.require_site_edit(website_id)
    website = await service.get_website(ctx.session, website_id)
    job = await request_publish(ctx.session, website.id)
    ctx.audit("website.publish", "website", website.id, website_id=website.id)
    await ctx.commit()
    return job


@websites.get(
    "/{website_id}/publish-jobs", response_model=list[PublishJobOut], summary="Recent jobs"
)
async def publish_jobs(
    ctx: Ctx, website_id: uuid.UUID, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[PublishJob]:
    ctx.principal.require_site_read(website_id)
    rows = await ctx.session.scalars(
        select(PublishJob)
        .where(PublishJob.website_id == website_id)
        .order_by(PublishJob.created_at.desc(), PublishJob.id.desc())
        .limit(limit)
    )
    return list(rows)


@router.post(
    "/publish-jobs/{job_id}/retry",
    response_model=PublishJobOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Requeue a failed publish job",
    tags=["admin: websites"],
)
async def retry_job(ctx: Ctx, job_id: uuid.UUID) -> PublishJob:
    job = await ctx.session.get(PublishJob, job_id)
    if job is None:
        raise NotFound("Publish job not found", code="job_not_found")
    ctx.principal.require_site_edit(job.website_id)
    if job.status is not JobStatus.FAILED:
        raise Conflict("Only failed jobs can be retried", code="job_not_failed")
    pending = await retry_failed(ctx.session, job)
    ctx.audit("publish_job.retry", "publish_job", job.id, website_id=job.website_id)
    await ctx.commit()
    return pending


# --- domains --------------------------------------------------------------------------


@domains.get("/websites/{website_id}/domains", response_model=list[DomainOut])
async def list_domains(ctx: Ctx, website_id: uuid.UUID) -> list[DomainOut]:
    ctx.principal.require_site_read(website_id)
    await service.get_website(ctx.session, website_id)
    return [service.domain_out(d) for d in await service.list_domains(ctx.session, website_id)]


@domains.post(
    "/websites/{website_id}/domains",
    response_model=DomainOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a domain (pending until verified)",
)
async def add_domain(ctx: Ctx, website_id: uuid.UUID, body: DomainCreate) -> DomainOut:
    """The response contains the DNS TXT record to create for verification. The first
    domain of a website becomes its primary domain."""
    website = await service.get_website(ctx.session, website_id)
    domain = await service.add_domain(ctx, website, body)
    await ctx.commit()
    return service.domain_out(domain)


async def _domain_and_site(ctx: Ctx, domain_id: uuid.UUID) -> tuple[WebsiteDomain, Website]:
    domain = await service.get_domain(ctx.session, domain_id)
    ctx.principal.require_site_read(domain.website_id)
    website = await service.get_website(ctx.session, domain.website_id)
    return domain, website


@domains.patch("/domains/{domain_id}", response_model=DomainOut)
async def update_domain(ctx: Ctx, domain_id: uuid.UUID, body: DomainUpdate) -> DomainOut:
    domain, website = await _domain_and_site(ctx, domain_id)
    await service.update_domain(ctx, website, domain, body)
    await ctx.commit()
    return service.domain_out(domain)


@domains.delete("/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_domain(ctx: Ctx, domain_id: uuid.UUID) -> None:
    domain, website = await _domain_and_site(ctx, domain_id)
    await service.delete_domain(ctx, website, domain)
    await ctx.commit()


@domains.post(
    "/domains/{domain_id}/verify",
    response_model=DomainOut,
    summary="Check the DNS TXT record and activate the domain (admin)",
    responses={412: {"description": "TXT record missing or wrong"}},
)
async def verify_domain(
    ctx: Ctx,
    domain_id: uuid.UUID,
    resolver: TxtResolverDep,
    force: Annotated[bool, Query(description="Skip the DNS check")] = False,
) -> DomainOut:
    domain, website = await _domain_and_site(ctx, domain_id)
    await service.verify_domain(ctx, website, domain, resolver, force=force)
    await ctx.commit()
    return service.domain_out(domain)


@domains.post("/domains/{domain_id}/make-primary", response_model=DomainOut)
async def make_primary(ctx: Ctx, domain_id: uuid.UUID) -> DomainOut:
    domain, website = await _domain_and_site(ctx, domain_id)
    await service.make_primary(ctx, website, domain)
    await ctx.commit()
    return service.domain_out(domain)


# --- theme ----------------------------------------------------------------------------


@theme.get("", response_model=ThemeOut, summary="Theme tokens (ETag = version)")
async def get_theme(ctx: Ctx, website_id: uuid.UUID, response: Response) -> ThemeOut:
    ctx.principal.require_site_read(website_id)
    website = await service.get_website(ctx.session, website_id)
    row = await service.get_theme(ctx.session, website)
    await ctx.session.commit()  # persists a lazily created default theme
    response.headers["ETag"] = version_etag(row.version)
    return ThemeOut.model_validate(row)


@theme.put(
    "",
    response_model=ThemeOut,
    summary="Replace theme tokens",
    responses={
        412: {"description": "Version mismatch (someone else saved first)"},
        428: {"description": "If-Match header missing"},
    },
)
async def put_theme(
    ctx: Ctx, website_id: uuid.UUID, body: ThemeUpdate, if_match: IfMatch, response: Response
) -> ThemeOut:
    """Requires `If-Match: "<version>"` from the last GET."""
    if if_match is None:
        raise PreconditionRequired('Send If-Match: "<version>"', code="if_match_required")
    website = await service.get_website(ctx.session, website_id)
    row = await service.update_theme(ctx, website, body.tokens, if_match=if_match)
    await ctx.commit()
    response.headers["ETag"] = version_etag(row.version)
    return ThemeOut.model_validate(row)


router.include_router(websites)
router.include_router(domains)
router.include_router(theme)
