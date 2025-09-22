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
# Helper: სურათების ამოღება
# ------------------------------
def extract_images(container):
    image_urls = set()

    # <img> + lazy attributes + srcset
    for img in container.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
            or img.get("data-background")
        )
        if not src and img.get("srcset"):
            src = img["srcset"].split(",")[0].split()[0]
        if src:
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith(("http://", "https://")):
                image_urls.add(src)

    # <source srcset="...">
    for source in container.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            first = srcset.split(",")[0].split()[0]
            if first.startswith("//"):
                first = "https:" + first
            if first.startswith(("http://", "https://")):
                image_urls.add(first)

    # style="background-image:url(...)"
    for tag in container.find_all(style=True):
        style = tag["style"]
        for match in re.findall(r"url\((.*?)\)", style):
            url = match.strip("\"' ")
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith(("http://", "https://")):
                image_urls.add(url)

    return list(image_urls)

# ------------------------------
# Helper: ბანერის URL-ის პოვნა
# ------------------------------
def find_banner_url(soup: BeautifulSoup):
    # 1) სცადე კონკრეტული wrapper-კლასი
    wrap = soup.select_one(".wrapper-banner-image")
    if wrap:
        # სცადე style="background-image:url(...)"
        style = wrap.get("style")
        if style:
            m = re.search(r"background-image\s*:\s*url\((.*?)\)", style, re.IGNORECASE)
            if m:
                url = m.group(1).strip("\"' ")
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith(("http://", "https://")):
                    return url
        # ან შიგნით არსებული <img>
        inner_img = wrap.find("img")
        if inner_img:
            src = (
                inner_img.get("src")
                or inner_img.get("data-src")
                or inner_img.get("data-lazy-src")
                or inner_img.get("data-original")
                or inner_img.get("data-background")
            )
            if not src and inner_img.get("srcset"):
                src = inner_img["srcset"].split(",")[0].split()[0]
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                if src.startswith(("http://", "https://")):
                    return src

    # 2) თუ ზუსტად ეგ wrapper არ არის, ვეძებ ნებისმიერ ელემენტს, რომელსაც აქვს background-image:url(...)
    any_bg = soup.find(style=re.compile(r"background-image\s*:\s*url\(", re.IGNORECASE))
    if any_bg:
        style = any_bg.get("style", "")
        m = re.search(r"background-image\s*:\s*url\((.*?)\)", style, re.IGNORECASE)
        if m:
            url = m.group(1).strip("\"' ")
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith(("http://", "https://")):
                return url

    # 3) fallback: og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        url = og["content"].strip()
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith(("http://", "https://")):
            return url

    return None

# ------------------------------
# Helper: HTML გაწმენდა
# ------------------------------
def clean_article(article):
    # წაშალე script/style/svg/noscript
    for tag in article(["script", "style", "svg", "noscript"]):
        tag.decompose()

    # გაასუფთავე ატრიბუტები
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
            src = (
                tag.get("src")
                or tag.get("data-src")
                or tag.get("data-lazy-src")
                or tag.get("data-original")
                or tag.get("data-background")
            )
            if not src and tag.get("srcset"):
                src = tag["srcset"].split(",")[0].split()[0]

            if src and src.startswith("//"):
                src = "https:" + src

            alt = tag.get("alt", "").strip() or "Image"
            tag.attrs = {"src": src or "", "alt": alt}
        else:
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

    # --- ბანერი მოძებნე wrapper-იდან ან style background-image-იდან ---
    banner_url = find_banner_url(soup)

    # article-ის თავში: ჯერ H1 (თუ არ ზის უკვე), მერე ბანერი <p><img .../></p>
    if h1:
        article.insert(0, h1)
    if banner_url:
        p = soup.new_tag("p")
        img = soup.new_tag("img", src=banner_url, alt="Banner")
        p.append(img)
        article.insert(1, p)

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
            title = soup.title.string.strip()
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
        # Images
        # ====================
        images = []

        # ბანერი (იმავე ლოგიკით, რაც ზედაა)
        banner_url = find_banner_url(soup)
        if banner_url:
            images.append(banner_url)

        # article-ის შიდა სურათები
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
