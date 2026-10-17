"""HIVE-1275: the dashboard favicon must resolve under the Vite base path.

The frontend is served at /hive/ (VITE_BASE_PATH). index.html previously
declared the icons with absolute root paths (/favicon.png), which the browser
requested from the domain root where nginx only serves /hive/ -> HTTP 404 +
console noise. The fix uses Vite's %BASE_URL% so the built href resolves to
/hive/favicon.png, and every declared icon file exists in public/.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "frontend" / "index.html"
PUBLIC = ROOT / "frontend" / "public"


def test_favicon_links_use_base_url():
    html = INDEX.read_text()
    for icon in ("favicon.svg", "favicon.png", "apple-touch-icon.png"):
        assert f'%BASE_URL%{icon}' in html, f"{icon} must be referenced via %BASE_URL%"
        assert f'href="/{icon}"' not in html, f"{icon} must not use an absolute root href"


def test_declared_icon_files_exist_in_public():
    html = INDEX.read_text()
    for icon in ("favicon.svg", "favicon.png", "apple-touch-icon.png"):
        assert (PUBLIC / icon).is_file(), f"public/{icon} must exist"
        assert f'%BASE_URL%{icon}' in html
