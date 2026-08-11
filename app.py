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
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip()
    return name or "untitled"


def get_list_slug_name(url: str) -> str:
    """Try to derive a nice name for the zip file / folder from the list URL."""
    parts = [p for p in url.rstrip("/").split("/") if p]
    if "list" in parts:
        idx = parts.index("list")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "list"


def fetch_page_film_links(base_url: str, page_num: int):
    """Fetch one page of the list and return (films, debug_info).

    films is a list of (title, film_page_url). debug_info is a dict with
    status_code and how many candidate elements were found, to help
    diagnose scraping failures.
    """
    url = base_url.rstrip("/") + f"/page/{page_num}/"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    debug_info = {"url": url, "status_code": resp.status_code, "candidates": 0}

    if resp.status_code != 200:
        return [], debug_info

    soup = BeautifulSoup(resp.text, "html.parser")

    # Letterboxd markup has varied over time; try several selector strategies.
    candidates = soup.select("li.poster-container div.film-poster")
    if not candidates:
        candidates = soup.select("li.posteritem div.film-poster")
    if not candidates:
        candidates = soup.select("div.film-poster")
    if not candidates:
        candidates = soup.select("[data-film-slug]")

    debug_info["candidates"] = len(candidates)

    films = []
    seen_slugs = set()
    for c in candidates:
        title = (
            c.get("data-film-name")
            or c.get("data-item-name")
            or c.get("alt")
        )
        target_link = c.get("data-target-link")
        slug = c.get("data-film-slug") or c.get("data-item-slug")

        img = c.find("img")
        if not title and img:
            title = img.get("alt")

        film_url = None
        if target_link:
            film_url = "https://letterboxd.com" + target_link
        elif slug:
            film_url = f"https://letterboxd.com/film/{slug}/"

        if title and film_url and slug not in seen_slugs:
            films.append((title, film_url))
            if slug:
                seen_slugs.add(slug)

    return films, debug_info


def get_poster_from_film_page(film_url: str):
    """Visit the movie's own page and pull the real poster from the og:image meta tag."""
    try:
        resp = requests.get(film_url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        meta = soup.find("meta", property="og:image")
        if meta and meta.get("content"):
            return meta["content"]
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
    "Paste a link to a Letterboxd list. This app will go through the list "
    "pages you choose, visit each movie's own page to grab its real poster, "
    "and package them all into a zip file you can download."
)

list_url = st.text_input(
    "Letterboxd list URL",
    placeholder="https://letterboxd.com/username/list/list-name/",
)

col1, col2 = st.columns(2)
with col1:
    start_page = st.number_input("From page", min_value=1, value=1, step=1)
with col2:
    end_page = st.number_input("To page", min_value=1, value=1, step=1)

st.caption(
    "Not sure how many pages the list has? Set 'To page' high — the app "
    "stops automatically once it hits a page with no films."
)

start = st.button("Fetch posters", type="primary", disabled=not list_url)

if start:
    if "letterboxd.com" not in list_url or "/list/" not in list_url:
        st.error("That doesn't look like a valid Letterboxd list URL.")
    elif end_page < start_page:
        st.error("'To page' must be greater than or equal to 'From page'.")
    else:
        list_name = get_list_slug_name(list_url)
        status = st.empty()
        progress = st.progress(0)

        all_films = []
        page_num = int(start_page)
        debug_log = []

        while page_num <= int(end_page):
            status.info(f"Scanning page {page_num}...")
            films, debug_info = fetch_page_film_links(list_url, page_num)
            debug_log.append(debug_info)
            if not films:
                break
            all_films.extend(films)
            page_num += 1

        if not all_films:
            status.error(
                "No films found in that page range. Double check the list "
                "URL and page numbers are correct."
            )
            with st.expander("Debug info"):
                for d in debug_log:
                    st.write(d)
                st.write(
                    "If status_code is not 200, Letterboxd may be blocking "
                    "requests from this server. If status_code is 200 but "
                    "candidates is 0, Letterboxd's page markup may have "
                    "changed and the scraper's selectors need updating."
                )
        else:
            status.info(f"Found {len(all_films)} films. Fetching posters from each movie page...")

            zip_buffer = io.BytesIO()
            used_names = {}
            folder_name = sanitize_filename(f"letterboxd {list_name}")
            success_count = 0

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, (title, film_url) in enumerate(all_films):
                    try:
                        poster_url = get_poster_from_film_page(film_url)
                        if not poster_url:
                            st.warning(f"Could not find a poster for '{title}'.")
                            progress.progress((i + 1) / len(all_films))
                            continue

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

                        zf.writestr(f"{folder_name}/{filename}", img_bytes)
                        success_count += 1
                    except Exception as e:
                        st.warning(f"Could not download poster for '{title}': {e}")

                    progress.progress((i + 1) / len(all_films))

            zip_buffer.seek(0)
            zip_filename = f"letterboxd {list_name}.zip"

            status.success(f"Done! {success_count} of {len(all_films)} posters packaged into your zip file.")
            st.download_button(
                label="⬇️ Download zip",
                data=zip_buffer,
                file_name=zip_filename,
                mime="application/zip",
            )
