from xml.sax.saxutils import escape

from upmovies.public.service import SitemapFilm


def render_sitemap(base_url: str, films: list[SitemapFilm]) -> str:
    base = base_url.rstrip("/")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{escape(base)}/</loc></url>",
    ]
    for film in films:
        loc = f"{base}/film/{film.slug}"
        lastmod = film.lastmod.date().isoformat()
        lines.append(f"  <url><loc>{escape(loc)}</loc><lastmod>{lastmod}</lastmod></url>")
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"
