import io
import re
import zipfile

import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Letterboxd List Poster Downloader", page_icon="🎬")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip()
    return name or "untitled"


def get_list_slug_name(url: str) -> str:
    """Try to derive a nice name for the zip file from the list URL."""
    parts = [p for p in url.rstrip("/").split("/") if p]
    if "list" in parts:
        idx = parts.index("list")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "list"


def get_poster_url_from_img(img_tag) -> str:
    """Letterboxd lazy-loads posters; check common attributes."""
    for attr in ("data-src", "srcset", "src"):
        val = img_tag.get(attr)
        if val:
            # srcset may contain multiple urls, take the first
            first = val.split(",")[0].strip().split(" ")[0]
            if first.startswith("http"):
                return first
    return None


def fetch_page_films(base_url: str, page_num: int):
    """Fetch one page of the list and return list of (title, poster_url)."""
    url = base_url.rstrip("/") + f"/page/{page_num}/"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    containers = soup.select("li.poster-container div.film-poster, li.poster-container div[data-film-name]")
    if not containers:
        containers = soup.select("div.film-poster")

    films = []
    for c in containers:
        title = c.get("data-film-name") or c.get("data-item-name")
        img = c.find("img")
        if not title and img:
            title = img.get("alt")
        poster_url = get_poster_url_from_img(img) if img else None

        # Letterboxd often serves a real poster only via an ajax endpoint
        # referenced in data-target-link; fall back to that if no direct image found.
        if not poster_url:
            target_link = c.get("data-target-link") or c.get("data-film-slug")
            if target_link:
                poster_url = try_ajax_poster(target_link)

        if title and poster_url:
            films.append((title, poster_url))

    return films


def try_ajax_poster(target_link: str):
    """Fallback: query Letterboxd's ajax poster endpoint for a larger image."""
    try:
        slug = target_link.strip("/").split("/")[-1] if "film" in target_link else target_link
        ajax_url = f"https://letterboxd.com/ajax/poster{target_link}std/230x345/"
        resp = requests.get(ajax_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            img = soup.find("img")
            if img and img.get("src"):
                return img["src"]
    except Exception:
        pass
    return None


def download_image(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.content


def get_extension(url: str) -> str:
    match = re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", url.lower())
    return match.group(1) if match else "jpg"


st.title("🎬 Letterboxd List Poster Downloader")
st.write(
    "Paste a link to a Letterboxd list. This app will go through every page of "
    "the list, grab each movie's poster thumbnail, and package them all into a "
    "zip file you can download."
)

list_url = st.text_input(
    "Letterboxd list URL",
    placeholder="https://letterboxd.com/username/list/list-name/",
)

start = st.button("Fetch posters", type="primary", disabled=not list_url)

if start:
    if "letterboxd.com" not in list_url or "/list/" not in list_url:
        st.error("That doesn't look like a valid Letterboxd list URL.")
    else:
        list_name = get_list_slug_name(list_url)
        status = st.empty()
        progress = st.progress(0)

        all_films = []
        page_num = 1
        max_pages = 200  # safety cap

        while page_num <= max_pages:
            status.info(f"Scanning page {page_num}...")
            films = fetch_page_films(list_url, page_num)
            if not films:
                break
            all_films.extend(films)
            page_num += 1

        if not all_films:
            status.error(
                "No films/posters found. Double check the list URL is correct "
                "and public."
            )
        else:
            status.info(f"Found {len(all_films)} films. Downloading posters...")

            zip_buffer = io.BytesIO()
            used_names = {}

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, (title, poster_url) in enumerate(all_films):
                    try:
                        img_bytes = download_image(poster_url)
                        ext = get_extension(poster_url)
                        base_name = sanitize_filename(title)

                        # avoid collisions from duplicate titles
                        count = used_names.get(base_name, 0)
                        used_names[base_name] = count + 1
                        filename = (
                            f"{base_name}.{ext}"
                            if count == 0
                            else f"{base_name} ({count}).{ext}"
                        )

                        zf.writestr(filename, img_bytes)
                    except Exception as e:
                        st.warning(f"Could not download poster for '{title}': {e}")

                    progress.progress((i + 1) / len(all_films))

            zip_buffer.seek(0)
            zip_filename = f"letterboxd {list_name}.zip"

            status.success(f"Done! {len(all_films)} posters packaged into your zip file.")
            st.download_button(
                label="⬇️ Download zip",
                data=zip_buffer,
                file_name=zip_filename,
                mime="application/zip",
            )
