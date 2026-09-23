from xml.sax.saxutils import escape

from upmovies.public.service import SitemapEntity, SitemapFilm


def render_sitemap(base_url: str, films: list[SitemapFilm], entities: list[SitemapEntity]) -> str:
    """The sitemap: the root, every indexed film page, then every entity page (EF-17).

    Films carry a `lastmod` and entities do not — see `get_sitemap_entities` for why — so the
    two loops stay separate rather than being folded into one row type with a nullable field.
    """
    base = base_url.rstrip("/")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{escape(base)}/</loc></url>",
    ]
    for film in films:
        loc = f"{base}/film/{film.ref}"
        lastmod = film.lastmod.date().isoformat()
        lines.append(f"  <url><loc>{escape(loc)}</loc><lastmod>{lastmod}</lastmod></url>")
    for entity in entities:
        loc = f"{base}/{entity.path}/{entity.ref}"
        lines.append(f"  <url><loc>{escape(loc)}</loc></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"
