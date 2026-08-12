import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

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

MAX_WORKERS = 16


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


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


def fetch_page_film_links(session: requests.Session, base_url: str, page_num: int):
    """Fetch one page of the list and return (films, debug_info).

    films is a list of (title, film_page_url). debug_info is a dict with
    status_code and how many candidate elements were found, to help
    diagnose scraping failures.
    """
    url = base_url.rstrip("/") + f"/page/{page_num}/"
    resp = session.get(url, timeout=20)
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
    if candidates:
        first = candidates[0]
        parent_li = first.find_parent("li")
        debug_info["sample_html"] = str(first)[:500]
        debug_info["sample_parent_html"] = str(parent_li)[:1200] if parent_li else None
    else:
        debug_info["sample_html"] = None
        debug_info["sample_parent_html"] = None

    films = []
    seen_urls = set()
    for c in candidates:
        # Title: prefer explicit data attributes, fall back to the poster image's alt text.
        title = (
            c.get("data-film-name")
            or c.get("data-item-name")
            or c.get("alt")
        )
        img = c.find("img")
        if not title and img:
            title = img.get("alt") or img.get("data-original-alt")

        # Film URL: check the element itself, then walk up through all ancestors
        # looking for data attributes or an <a href="/film/.../">, since Letterboxd
        # often puts the real data on the <li> wrapping the (lazy-loaded) poster div.
        film_url = None

        node = c
        for _ in range(4):  # climb a few levels: div -> li -> ul, etc.
            if node is None:
                break
            target_link = node.get("data-target-link")
            slug = node.get("data-film-slug") or node.get("data-item-slug")
            if target_link:
                film_url = "https://letterboxd.com" + target_link
                break
            if slug:
                film_url = f"https://letterboxd.com/film/{slug}/"
                break
            a_tag = node.find("a", href=re.compile(r"^/film/")) if hasattr(node, "find") else None
            if a_tag and a_tag.get("href"):
                film_url = "https://letterboxd.com" + a_tag["href"]
                if not title:
                    title = a_tag.get("title") or a_tag.get_text(strip=True)
                break
            node = node.find_parent()

        if title and film_url and film_url not in seen_urls:
            films.append((title.strip(), film_url))
            seen_urls.add(film_url)

    return films, debug_info


def get_poster_and_year_from_film_page(session: requests.Session, film_url: str):
    """Visit the movie's own page and pull the real poster (og:image) and release year."""
    try:
        resp = session.get(film_url, timeout=20)
        if resp.status_code != 200:
            return None, None
        soup = BeautifulSoup(resp.text, "html.parser")

        poster_url = None
        meta_img = soup.find("meta", property="og:image")
        if meta_img and meta_img.get("content"):
            poster_url = meta_img["content"]

        year = None
        # Letterboxd usually links the release year like <a href="/films/year/2010/">
        year_link = soup.find("a", href=re.compile(r"^/films/year/\d{4}/"))
        if year_link:
            match = re.search(r"/films/year/(\d{4})/", year_link["href"])
            if match:
                year = match.group(1)
        if not year:
            # Fall back to parsing "Title (YYYY)" out of the og:title meta tag.
            meta_title = soup.find("meta", property="og:title")
            if meta_title and meta_title.get("content"):
                match = re.search(r"\((\d{4})\)", meta_title["content"])
                if match:
                    year = match.group(1)

        return poster_url, year
    except Exception:
        return None, None


def download_image(session: requests.Session, url: str) -> bytes:
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    return resp.content


def get_extension(url: str) -> str:
    match = re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", url.lower())
    return match.group(1) if match else "jpg"


def fetch_poster_bytes(session: requests.Session, title: str, film_url: str):
    """Worker used by the thread pool: resolve a film's poster + year and download it."""
    poster_url, year = get_poster_and_year_from_film_page(session, film_url)
    if not poster_url:
        return title, year, None, None
    img_bytes = download_image(session, poster_url)
    ext = get_extension(poster_url)
    return title, year, img_bytes, ext


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
        session = make_session()

        # Scan list pages concurrently.
        page_range = list(range(int(start_page), int(end_page) + 1))
        status.info(f"Scanning {len(page_range)} page(s)...")

        page_results = {}
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(page_range))) as executor:
            futures = {
                executor.submit(fetch_page_film_links, session, list_url, p): p
                for p in page_range
            }
            for future in as_completed(futures):
                p = futures[future]
                films, debug_info = future.result()
                page_results[p] = (films, debug_info)

        all_films = []
        debug_log = []
        for p in page_range:
            films, debug_info = page_results[p]
            debug_log.append(debug_info)
            if not films:
                # Stop including pages once we hit an empty one, mirroring
                # the old "stop at end of list" behavior, but only for
                # pages at/after the first empty one encountered in order.
                break
            all_films.extend(films)

        if not all_films:
            status.error(
                "No films found in that page range. Double check the list "
                "URL and page numbers are correct."
            )
            with st.expander("Debug info"):
                for d in debug_log:
                    st.write(f"Page: {d['url']} — status {d['status_code']} — {d['candidates']} candidates")
                    if d.get("sample_html"):
                        st.write("Poster element:")
                        st.code(d["sample_html"], language="html")
                    if d.get("sample_parent_html"):
                        st.write("Parent `<li>` element:")
                        st.code(d["sample_parent_html"], language="html")
                st.write(
                    "If status_code is not 200, Letterboxd may be blocking "
                    "requests from this server. If candidates is 0, the "
                    "selectors found no poster elements at all. If "
                    "candidates > 0 but no films were extracted, check the "
                    "sample HTML above for the actual attribute/tag names."
                )
        else:
            status.info(f"Found {len(all_films)} films. Fetching posters from each movie page...")

            zip_buffer = io.BytesIO()
            used_names = {}
            folder_name = sanitize_filename(f"letterboxd {list_name}")
            success_count = 0
            done_count = 0
            progress_lock = Lock()

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = {
                        executor.submit(fetch_poster_bytes, session, title, film_url): title
                        for title, film_url in all_films
                    }
                    for future in as_completed(futures):
                        title = futures[future]
                        try:
                            title, img_bytes, ext = future.result()
                            if img_bytes is None:
                                st.warning(f"Could not find a poster for '{title}'.")
                            else:
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

                        with progress_lock:
                            done_count += 1
                            progress.progress(done_count / len(all_films))

            zip_buffer.seek(0)
            zip_filename = f"letterboxd {list_name}.zip"

            status.success(f"Done! {success_count} of {len(all_films)} posters packaged into your zip file.")
            st.download_button(
                label="⬇️ Download zip",
                data=zip_buffer,
                file_name=zip_filename,
                mime="application/zip",
            )
