# blog_scraper_clean.py
# -*- coding: utf-8 -*-

import os
import re
import json
import logging
import requests
from flask import Flask, request, Response
from flask_cors import CORS
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
CORS(app)

# ------------------------------
# Helpers
# ------------------------------
def _normalize_url(url: str | None) -> str | None:
    """Return a safe, Wix-friendly absolute URL:
       - keep https
       - don't decode %28/%29 etc.
       - strip surrounding quotes/spaces
       - remove ?query (HubSpot resizer) to fetch the original file
    """
    if not url:
        return None
    url = url.strip("\"' ")
    if url.startswith("//"):
        url = "https:" + url
    # ვაქნეთ სუფთა ფაილის ბმული, query-ს გარეშე (Wix-ს ასე უადვილდება ჩამოტვირთვა)
    if "?" in url:
        url = url.split("?", 1)[0]
    if url.startswith(("http://", "https://")):
        return url
    return None

def _img_src_from_tag(img) -> str | None:
    src = (
        img.get("src")
        or img.get("data-src")
        or img.get("data-lazy-src")
        or img.get("data-original")
        or img.get("data-background")
    )
    if not src and img.get("srcset"):
        src = img["srcset"].split(",")[0].split()[0]
    return _normalize_url(src)

def extract_images(container):
    """Order-preserving unique list of image URLs from container."""
    seen = set()
    out = []

    # <img>
    for img in container.find_all("img"):
        src = _img_src_from_tag(img)
        if src and src not in seen:
            seen.add(src)
            out.append(src)

    # <source srcset="...">
    for source in container.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            first = srcset.split(",")[0].split()[0]
            first = _normalize_url(first)
            if first and first not in seen:
                seen.add(first)
                out.append(first)

    # style="background-image:url(...)"
    for tag in container.find_all(style=True):
        style = tag.get("style") or ""
        for match in re.findall(r"url\((.*?)\)", style):
            url = _normalize_url(match)
            if url and url not in seen:
                seen.add(url)
                out.append(url)

    return out

def find_banner_url(soup: BeautifulSoup):
    """Try to get banner from wrapper or any element with background-image, fallback to og:image."""
    # 1) explicit wrapper
    wrap = soup.select_one(".wrapper-banner-image")
    if wrap:
        style = wrap.get("style")
        if style:
            m = re.search(r"background-image\s*:\s*url\((.*?)\)", style, re.IGNORECASE)
            if m:
                url = _normalize_url(m.group(1))
                if url:
                    return url
        inner_img = wrap.find("img")
        if inner_img:
            url = _img_src_from_tag(inner_img)
            if url:
                return url

    # 2) any background-image
    any_bg = soup.find(style=re.compile(r"background-image\s*:\s*url\(", re.IGNORECASE))
    if any_bg:
        style = any_bg.get("style", "")
        m = re.search(r"background-image\s*:\s*url\((.*?)\)", style, re.IGNORECASE)
        if m:
            url = _normalize_url(m.group(1))
            if url:
                return url

    # 3) og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        url = _normalize_url(og["content"].strip())
        if url:
            return url

    return None

def remove_author_images(article: BeautifulSoup):
    """Remove likely author/avatar images (keeps content images)."""
    to_remove = []
    for img in article.find_all("img"):
        alt = (img.get("alt") or "").strip().lower()
        src = (_img_src_from_tag(img) or "").lower()
        classes = " ".join(img.get("class", [])).lower()

        # ჰეურისტიკები: ავტორი/ავატარი/byline
        if any(k in alt for k in ["author", "avatar", "byline"]) \
           or any(k in classes for k in ["author", "avatar", "byline"]) \
           or "madeline%20grecek.png" in src \
           or "madeline grecek" in alt:
            to_remove.append(img)

        # თუ img არის <p>-ში და ის <p> არაფერს სხვას არ შეიცავს (ხშირად ავტორის ბლოკი)
        parent = img.parent
        if parent and parent.name == "p":
            only_img = True
            for c in parent.children:
                if getattr(c, "name", None) is None:
                    # ტექსტი არსებობს და არა მხოლოდ whitespace?
                    if str(c).strip():
                        only_img = False
                        break
                elif c.name != "img":
                    only_img = False
                    break
            if only_img and ("author" in classes or "avatar" in classes or "byline" in classes):
                to_remove.append(parent)

    for node in to_remove:
        node.decompose()

# ------------------------------
# HTML გაწმენდა
# ------------------------------
def clean_article(article):
    # წაშალე script/style/svg/noscript
    for tag in article(["script", "style", "svg", "noscript"]):
        tag.decompose()

    # გაასუფთავე ატრიბუტები; <img> დატოვე ნორმალიზებული src/alt-ით
    for tag in article.find_all(True):
        if tag.name not in [
            "p", "h1", "h2", "h3", "h4", "h5", "h6",
            "ul", "ol", "li", "img",
            "strong", "em", "b", "i", "a",
            "table", "thead", "tbody", "tr", "th", "td"
        ]:
            tag.unwrap()
            continue

        if tag.name == "img":
            src = _img_src_from_tag(tag) or ""
            alt = (tag.get("alt") or "Image").strip()
            tag.attrs = {"src": src, "alt": alt}
        else:
            # ყველა სხვა ატრიბუტი ვშლით, რომ სუფთა HTML მივიღოთ
            tag.attrs = {}

    return article

# ------------------------------
# Blog content extraction
# ------------------------------
def extract_blog_content(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # მთავარი article მოძებნე
    article = soup.find("article")
    if not article:
        for cls in ["blog-content", "post-content", "entry-content", "content", "article-body"]:
            article = soup.find("div", class_=cls)
            if article:
                break
    if not article:
        article = soup.body

    # --- H1 ---
    h1 = soup.find("h1")

    # --- ბანერი მოძებნე ---
    banner_url = find_banner_url(soup)

    # article-ის თავში: ჯერ H1 (თუ იარსება), მერე ბანერი როგორც <p><img .../></p>
    if h1:
        article.insert(0, h1)
    if banner_url:
        p = soup.new_tag("p")
        img = soup.new_tag("img", src=banner_url, alt="Banner")
        p.append(img)
        article.insert(1, p)

    # ავტორის სურათების მოცილება (თუ დარჩა)
    remove_author_images(article)

    # გაწმენდა და ნორმალიზაცია
    return clean_article(article)

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

        # ====================
        # Title
        # ====================
        title = None
        if soup.title:
            title = (soup.title.string or "").strip()
        h1 = soup.find("h1")
        if h1 and not title:
            title = h1.get_text(strip=True)
        title = title or ""

        # ====================
        # Blog content
        # ====================
        article = extract_blog_content(resp.text)
        if not article:
            return Response("Could not extract blog content", status=422)

        # ====================
        # Images (order-preserving unique)
        # ====================
        images = []
        banner_url = find_banner_url(soup)
        if banner_url:
            images.append(banner_url)

        for img in extract_images(article):
            if img not in images:
                images.append(img)

        # სახელების გენერაცია
        image_names = [f"image{i+1}.png" for i in range(len(images))]

        # ====================
        # Build content_html
        # ====================
        content_html = str(article).strip()

        # ====================
        # Result
        # ====================
        result = {
            "title": title,
            "content_html": content_html,
            "images": images,
            "image_names": image_names,
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
