# blog_scraper_clean.py
# -*- coding: utf-8 -*-

import os
import re
import json
import logging
import requests
from urllib.parse import urlparse
from flask import Flask, request, Response
from flask_cors import CORS
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
CORS(app)

# ------------------------------
# Helpers
# ------------------------------
ABS_PREFIXES = ("http://", "https://")

def _normalize_url(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip()
    if not u:
        return None
    # protocol-relative -> https:
    if u.startswith("//"):
        u = "https:" + u
    return u if u.startswith(ABS_PREFIXES) else None

def _get_img_src(tag) -> str | None:
    """Extract the most likely image URL from <img> with lazy attrs/srcset etc."""
    src = (
        tag.get("src")
        or tag.get("data-src")
        or tag.get("data-lazy-src")
        or tag.get("data-original")
        or tag.get("data-background")
    )
    if not src and tag.get("srcset"):
        try:
            src = tag["srcset"].split(",")[0].split()[0]
        except Exception:
            src = None
    return _normalize_url(src)

def _guess_ext_from_url(u: str) -> str:
    """Try to keep original extension; default to png."""
    try:
        path = urlparse(u).path
        ext = os.path.splitext(path)[1].lower()
        if ext in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"]:
            # svg uploads ზოგჯერ ბლოკირდება—საჩვენებლად დავტოვოთ, მაგრამ placeholder-ში ე.წ. svg-საც შევინარჩუნებთ
            return ext.lstrip(".")
    except Exception:
        pass
    return "png"

def extract_images(container):
    """Collect absolute image URLs from any tags."""
    image_urls = set()

    # <img> + lazy attributes + srcset
    for img in container.find_all("img"):
        u = _get_img_src(img)
        if u:
            image_urls.add(u)

    # <source srcset="...">
    for source in container.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            first = srcset.split(",")[0].split()[0].strip()
            u = _normalize_url(first)
            if u:
                image_urls.add(u)

    # style="background-image:url(...)"
    for tag in container.find_all(style=True):
        style = tag["style"]
        for match in re.findall(r"url\((.*?)\)", style):
            u = _normalize_url(match.strip("\"' "))
            if u:
                image_urls.add(u)

    return list(image_urls)

# ------------------------------
# Find banner URL
# ------------------------------
def find_banner_url(soup: BeautifulSoup):
    # explicit wrapper
    wrap = soup.select_one(".wrapper-banner-image")
    if wrap:
        style = wrap.get("style")
        if style:
            m = re.search(r"background-image\s*:\s*url\((.*?)\)", style, re.IGNORECASE)
            if m:
                u = _normalize_url(m.group(1).strip("\"' "))
                if u:
                    return u
        inner_img = wrap.find("img")
        if inner_img:
            u = _get_img_src(inner_img)
            if u:
                return u

    # any node with background-image
    any_bg = soup.find(style=re.compile(r"background-image\s*:\s*url\(", re.IGNORECASE))
    if any_bg:
        style = any_bg.get("style", "")
        m = re.search(r"background-image\s*:\s*url\((.*?)\)", style, re.IGNORECASE)
        if m:
            u = _normalize_url(m.group(1).strip("\"' "))
            if u:
                return u

    # fallback: og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        u = _normalize_url(og["content"].strip())
        if u:
            return u

    return None

# ------------------------------
# Clean HTML (keep only safe tags/strip attrs except imgs/links)
# ------------------------------
def clean_article(article):
    for tag in article(["script", "style", "svg", "noscript"]):
        tag.decompose()

    allow = {
        "p","h1","h2","h3","h4","h5","h6",
        "ul","ol","li","img","strong","em","b","i","a",
        "table","thead","tbody","tr","th","td","figure"
    }
    for tag in article.find_all(True):
        if tag.name not in allow:
            tag.unwrap()
            continue

        if tag.name == "img":
            # do not normalize src here (we'll replace with placeholders later)
            alt = tag.get("alt", "").strip() or "Image"
            tag.attrs = {"alt": alt}
        elif tag.name == "a":
            href = tag.get("href")
            href = _normalize_url(href) or "#"
            tag.attrs = {"href": href, "rel": "noopener", "target": "_blank"}
        elif tag.name == "figure":
            # we'll control only data-img-slot later
            tag.attrs = {}
        else:
            tag.attrs = {}

    return article

# ------------------------------
# Build placeholders + mapping
# ------------------------------
def apply_placeholders(article: BeautifulSoup, banner_url: str | None):
    """
    - Inserts banner as <p><figure data-img-slot="1"><img src="images/image1.ext" alt="Banner"/></figure></p>
      if we have a banner_url.
    - Replaces all <img> with images/imageN.ext placeholders.
    - Adds unique data-img-slot only for the first occurrence of each unique image.
    - Returns mapping (image_url_map), ordered images list, image_names list.
    """
    soup = article  # same object

    image_url_map = {}           # filename -> original URL
    name_for_url = {}            # original URL -> filename
    images_list = []             # list of original URLs (ordered, unique)
    image_names = []             # list of filenames (ordered, unique)
    slot_counter = 1

    def new_filename_for(url: str) -> str:
        nonlocal slot_counter
        ext = _guess_ext_from_url(url)
        # for banner use 1, others increment from current length+1
        idx = len(image_url_map) + 1
        # but if we call for banner first, it will be image1
        return f"image{idx}.{ext}"

    # 1) Insert banner at top if present
    if banner_url:
        banner_url = _normalize_url(banner_url)
        if banner_url:
            fname = new_filename_for(banner_url)  # should become image1.ext
            image_url_map[fname] = banner_url
            name_for_url[banner_url] = fname
            images_list.append(banner_url)
            image_names.append(fname)

            # Prepend <p><figure data-img-slot="1"><img .../></figure></p>
            p = soup.new_tag("p")
            fig = soup.new_tag("figure")
            fig.attrs["data-img-slot"] = str(slot_counter)  # 1
            img = soup.new_tag("img", src=f"images/{fname}", alt="Banner")
            fig.append(img)
            p.append(fig)
            if soup.contents:
                soup.insert(0, p)
            else:
                soup.append(p)
            slot_counter += 1

    # 2) Replace all imgs with placeholders (dedupe by URL)
    for img in soup.find_all("img"):
        # If this img is our freshly inserted banner placeholder, skip (it already has placeholder)
        existing_src = img.get("src")
        if existing_src and existing_src.startswith("images/image") and "Banner" in (img.get("alt") or ""):
            continue

        url = _get_img_src(img)
        if not url:
            # no valid absolute URL -> drop the image entirely
            img.decompose()
            continue

        # Already seen?
        if url in name_for_url:
            fname = name_for_url[url]
            # reuse placeholder
            img["src"] = f"images/{fname}"
            # ensure alt present
            if not img.get("alt"):
                img["alt"] = "Image"
            # DO NOT assign another slot for duplicates
            # If parent is figure with data-img-slot, remove the slot to avoid duplicates
            if img.parent and img.parent.name == "figure":
                if "data-img-slot" in img.parent.attrs:
                    del img.parent.attrs["data-img-slot"]
        else:
            # First time we see this URL
            fname = new_filename_for(url)
            name_for_url[url] = fname
            image_url_map[fname] = url
            images_list.append(url)
            image_names.append(fname)

            img["src"] = f"images/{fname}"
            if not img.get("alt"):
                img["alt"] = "Image"

            # Wrap in <figure data-img-slot="N"> (unique)
            if img.parent and img.parent.name == "figure":
                fig = img.parent
            else:
                fig = soup.new_tag("figure")
                img.replace_with(fig)
                fig.append(img)
            fig.attrs["data-img-slot"] = str(slot_counter)
            slot_counter += 1

    return soup, image_url_map, images_list, image_names

# ------------------------------
# Extract blog content
# ------------------------------
def extract_blog_content(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # main article
    article = soup.find("article")
    if not article:
        for cls in ["blog-content", "post-content", "entry-content", "content", "article-body"]:
            article = soup.find("div", class_=cls)
            if article:
                break
    if not article:
        article = soup.body or soup

    h1 = soup.find("h1")

    # find banner by rules (wrapper/style/og:image)
    banner_url = find_banner_url(soup)

    # prepend H1 if not already inside article
    if h1:
        article.insert(0, h1)

    # Clean tags/attrs first
    article = clean_article(article)

    # Now replace imgs with placeholders + insert banner figure at top
    article, image_url_map, images_list, image_names = apply_placeholders(article, banner_url)

    return article, image_url_map, images_list, image_names

# ------------------------------
# API
# ------------------------------
@app.route("/scrape-blog", methods=["POST"])
def scrape_blog():
    try:
        data = request.get_json(force=True)
        url = data.get("url")
        if not url:
            return Response("Missing 'url' field", status=400)

        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Title
        title = None
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        h1 = soup.find("h1")
        if h1 and not title:
            title = h1.get_text(strip=True)
        title = title or ""

        # Content + placeholders/mapping
        article, image_url_map, images, image_names = extract_blog_content(resp.text)
        if not article:
            return Response("Could not extract blog content", status=422)

        content_html = str(article).strip()

        result = {
            "title": title,
            "content_html": content_html,
            # Keep legacy fields (some clients use them)
            "images": images,                # list of original URLs (unique, ordered)
            "image_names": image_names,      # corresponding filenames
            # Add the crucial map for the converter
            "image_url_map": image_url_map   # {"image1.png": "https://...", ...}
        }
        return Response(json.dumps(result, ensure_ascii=False), mimetype="application/json")

    except Exception as e:
        logging.exception("Error scraping blog")
        return Response(f"Error: {str(e)}", status=500)

# ------------------------------
# Run
# ------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
