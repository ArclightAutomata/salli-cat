#!/usr/bin/env python3
"""Salli Cat CLI - CRT manual preservation tool."""

import click

from pathlib import Path


@click.group()
@click.version_option(version="0.1.0", prog_name="salli")
def cli():
    """Salli Cat - CRT manual preservation tool.

    Scrapes CRT manuals from multiple sources and uploads them to the Internet Archive.
    """
    pass


# =============================================================================
# Scrape command group
# =============================================================================

@cli.group()
def scrape():
    """Scrape manuals from various sources."""
    pass


@scrape.command("manualslib")
@click.option("--brand", help="Specific brand to scrape (overrides config)")
@click.option("--brands", multiple=True, help="Multiple brands to scrape (overrides config)")
@click.option("--discover-brands", is_flag=True, help="Discover all brands with TV category")
@click.option("--use-discovered", is_flag=True, help="Scrape all discovered brands")
@click.option("--index-only", is_flag=True, help="Only build index, don't download")
@click.option("--download-only", is_flag=True, help="Only download pending manuals (all brands with pending manuals unless --brand(s) given)")
@click.option("--limit", type=int, help="Limit number of downloads")
@click.option("--clear", is_flag=True, help="Clear all manual records before scraping")
@click.option("--clear-brands", is_flag=True, help="Clear all discovered brands")
@click.option("--clear-all", is_flag=True, help="Clear both manuals and brands")
def scrape_manualslib(brand, brands, discover_brands, use_discovered, index_only, download_only, limit, clear, clear_brands, clear_all):
    """Scrape CRT manuals from ManualsLib."""
    import sys

    # Combine --brand and --brands into a single list
    all_brands = list(brands) if brands else []
    if brand:
        all_brands.insert(0, brand)

    # Build argv for the scraper
    argv = []
    if all_brands:
        argv.extend(["--brands"] + all_brands)
    if discover_brands:
        argv.append("--discover-brands")
    if use_discovered:
        argv.append("--use-discovered")
    if index_only:
        argv.append("--index-only")
    if download_only:
        argv.append("--download-only")
    if limit:
        argv.extend(["--limit", str(limit)])
    if clear:
        argv.append("--clear")
    if clear_brands:
        argv.append("--clear-brands")
    if clear_all:
        argv.append("--clear-all")

    # Patch sys.argv and run
    sys.argv = ["manualslib_scraper"] + argv

    from manualslib_scraper import main
    main()


@scrape.command("manualsbase")
@click.option("--index-only", is_flag=True, help="Only build index, don't download")
@click.option("--download-only", is_flag=True, help="Only download pending manuals")
@click.option("--limit-brands", type=int, help="Limit number of brands to process")
@click.option("--brands", multiple=True, help="Specific brand URLs to scrape")
@click.option("--clear", is_flag=True, help="Clear all manualsbase records")
def scrape_manualsbase(index_only, download_only, limit_brands, brands, clear):
    """Scrape CRT manuals from ManualsBase."""
    import sys

    argv = []
    if index_only:
        argv.append("--index-only")
    if download_only:
        argv.append("--download-only")
    if limit_brands:
        argv.extend(["--limit-brands", str(limit_brands)])
    if brands:
        argv.extend(["--brands"] + list(brands))
    if clear:
        argv.append("--clear")

    sys.argv = ["manualsbase_scraper"] + argv

    from manualsbase_scraper import main
    main()


@scrape.command("manualzz")
@click.option("--urls", multiple=True, help="Specific catalog URLs to scrape")
@click.option("--index-only", is_flag=True, help="Only build index, don't download")
@click.option("--download-only", is_flag=True, help="Only download pending manuals")
@click.option("--clear", is_flag=True, help="Clear all manualzz records")
def scrape_manualzz(urls, index_only, download_only, clear):
    """Scrape CRT manuals from Manualzz."""
    import sys

    argv = []
    if urls:
        argv.extend(["--urls"] + list(urls))
    if index_only:
        argv.append("--index-only")
    if download_only:
        argv.append("--download-only")
    if clear:
        argv.append("--clear")

    sys.argv = ["manualzz_scraper"] + argv

    from manualzz_scraper import main
    main()


@scrape.command("elektrotanya")
@click.option("--query", multiple=True, help="Search terms (overrides config queries)")
@click.option("--kategoria", multiple=True, help="Category filters, e.g. tv monitor (overrides config)")
@click.option("--index-only", is_flag=True, help="Only build index, don't download")
@click.option("--download-only", is_flag=True, help="Only download pending manuals")
@click.option("--limit", type=int, help="Max downloads this run (default: 10, the site's daily cap)")
@click.option("--clear", is_flag=True, help="Clear all elektrotanya records")
def scrape_elektrotanya(query, kategoria, index_only, download_only, limit, clear):
    """Scrape service manuals from elektrotanya.com (10 files/day per IP)."""
    import sys

    argv = []
    if query:
        argv.extend(["--query"] + list(query))
    if kategoria:
        argv.extend(["--kategoria"] + list(kategoria))
    if index_only:
        argv.append("--index-only")
    if download_only:
        argv.append("--download-only")
    if limit is not None:
        argv.extend(["--limit", str(limit)])
    if clear:
        argv.append("--clear")

    sys.argv = ["elektrotanya_scraper"] + argv

    from elektrotanya_scraper import main
    main()


# =============================================================================
# Upload command
# =============================================================================

@cli.command()
@click.option("--source", type=click.Choice(["manualslib", "manualsbase", "manualzz", "elektrotanya"]),
              help="Source to upload from (default: all sources)")
@click.option("--limit", type=int, help="Limit number of uploads")
@click.option("--dry-run", is_flag=True, help="Preview uploads without uploading")
def upload(source, limit, dry_run):
    """Upload downloaded manuals to Internet Archive."""
    import sys

    argv = []
    if source:
        argv.extend(["--source", source])
    if limit:
        argv.extend(["--limit", str(limit)])
    if dry_run:
        argv.append("--dry-run")

    sys.argv = ["ia_uploader"] + argv

    from ia_uploader import main
    main()


# =============================================================================
# Dashboard command
# =============================================================================

@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=5000, type=int, help="Port to bind to")
@click.option("--debug", is_flag=True, help="Run in debug mode")
def dashboard(host, port, debug):
    """Run the web dashboard."""
    from dashboard import app
    app.run(host=host, port=port, debug=debug)

# Add 'dash' as shortcut for 'dashboard'
cli.add_command(dashboard, name="dash")


# =============================================================================
# Check-archive command
# =============================================================================

@cli.command("check-archive")
@click.option("--continuous", is_flag=True, help="Run continuously, checking new manuals")
@click.option("--delay-min", type=float, help="Minimum delay between checks (seconds)")
@click.option("--delay-max", type=float, help="Maximum delay between checks (seconds)")
@click.option("--stats", is_flag=True, help="Just show current statistics")
def check_archive(continuous, delay_min, delay_max, stats):
    """Check if manuals already exist on archive.org."""
    import sys

    argv = []
    if continuous:
        argv.append("--continuous")
    if delay_min is not None:
        argv.extend(["--delay-min", str(delay_min)])
    if delay_max is not None:
        argv.extend(["--delay-max", str(delay_max)])
    if stats:
        argv.append("--stats")

    sys.argv = ["archive_checker"] + argv

    from archive_checker import main
    main()


# =============================================================================
# Status command
# =============================================================================

@cli.command()
def status():
    """Show current scraping and upload statistics."""
    import database

    database.init_db()

    click.echo("\n" + "=" * 60)
    click.echo("SALLI CAT STATUS")
    click.echo("=" * 60)

    # Overall stats
    stats = database.get_stats()
    click.echo(f"\nOverall:")
    click.echo(f"  Total manuals:    {stats['total']:,}")
    click.echo(f"  Downloaded:       {stats['downloaded']:,}")
    click.echo(f"  Archived:         {stats['archived']:,}")
    click.echo(f"  Pending:          {stats['pending']:,}")

    # Per-source stats
    for source in ["manualslib", "manualsbase", "manualzz", "elektrotanya"]:
        source_stats = database.get_stats(source=source)
        if source_stats['total'] > 0:
            click.echo(f"\n{source.capitalize()}:")
            click.echo(f"  Total:      {source_stats['total']:,}")
            click.echo(f"  Downloaded: {source_stats['downloaded']:,}")
            click.echo(f"  Archived:   {source_stats['archived']:,}")
            click.echo(f"  Pending:    {source_stats['pending']:,}")

    # Brand stats (if any discovered)
    try:
        brand_stats = database.get_brand_stats()
        if brand_stats['total'] > 0:
            click.echo(f"\nDiscovered Brands:")
            click.echo(f"  Total:   {brand_stats['total']:,}")
            click.echo(f"  Scraped: {brand_stats['scraped']:,}")
            click.echo(f"  Pending: {brand_stats['pending']:,}")
    except Exception:
        pass

    click.echo("\n" + "=" * 60 + "\n")


# =============================================================================
# Clear command
# =============================================================================

@cli.group()
def clear():
    """Clear database records."""
    pass


@clear.command("all")
@click.confirmation_option(prompt="Are you sure you want to clear ALL records?")
def clear_all():
    """Clear all records from the database."""
    import database
    database.init_db()
    database.clear_everything()
    click.echo("All records cleared.")


@clear.command("manuals")
@click.option("--source", type=click.Choice(["manualslib", "manualsbase", "manualzz", "elektrotanya"]),
              help="Only clear records from this source")
@click.confirmation_option(prompt="Are you sure you want to clear manual records?")
def clear_manuals(source):
    """Clear manual records from the database."""
    import database
    database.init_db()
    if source:
        database.clear_manuals_by_source(source)
        click.echo(f"{source.capitalize()} records cleared.")
    else:
        database.clear_all()
        click.echo("All manual records cleared.")


@clear.command("brands")
@click.confirmation_option(prompt="Are you sure you want to clear discovered brands?")
def clear_brands():
    """Clear discovered brands from the database."""
    import database
    database.init_db()
    database.clear_brands()
    click.echo("Discovered brands cleared.")


def main():
    cli()


if __name__ == "__main__":
    main()
