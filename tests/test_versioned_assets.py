"""
Versioned Mini App assets served by api/server.py.

index.html is rewritten so every CSS/JS reference points at /v/<content hash>/…;
the current hash is cached as immutable, a stale one is served but must
revalidate, and index.html itself is never cached.
"""

import re

from aiohttp.test_utils import AioHTTPTestCase

from api import server
from api.server import create_app, get_asset_version, render_index_html

IMMUTABLE = "public, max-age=31536000, immutable"


def test_version_is_a_short_content_hash():
    version = get_asset_version()
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    assert get_asset_version() == version


def test_index_references_are_rewritten_to_the_current_version():
    html = render_index_html()
    version = get_asset_version()
    refs = re.findall(r'(?:href|src)="(/[^"]*\.(?:css|js))"', html)
    assert refs, "index.html must reference its CSS/JS"
    assert all(ref.startswith(f"/v/{version}/") for ref in refs), refs
    assert f"/v/{version}/js/app.js" in html
    assert "?v=" not in "".join(refs)
    # The external Telegram SDK is left alone.
    assert "https://telegram.org/js/telegram-web-app.js" in html


class TestVersionedAssetRoutes(AioHTTPTestCase):
    async def get_application(self):
        return create_app()

    async def test_index_is_html_and_never_cached(self):
        resp = await self.client.get("/")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        assert "no-store" in resp.headers["Cache-Control"]
        assert f"/v/{get_asset_version()}/js/app.js" in await resp.text()

    async def test_current_version_is_immutable(self):
        resp = await self.client.get(f"/v/{get_asset_version()}/js/app.js")
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == IMMUTABLE
        assert server._ASSET_CURRENT_HEADER not in resp.headers
        with open(f"{server.WEB_DIR}/js/app.js", "rb") as f:
            assert await resp.read() == f.read()

    async def test_module_imports_resolve_under_the_same_prefix(self):
        resp = await self.client.get(f"/v/{get_asset_version()}/js/api.js")
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == IMMUTABLE

    async def test_stale_version_is_served_but_revalidated(self):
        stale = "0" * 12 if get_asset_version() != "0" * 12 else "1" * 12
        resp = await self.client.get(f"/v/{stale}/css/main.css")
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == "no-cache, must-revalidate"
        assert server._ASSET_CURRENT_HEADER not in resp.headers

    async def test_unknown_file_is_404(self):
        resp = await self.client.get(f"/v/{get_asset_version()}/js/nope.js")
        assert resp.status == 404

    async def test_path_traversal_is_rejected(self):
        for path in (
            f"/v/{get_asset_version()}/js/..%2Findex.html",
            f"/v/{get_asset_version()}/js/../../config.py",
            f"/v/{get_asset_version()}/assets/x.js",
        ):
            resp = await self.client.get(path)
            assert resp.status == 404, path

    async def test_plain_paths_still_revalidate(self):
        resp = await self.client.get("/js/app.js")
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == "no-cache, must-revalidate"
