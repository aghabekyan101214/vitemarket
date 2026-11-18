"""Operational commands.

python -m app.cli seed                          # dev data + dev admin (refused in production)
python -m app.cli create-admin --email a@b.com --role admin
python -m app.cli create-admin --email e@b.com --role site_editor --website nordic-home-de
echo "$PW" | python -m app.cli create-admin --email a@b.com --role admin --password-stdin
python -m app.cli publish-all                       # queue a full publish of every site
python -m app.cli openapi > openapi.json            # spec for frontend client generation
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import Database
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.modules.auth.models import AdminRole
from app.modules.auth.service import create_admin_user

log = get_logger("app.cli")


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    if not sys.stdin.isatty():
        raise SystemExit("No TTY for a password prompt; use --password-stdin")
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match")
    return password


async def _create_admin(args: argparse.Namespace) -> None:
    password = _read_password(args)
    db = Database(get_settings(), application_name="showcase-cli")
    try:
        async with db.sessionmaker() as session:
            user, created = await create_admin_user(
                session,
                email=args.email,
                password=password,
                role=AdminRole(args.role),
                website_keys=args.website or [],
                reset_password_if_exists=args.reset_password,
            )
            await session.commit()
        print(f"{'Created' if created else 'Updated'} admin {user.email} ({user.role.value})")
    finally:
        await db.dispose()


async def _seed(args: argparse.Namespace) -> None:
    from app.core.storage import S3Storage
    from app.seed import DEV_ADMIN_EMAIL, DEV_ADMIN_PASSWORD, seed, seed_images

    settings = get_settings()
    if settings.is_production:
        raise SystemExit("Refusing to seed demo data in production")

    db = Database(settings, application_name="showcase-cli")
    try:
        async with db.sessionmaker() as session:
            summary = await seed(session)
            images_note = ""
            if not args.no_images:
                try:
                    images = await seed_images(session, S3Storage(settings))
                    images_note = f", {images} images"
                except AppError as exc:
                    images_note = f" (images skipped: {exc.detail})"
            try:
                await create_admin_user(
                    session,
                    email=DEV_ADMIN_EMAIL,
                    password=DEV_ADMIN_PASSWORD,
                    role=AdminRole.ADMIN,
                )
                admin_note = f"created dev admin {DEV_ADMIN_EMAIL} / {DEV_ADMIN_PASSWORD}"
            except AppError:
                admin_note = f"dev admin {DEV_ADMIN_EMAIL} already exists"
            await session.commit()
    finally:
        await db.dispose()

    print(
        f"Seeded {summary.websites} websites, {summary.domains} domains, "
        f"{summary.products} products, {summary.translations} translations, "
        f"{summary.assignments} assignments, {summary.content_pages} content pages, "
        f"{summary.seo_rows} SEO rows{images_note}; {admin_note}"
    )


async def _media_gc(_: argparse.Namespace) -> None:
    from app.core.storage import S3Storage
    from app.modules.media.service import purge_due

    settings = get_settings()
    db = Database(settings, application_name="showcase-cli")
    storage = S3Storage(settings)
    total = 0
    try:
        while True:
            async with db.sessionmaker() as session:
                purged = await purge_due(session, storage)
            total += purged
            if purged == 0:
                break
    finally:
        await db.dispose()
    print(f"Purged {total} objects")


async def publish_all(session: AsyncSession) -> int:
    """Queue a full-site publish for every website (e.g. after a database restore)."""
    from app.models import Website
    from app.modules.publishing.impact import whole_site
    from app.modules.publishing.service import mark_changed
    from app.modules.websites.resolution import SiteRef

    websites = list(await session.scalars(select(Website)))
    await mark_changed(
        session,
        {w.id: whole_site(SiteRef.of(w), routing=True) for w in websites},
        debounce_seconds=0,
    )
    await session.commit()
    return len(websites)


async def _publish_all(_: argparse.Namespace) -> None:
    db = Database(get_settings(), application_name="showcase-cli")
    try:
        async with db.sessionmaker() as session:
            count = await publish_all(session)
    finally:
        await db.dispose()
    print(f"Queued a full publish for {count} websites")


async def _openapi(_: argparse.Namespace) -> None:
    import json

    from app.main import create_app

    app = create_app(get_settings())
    print(json.dumps(app.openapi(), indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="Load idempotent development data")
    p_seed.add_argument("--no-images", action="store_true", help="Skip placeholder images")
    p_seed.set_defaults(handler=_seed)

    p_gc = sub.add_parser("media-gc", help="Purge deleted media whose retention has passed")
    p_gc.set_defaults(handler=_media_gc)

    p_pub = sub.add_parser("publish-all", help="Queue a full publish of every website")
    p_pub.set_defaults(handler=_publish_all)

    p_spec = sub.add_parser("openapi", help="Print the OpenAPI document as JSON")
    p_spec.set_defaults(handler=_openapi)

    p_admin = sub.add_parser("create-admin", help="Create an admin user")
    p_admin.add_argument("--email", required=True)
    p_admin.add_argument("--role", choices=[r.value for r in AdminRole], default="admin")
    p_admin.add_argument(
        "--website", action="append", help="Website key a site_editor may manage (repeatable)"
    )
    p_admin.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password from stdin instead of prompting",
    )
    p_admin.add_argument(
        "--reset-password",
        action="store_true",
        help="If the user exists, reset password/role instead of failing",
    )
    p_admin.set_defaults(handler=_create_admin)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    configure_logging(get_settings())
    try:
        asyncio.run(args.handler(args))
    except AppError as exc:
        raise SystemExit(f"Error: {exc.detail or exc.title}") from None


if __name__ == "__main__":
    main()
