"""Download photos and videos from Pinterest or Instagram from a signed-in browser tab."""

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
from itertools import chain
from base64 import b64decode
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
import time
from urllib.parse import urlsplit

IMAGE_NAME = re.compile(r"([0-9a-f]{8,})\.(jpe?g|png|webp)", re.IGNORECASE)
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp")
VIDEO_EXTENSIONS = ("mp4", "webm")
MAX_VIDEO_BYTES = 256 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
POLL_MS = 2000
VIEWPORT_WAIT_MS = 12_000
DEFAULT_IDLE_SECONDS = 60
MAX_PROFILE_RELOADS = 2
CHROME_SETUP_URL = "chrome://inspect/#remote-debugging"
SAFARI_SETUP_URL = (
    "https://github.com/mahidhar347/browser-media-downloader#instagram-in-safari"
)


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def tab_key(url):
    parts = urlsplit(url)
    return parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query


def image_candidates(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "i.pinimg.com":
        return None
    name = parts.path.rsplit("/", 1)[-1]
    if not IMAGE_NAME.fullmatch(name):
        return None
    original = f"https://i.pinimg.com/originals/{name}"
    large = f"https://i.pinimg.com/736x/{name}"
    return original, tuple(dict.fromkeys((original, large, url)))


def image_extension(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    return None


def video_candidates(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in {"v.pinimg.com", "i.pinimg.com"}:
        return None
    extension = parts.path.rsplit(".", 1)[-1].lower()
    if extension in (*VIDEO_EXTENSIONS, "m3u8"):
        return parts._replace(query="", fragment="").geturl(), (url,)
    return None


def video_extension(data):
    if data[4:8] == b"ftyp":
        return "mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in data[:4096]:
        return "webm"
    return None


def read_resource(session, stream, max_bytes):
    data = bytearray()
    try:
        while True:
            chunk = session.send("IO.read", {"handle": stream, "size": 256 * 1024})
            encoded = chunk["data"]
            data.extend(b64decode(encoded) if chunk.get("base64Encoded") else encoded.encode())
            if len(data) > max_bytes:
                return None
            if chunk["eof"]:
                return data
    finally:
        session.send("IO.close", {"handle": stream})


def fetch(session, frame_id, urls, video=False):
    max_bytes = MAX_VIDEO_BYTES if video else MAX_IMAGE_BYTES
    reason = "no image returned"
    for url in urls:
        resource = session.send("Network.loadNetworkResource", {
            "frameId": frame_id,
            "url": url,
            "options": {"disableCache": False, "includeCredentials": False},
        })["resource"]
        stream = resource.get("stream")
        if not resource["success"] or not stream:
            status = resource.get("httpStatusCode")
            reason = f"HTTP {status}" if status else resource.get("netErrorName", reason)
            if stream:
                session.send("IO.close", {"handle": stream})
            continue
        data = read_resource(session, stream, max_bytes)
        if data is None:
            reason = f"file exceeds {max_bytes // (1024 * 1024)} MiB"
            continue
        extension = video_extension(data) if video else image_extension(data)
        if extension:
            return data, extension, None
        reason = "response did not contain the expected media format"
    return None, None, reason


def save_file(destination, data):
    with NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.close()
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)


def save_playlist(url, destination):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return "ffmpeg is required to save HLS videos"

    with NamedTemporaryFile(dir=destination.parent, suffix=".mp4", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        result = subprocess.run(
            [
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-y",
                "-protocol_whitelist", "https,tls,tcp,crypto", "-tls_verify", "1",
                "-rw_timeout", "15000000", "-i", url,
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-movflags", "+faststart", str(temp_path),
            ],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode or not temp_path.stat().st_size:
            return "ffmpeg could not read or save the HLS stream"
        temp_path.replace(destination)
        return None
    except subprocess.TimeoutExpired:
        return "HLS download timed out"
    finally:
        temp_path.unlink(missing_ok=True)


def download_file(session, frame_id, output, stem, choices, video):
    for url in choices:
        if video and urlsplit(url).path.lower().endswith(".m3u8"):
            destination = output / f"{stem}.mp4"
            reason = save_playlist(url, destination)
            if reason is None:
                return destination, None
        else:
            data, extension, reason = fetch(session, frame_id, (url,), video=video)
            if data is not None:
                destination = output / f"{stem}.{extension}"
                save_file(destination, data)
                return destination, None
    return None, reason


INSTALL_FEED_WATCH = r"""() => {
    const root = document.scrollingElement;
    if (!root) throw new Error("Pinterest has no scrolling document yet");
    root.scrollTo({top: 0, behavior: "instant"});
    const pending = new Map();
    const unresolvedVideos = new Set();
    const selector = "img, video, source, meta[property^='og:video']";

    function collect(node) {
        if (!(node instanceof Element)) return;
        if (node instanceof HTMLImageElement) {
            for (const url of [node.currentSrc, node.src, node.getAttribute("data-src")]) {
                if (url?.startsWith("https://i.pinimg.com/")) pending.set(url, false);
            }
            return;
        }
        const video = node instanceof HTMLVideoElement ? node : node.closest("video");
        const urls = video ? [
            video.currentSrc, video.src, video.getAttribute("data-src"),
            ...Array.from(video.querySelectorAll("source"), source => source.src),
        ] : node.matches("meta[property^='og:video']") ? [node.content] : [];
        const direct = urls.find(url =>
            /^https:\/\/(?:i|v)\.pinimg\.com\/.*\.(?:mp4|webm)(?:[?#]|$)/i.test(url));
        const playlist = urls.find(url =>
            /^https:\/\/(?:i|v)\.pinimg\.com\/.*\.m3u8(?:[?#]|$)/i.test(url));
        const media = direct || playlist;
        if (video) {
            const card = video.closest("[data-test-id='pin'], [data-test-id='pinWrapper']");
            const key = card?.querySelector("a[href*='/pin/']")?.getAttribute("href")
                || video.poster || video;
            if (media) unresolvedVideos.delete(key);
            else unresolvedVideos.add(key);
            if (video.poster) pending.delete(video.poster);
        }
        if (media) pending.set(media, true);
    }

    function collectTree(node) {
        collect(node);
        node.querySelectorAll?.(selector).forEach(collect);
    }

    const observer = new MutationObserver(records => {
        for (const record of records) {
            collect(record.target);
            record.addedNodes.forEach(collectTree);
            record.removedNodes.forEach(collectTree);
        }
    });
    observer.observe(document, {
        subtree: true, childList: true, attributes: true,
        attributeFilter: ["src", "srcset", "data-src", "poster", "content"],
    });
    const onLoad = event => {
        collect(event.target);
    };
    document.addEventListener("load", onLoad, true);
    document.addEventListener("loadedmetadata", onLoad, true);
    document.querySelectorAll(selector).forEach(collect);
    globalThis.__pinImageFeed = {
        sample() {
            let loading = false;
            document.querySelectorAll(selector).forEach(collect);
            for (const img of document.images) {
                if (!img.complete && !loading) {
                    const rect = img.getBoundingClientRect();
                    loading = rect.bottom > 0 && rect.top < root.clientHeight;
                }
            }
            const urls = Array.from(pending.entries());
            pending.clear();
            return [urls, root.scrollTop, root.scrollHeight, root.clientHeight, loading, unresolvedVideos.size];
        },
        advance() {
            root.scrollBy({top: Math.max(1, root.clientHeight * 0.55), behavior: "instant"});
        },
        stop() {
            observer.disconnect();
            document.removeEventListener("load", onLoad, true);
            document.removeEventListener("loadedmetadata", onLoad, true);
            delete globalThis.__pinImageFeed;
        },
    };
}"""


def download_media(page, session, output, limit, idle_seconds=DEFAULT_IDLE_SECONDS):
    page.wait_for_load_state("domcontentloaded", timeout=idle_seconds * 1000)
    frame_id = session.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
    page.evaluate(INSTALL_FEED_WATCH)
    try:
        return scan_feed(page, session, frame_id, output, limit, idle_seconds)
    finally:
        if not page.is_closed():
            page.evaluate("() => globalThis.__pinImageFeed?.stop()")


def scan_feed(page, session, frame_id, output, limit, idle_seconds):
    seen = set()
    saved = photos = videos = skipped = failed = unresolved = 0
    quiet_ms = viewport_ms = 0
    previous_position = None
    print("Starting at the top. Waiting for Pinterest to load media.", flush=True)

    while limit is None or saved < limit:
        page.wait_for_timeout(POLL_MS)
        urls, top, height, viewport, loading, unresolved = page.evaluate("() => globalThis.__pinImageFeed.sample()")
        position = (top, height, viewport)
        new_media = {}
        for url, video in urls:
            candidates = video_candidates(url) if video else image_candidates(url)
            if candidates:
                original, choices = candidates
                if original not in seen:
                    new_media.setdefault((original, video), {}).update(dict.fromkeys(choices))

        for (original, video), choices in new_media.items():
            seen.add(original)
            stem = hashlib.sha1(original.encode()).hexdigest()[:16]
            extensions = VIDEO_EXTENSIONS if video else IMAGE_EXTENSIONS
            if any((output / f"{stem}.{ext}").is_file() for ext in extensions):
                skipped += 1
                continue

            destination, reason = download_file(session, frame_id, output, stem, choices, video)
            if destination is None:
                page.wait_for_timeout(POLL_MS)
                destination, reason = download_file(session, frame_id, output, stem, choices, video)
            if destination is None:
                failed += 1
                print(f"Failed after retry: {original} ({reason})", file=sys.stderr)
                continue

            saved += 1
            videos += int(video)
            photos += int(not video)
            print(f"[{saved:>4}] {destination.name}  {destination.stat().st_size / 1024:.1f} KiB", flush=True)
            if saved == limit:
                break

        if saved == limit:
            print(f"Reached the limit of {limit} new files.", flush=True)
            break

        quiet_ms = quiet_ms + POLL_MS if not new_media and position == previous_position else 0
        same_viewport = previous_position is not None and top == previous_position[0]
        viewport_ms = viewport_ms + POLL_MS if same_viewport else POLL_MS
        previous_position = position
        at_bottom = top + viewport >= height - 2
        if quiet_ms >= idle_seconds * 1000:
            location = "at the bottom" if at_bottom else "while scrolling"
            print(f"No new files or scroll progress for {idle_seconds}s {location}.", flush=True)
            break
        if at_bottom and quiet_ms == POLL_MS:
            print(f"Waiting up to {idle_seconds}s for more results.", flush=True)
        waiting_for_media = (loading or not seen) and viewport_ms < VIEWPORT_WAIT_MS
        if not at_bottom and not waiting_for_media:
            page.evaluate("() => globalThis.__pinImageFeed.advance()")

    print(f"Saved {photos} photos and {videos} videos to {output} "
          f"({skipped} already present, {failed} failed).")
    if unresolved:
        print(f"{unresolved} video players exposed no downloadable file or HLS playlist URL.",
              file=sys.stderr)
    if not seen and not unresolved:
        print("No supported Pinterest media found in the tab.", file=sys.stderr)
    return 0 if seen and not failed and not unresolved else 1



INSTAGRAM_HOSTS = {"www.instagram.com", "instagram.com"}
POST_PATH = re.compile(r"/(?:p|reels?|tv)/([A-Za-z0-9_-]+)/?$")

SAFARI_SCRIPT = """on run argv
    set requestedURL to item 1 of argv
    set javascriptCode to item 2 of argv
    tell application id "com.apple.Safari"
        repeat with browserWindow in windows
            repeat with browserTab in tabs of browserWindow
                if (URL of browserTab as text) is requestedURL then
                    if (count of argv) > 2 then
                        set URL of browserTab to requestedURL
                        return "true"
                    end if
                    return do JavaScript javascriptCode in browserTab
                end if
            end repeat
        end repeat
    end tell
    error "Open the requested Instagram URL in Safari and copy the exact address."
end run"""

SAFARI_WORKER_SCRIPT = """on run argv
    set operation to item 1 of argv
    set requestedURL to item 2 of argv
    tell application id "com.apple.Safari"
        if operation is "open" then
            set postURL to item 3 of argv
            repeat with browserWindow in windows
                repeat with browserTab in tabs of browserWindow
                    if (URL of browserTab as text) is requestedURL then
                        set workerTab to make new tab at end of tabs of browserWindow
                        set URL of workerTab to postURL
                        return ((id of browserWindow as text) & ":" & ((count of tabs of browserWindow) as text))
                    end if
                end repeat
            end repeat
            error "The Instagram profile tab is no longer open at the supplied URL."
        end if

        set windowNumber to (item 3 of argv) as integer
        set tabNumber to (item 4 of argv) as integer
        set workerTab to tab tabNumber of window id windowNumber
        if operation is "status" then return URL of workerTab as text
        if (URL of workerTab as text) is not requestedURL then
            error "Open the requested Instagram URL in Safari: the download tab has not reached the post."
        end if
        if operation is "close" then
            close workerTab
            return ""
        end if
        if operation is "navigate" then
            set URL of workerTab to item 5 of argv
            return ""
        end if
        return do JavaScript (item 5 of argv) in workerTab
    end tell
end run"""


PROFILE_SCAN = r"""(() => {
    const grid = document.querySelector("main, [role=main]") || document;
    const links = Array.from(grid.querySelectorAll("a[href]"), link => new URL(link.href))
        .filter(url => url.origin === location.origin)
        .map(url => url.pathname.match(/\/(?:p|reels?|tv)\/[A-Za-z0-9_-]+\/?$/))
        .filter(Boolean)
        .map(match => location.origin + match[0].replace(/\/?$/, "/"));
    const root = document.scrollingElement;
    const loading = Array.from(grid.querySelectorAll('[role="progressbar"], [aria-busy="true"]'))
        .some(element => element.getClientRects().length);
    return {links, top: root.scrollTop, height: root.scrollHeight,
        viewport: root.clientHeight, loading, hidden: document.hidden};
})()"""

POST_VIEW = r"""(advance => {
    const article = document.querySelector("main article, article, main");
    if (!article) return null;
    const next = article.querySelector(
        'button[aria-label^="Next" i], [role="button"][aria-label^="Next" i], ' +
        'button:has(svg[aria-label^="Next" i]), [role="button"]:has(svg[aria-label^="Next" i])'
    );
    const canAdvance = Boolean(next && !next.disabled &&
        next.getAttribute("aria-disabled") !== "true" && next.getClientRects().length);
    if (advance) {
        if (canAdvance) next.click();
        return canAdvance;
    }

    function visibleWidth(element) {
        const rect = element.getBoundingClientRect();
        if (rect.width < 120 || rect.height < 120) return 0;
        if (element.checkVisibility && !element.checkVisibility({
            checkOpacity: true, checkVisibilityCSS: true
        })) return 0;
        let left = Math.max(0, rect.left);
        let right = Math.min(innerWidth, rect.right);
        for (let parent = element.parentElement; parent; parent = parent.parentElement) {
            if (getComputedStyle(parent).overflowX !== "visible") {
                const clip = parent.getBoundingClientRect();
                left = Math.max(left, clip.left);
                right = Math.min(right, clip.right);
            }
            if (parent === article) break;
        }
        return Math.max(0, right - left) / rect.width;
    }

    function isCDN(value) {
        try {
            const url = new URL(value);
            return url.protocol === "https:" && /(?:^|\.)(?:cdninstagram\.com|fbcdn\.net)$/.test(url.hostname);
        } catch {
            return false;
        }
    }

    const elements = Array.from(article.querySelectorAll("img, video"));
    const visible = elements.map((element, index) => ({
        element, index, fraction: visibleWidth(element)
    })).filter(item => item.fraction >= 0.6);
    const active = visible.find(item => item.element instanceof HTMLVideoElement) ||
        visible.sort((a, b) => b.fraction - a.fraction)[0];
    if (!active) return null;

    const element = active.element;
    const video = element instanceof HTMLVideoElement;
    const urls = video ? [
        element.currentSrc, element.src, element.getAttribute("data-src"),
        ...Array.from(element.querySelectorAll("source"), source => source.src),
    ] : [element.currentSrc || element.src];
    if (video && elements.filter(item => item instanceof HTMLVideoElement).length === 1 &&
        !canAdvance && !article.querySelector('[aria-label^="Previous" i]')) {
        urls.push(...Array.from(document.querySelectorAll('meta[property^="og:video"]'),
            meta => meta.content));
        const files = performance.getEntriesByType("resource").map(entry => entry.name)
            .filter(value => isCDN(value) && /\.mp4(?:[?#]|$)/i.test(value))
            .filter(value => !/[?&](?:bytestart|byteend|range)=/i.test(value));
        const distinct = new Map(files.map(value => [new URL(value).pathname, value]));
        if (distinct.size === 1) urls.push(...distinct.values());
    }
    const candidates = Array.from(new Set(urls.filter(isCDN)));
    if (!video && !candidates.length) return null;
    const pageMarker = article.querySelector('[aria-current="true"], [aria-current="step"], [aria-selected="true"]');
    return {
        video, candidates, next: canAdvance,
        marker: [active.index, pageMarker?.getAttribute("aria-label") ||
            pageMarker?.parentElement && Array.from(pageMarker.parentElement.children).indexOf(pageMarker),
            element.getAttribute("data-index"), element.poster || ""],
    };
})"""


def cdn_url(value):
    parts = urlsplit(value)
    return parts.scheme == "https" and (
        parts.netloc.endswith(".cdninstagram.com") or
        parts.netloc.endswith(".fbcdn.net") or
        parts.netloc in {"cdninstagram.com", "fbcdn.net"}
    )


def safari_command(script, *args):
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script, *args],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Safari returned an Apple Events error")
    return result.stdout.strip()


def instagram_post_key(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in INSTAGRAM_HOSTS:
        return None
    match = POST_PATH.search(parts.path)
    return match.group(1) if match else None


class SafariTab:
    def __init__(self, url, worker_id=None):
        self.url = url
        self.worker_id = worker_id

    def worker_url(self):
        actual = safari_command(SAFARI_WORKER_SCRIPT, "status", "", *self.worker_id)
        if instagram_post_key(actual) != instagram_post_key(self.url):
            path = urlsplit(actual).path or actual
            raise RuntimeError(
                f"Safari's download tab has not reached the post {self.url}; current location: {path}."
            )
        return actual

    def evaluate(self, javascript):
        code = "JSON.stringify(" + javascript + ")"
        if self.worker_id:
            actual = self.worker_url()
            result = safari_command(SAFARI_WORKER_SCRIPT, "evaluate", actual, *self.worker_id, code)
        else:
            result = safari_command(SAFARI_SCRIPT, self.url, code)
        if not result:
            raise RuntimeError("Safari returned an empty result while loading the tab.")
        return json.loads(result)

    def navigate(self, url):
        actual = self.worker_url()
        safari_command(SAFARI_WORKER_SCRIPT, "navigate", actual, *self.worker_id, url)
        self.url = url

    def reload(self):
        if self.worker_id:
            self.navigate(self.url)
        else:
            safari_command(SAFARI_SCRIPT, self.url, "", "reload")

    @contextmanager
    def post_tab(self, first_url):
        identifier = safari_command(SAFARI_WORKER_SCRIPT, "open", self.url, first_url)
        worker = SafariTab(first_url, identifier.split(":"))
        try:
            yield worker
        finally:
            try:
                actual = worker.worker_url()
                safari_command(SAFARI_WORKER_SCRIPT, "close", actual, *worker.worker_id)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                print("Safari's download tab could not be closed automatically.", file=sys.stderr)


class ChromeTab:
    def __init__(self, page):
        self.page = page

    def evaluate(self, javascript):
        from playwright.sync_api import Error as PlaywrightError

        try:
            return self.page.evaluate(javascript)
        except PlaywrightError as error:
            raise RuntimeError(str(error)) from error

    def navigate(self, url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    def reload(self):
        self.page.reload(wait_until="domcontentloaded", timeout=60_000)

    @contextmanager
    def post_tab(self, first_url):
        page = self.page.context.new_page()
        try:
            worker = ChromeTab(page)
            worker.navigate(first_url)
            yield worker
        finally:
            if not page.is_closed():
                page.close()


@contextmanager
def open_instagram_tab(browser, url):
    if browser == "safari":
        if sys.platform != "darwin":
            raise RuntimeError("Safari mode requires macOS.")
        tab = SafariTab(url)
        tab.evaluate("location.href")
        yield tab
        return

    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    try:
        with sync_playwright() as playwright:
            chrome = playwright.chromium.connect_over_cdp("chrome", no_defaults=True, timeout=60_000)
            tabs = [page for context in chrome.contexts for page in context.pages
                    if page.url == url]
            if len(tabs) != 1:
                raise RuntimeError("Open exactly one Chrome tab at the supplied Instagram URL.")
            yield ChromeTab(tabs[0])
    except PlaywrightError as error:
        raise RuntimeError(str(error)) from error


def is_transient_tab_error(error):
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    message = str(error).lower()
    return any(text in message for text in (
        "execution context was destroyed",
        "cannot find context with specified id",
        "has not reached the post",
        "safari returned an empty result while loading the tab",
    ))


def iter_posts(tab, idle_seconds, resume_key, completed, pending, queue_posts, recheck=False):
    key = json.dumps(resume_key)
    resumed = tab.evaluate(f"""(() => {{
        const root = document.scrollingElement;
        const checkpoint = globalThis.__mediaDownloadPosition;
        if (!{json.dumps(recheck)} && checkpoint?.key === {key} &&
            Math.abs(root.scrollTop - checkpoint.top) < 2)
            return true;
        root.scrollTo({{top: 0, behavior: "instant"}});
        globalThis.__mediaDownloadPosition = {{key: {key}, top: root.scrollTop}};
        return false;
    }})()""")
    if resumed:
        print("Continuing from the current position in the open profile tab.", flush=True)
    else:
        print("Scanning profile links from the top. Completed posts will be skipped.", flush=True)
    seen = completed.union(pending)
    yield from pending
    last_position = None
    last_progress = time.monotonic()
    reloads = 0

    def reload_profile():
        nonlocal reloads, last_position, last_progress
        reloads += 1
        print(f"Instagram stopped responding. Reloading the profile "
              f"({reloads}/{MAX_PROFILE_RELOADS}) and keeping the saved queue.", flush=True)
        tab.reload()
        last_position = None
        last_progress = time.monotonic()
        time.sleep(2)

    while True:
        try:
            state = tab.evaluate(PROFILE_SCAN)
            position = state["top"], state["height"]
            if position != last_position:
                last_progress = time.monotonic()
            last_position = position
            new_posts = [post for post in dict.fromkeys(state["links"]) if post not in seen]
            if new_posts:
                queue_posts(new_posts)
                seen.update(new_posts)
                for post in new_posts:
                    yield post
                    last_progress = time.monotonic()
            if time.monotonic() - last_progress >= idle_seconds:
                at_bottom = state["top"] + state["viewport"] >= state["height"] - 2
                stalled = not at_bottom or state["loading"] or not state["links"]
                if (stalled or (state["hidden"] and not reloads)) and reloads < MAX_PROFILE_RELOADS:
                    reload_profile()
                    continue
                if stalled:
                    raise RuntimeError("Instagram is still stalled. Reopen the profile and rerun "
                                       "the command to resume from the saved checkpoint.")
                tab.evaluate("delete globalThis.__mediaDownloadPosition")
                return
            tab.evaluate(f"""(() => {{
                const root = document.scrollingElement;
                root.scrollBy({{top: root.clientHeight * 0.6, behavior: "instant"}});
                globalThis.__mediaDownloadPosition = {{key: {key}, top: root.scrollTop}};
                return true;
            }})()""")
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            if not is_transient_tab_error(error) or reloads >= MAX_PROFILE_RELOADS:
                raise
            reload_profile()
        time.sleep(1.5)


def photo_extension(file):
    with file.open("rb") as source:
        head = source.read(16)
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "avif"
    return None


PERSON_COUNTER = """import Foundation
import Vision

let file = URL(fileURLWithPath: CommandLine.arguments[1])
do {
    let people = VNDetectHumanRectanglesRequest()
    people.upperBodyOnly = true
    let faces = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(data: try Data(contentsOf: file))
    try handler.perform([people, faces])
    let bodies = (people.results ?? []).map { $0.boundingBox }
    let faceBoxes = (faces.results ?? []).map { $0.boundingBox }
    let standaloneFaces = faceBoxes.filter { face in
        let center = CGPoint(x: face.midX, y: face.midY)
        return !bodies.contains { $0.insetBy(dx: -0.01, dy: -0.01).contains(center) }
    }
    print(max(faceBoxes.count, bodies.count + standaloneFaces.count))
} catch {
    FileHandle.standardError.write(Data(("Person detection failed: " + error.localizedDescription).utf8))
    exit(1)
}
"""


@contextmanager
def person_counter():
    if sys.platform != "darwin":
        raise RuntimeError("The one-person photo filter requires macOS.")
    with TemporaryDirectory(prefix="instagram-vision-") as directory:
        source = Path(directory) / "count_people.swift"
        binary = source.with_suffix("")
        source.write_text(PERSON_COUNTER)
        compiler = subprocess.run(
            ["/usr/bin/swiftc", str(source), "-o", str(binary)],
            capture_output=True, text=True, timeout=120,
        )
        if compiler.returncode:
            detail = compiler.stderr.strip()
            raise RuntimeError(f"Could not build the Apple Vision helper. "
                               f"macOS Command Line Tools are required. {detail}")

        def count_people(path):
            result = subprocess.run([str(binary), str(path)], capture_output=True,
                                    text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Apple Vision could not count people.")
            return int(result.stdout.strip())

        yield count_people


def save_instagram_file(url, directory, stem, video, count_people=None):
    if not cdn_url(url):
        return None
    if video and urlsplit(url).path.lower().endswith(".m3u8"):
        destination = directory / f"{stem}.mp4"
        reason = save_playlist(url, destination)
        if reason:
            raise RuntimeError(reason)
        return destination
    with NamedTemporaryFile(dir=directory, delete=False) as temporary:
        part = Path(temporary.name)
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "--disable", "--silent", "--show-error", "--fail",
             "--proto", "=https", "--max-time", "30", "--retry", "1",
             "--max-filesize", str(MAX_VIDEO_BYTES if video else MAX_IMAGE_BYTES), "--output", str(part), "--", url],
            capture_output=True, text=True, timeout=65,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Instagram CDN request failed")
        if video:
            with part.open("rb") as source:
                extension = video_extension(source.read(4096))
        else:
            extension = photo_extension(part)
        if not extension:
            raise RuntimeError("the CDN response was not the expected media format")
        if not video and count_people is not None:
            people = count_people(part)
            if people != 1:
                print(f"Skipped {stem}: detected {people} people.", flush=True)
                return None
        destination = directory / f"{stem}.{extension}"
        part.replace(destination)
        return destination
    finally:
        part.unlink(missing_ok=True)


def slide_key(state):
    return (
        state["video"], tuple(state["marker"]),
        tuple(urlsplit(url).path for url in state["candidates"]),
    )


def wait_for_post(tab, previous=None):
    deadline = time.monotonic() + 25
    video_grace = time.monotonic() + 5
    last_sample = None
    last_navigation_error = None
    while time.monotonic() < deadline:
        try:
            state = tab.evaluate(f"{POST_VIEW}(false)")
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            if not is_transient_tab_error(error):
                raise
            last_navigation_error = error
        else:
            last_navigation_error = None
            if state:
                sample = slide_key(state)
                source_ready = state["candidates"] or time.monotonic() >= video_grace
                if sample != previous and sample == last_sample and source_ready:
                    return state
                last_sample = sample
            else:
                last_sample = None
        time.sleep(0.5)
    if last_navigation_error:
        raise last_navigation_error
    return None


def has_valid_instagram_media(path, video):
    """Check the media header before reusing an earlier download."""
    if not path.is_file():
        return False
    try:
        if not video:
            return photo_extension(path) is not None
        with path.open("rb") as file:
            return video_extension(file.read(4096)) is not None
    except OSError:
        return False


def download_post(tab, post_url, output, limit=None, count_people=None):
    saved, failures, complete, stalled = read_instagram_post(
        tab, post_url, output, limit, count_people
    )
    if not stalled or saved == limit:
        return saved, failures, complete
    print("Instagram post stopped rendering. Reloading it once.", flush=True)
    tab.reload()
    remaining = None if limit is None else limit - saved
    added, errors, complete, _ = read_instagram_post(
        tab, post_url, output, remaining, count_people
    )
    return saved + added, errors, complete


def read_instagram_post(tab, post_url, output, limit, count_people):
    shortcode = urlsplit(post_url).path.strip("/").split("/")[-1]
    seen_files = set()
    seen_slides = set()
    saved = failures = slides = 0
    try:
        state = wait_for_post(tab)
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        if not is_transient_tab_error(error):
            raise
        state = None
    if state is None:
        print(f"{shortcode}: post media did not render in time.", file=sys.stderr)
        return 0, 1, False, True

    while state:
        key = slide_key(state)
        if key in seen_slides:
            print(f"{shortcode}: carousel returned to a slide already visited.", file=sys.stderr)
            failures += 1
            break
        seen_slides.add(key)
        slides += 1

        video = state["video"]
        extensions = VIDEO_EXTENSIONS if video else ("jpg", "png", "webp", "avif")
        reason = "no downloadable photo URL was exposed"
        if video:
            reason = "the player exposed no downloadable video file or playlist"
        for url in state["candidates"]:
            if not cdn_url(url):
                continue
            path = urlsplit(url).path
            digest = hashlib.sha256(path.encode()).hexdigest()[:12]
            stem = f"{shortcode}-{digest}"
            if path in seen_files or any(has_valid_instagram_media(output / f"{stem}.{ext}", video)
                                         for ext in extensions):
                print(f"{shortcode}, slide {slides}: already saved.", flush=True)
                break
            try:
                destination = save_instagram_file(
                    url, output, stem, video, None if video else count_people
                )
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                reason = str(error)
                continue
            if destination:
                seen_files.add(path)
                saved += 1
                print(f"Saved {destination.name} (slide {slides})", flush=True)
                if saved == limit:
                    return saved, failures, not state["next"] and not failures, False
            break
        else:
            failures += 1
            print(f"{shortcode}, slide {slides}: {reason}", file=sys.stderr)

        if not state["next"]:
            break
        try:
            advanced = tab.evaluate(f"{POST_VIEW}(true)")
            state = wait_for_post(tab, key) if advanced else None
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            if not is_transient_tab_error(error):
                raise
            state = None
        if state is None:
            print(f"{shortcode}: the slide after {slides} did not render in time.", file=sys.stderr)
            return saved, failures + 1, False, True
    print(f"{shortcode}: checked {slides} slide{'s' if slides != 1 else ''}.", flush=True)
    return saved, failures, not failures, False


def download_instagram(url, browser, output, limit, idle_seconds, count_people=None,
                       recheck=False):
    saved = failed = 0
    output.mkdir(parents=True, exist_ok=True)
    with open_instagram_tab(browser, url) as tab:
        if instagram_post_key(url):
            saved, failed, _ = download_post(tab, url, output, limit, count_people)
        else:
            saved, failed = download_profile(tab, url, output, limit, idle_seconds, count_people,
                                            recheck=recheck)
    print(f"Saved {saved} new Instagram files to {output}. Failures: {failed}.")
    return int(bool(failed))


def download_profile(tab, url, output, limit, idle_seconds, count_people, recheck=False):
    checkpoint = output / ".instagram-progress.sqlite3"
    scope = json.dumps(tab_key(url))
    resume_key = hashlib.sha256(f"{checkpoint}:{scope}".encode()).hexdigest()
    saved = failed = 0
    with closing(sqlite3.connect(checkpoint, isolation_level=None)) as progress:
        progress.execute("""CREATE TABLE IF NOT EXISTS posts (
            profile TEXT NOT NULL, url TEXT NOT NULL, complete INTEGER NOT NULL,
            PRIMARY KEY (profile, url)
        )""")
        # Preserve the existing database format. In addition to queued (0) and
        # completed (1), track failed (2) and in-progress (3) posts.
        if recheck:
            progress.execute("UPDATE posts SET complete = 0 WHERE profile = ? AND complete = 1",
                             (scope,))
        status = dict(progress.execute("""SELECT url, complete FROM posts WHERE profile = ?
            ORDER BY CASE complete WHEN 3 THEN 0 WHEN 0 THEN 1 ELSE 2 END, rowid""", (scope,)))
        completed = {post for post, phase in status.items() if phase == 1}
        pending = [post for post, phase in status.items() if phase != 1]
        if status:
            print(f"Checkpoint: {len(completed)} completed posts, {len(pending)} pending.", flush=True)

        def queue_posts(links):
            progress.executemany("INSERT OR IGNORE INTO posts VALUES (?, ?, 0)",
                                 ((scope, post) for post in links))

        posts = iter_posts(tab, idle_seconds, resume_key, completed, pending, queue_posts, recheck)
        first = next(posts, None)
        if first is None:
            if completed:
                print("No new Instagram posts found.", flush=True)
                return 0, 0
            raise RuntimeError("No post links found. Open the profile's Posts tab and wait for it to load.")
        with tab.post_tab(first) as worker:
            for index, post in enumerate(chain((first,), posts), 1):
                progress.execute("UPDATE posts SET complete = 3 WHERE profile = ? AND url = ?",
                                 (scope, post))
                print(f"Post {index}: {post}", flush=True)
                if index > 1:
                    worker.navigate(post)
                remaining = None if limit is None else limit - saved
                count, errors, complete = download_post(worker, post, output, remaining, count_people)
                if complete or errors:
                    progress.execute("UPDATE posts SET complete = ? WHERE profile = ? AND url = ?",
                                     (1 if complete else 2, scope, post))
                saved += count
                failed += errors
                if saved == limit:
                    break
        remaining_posts = progress.execute(
            "SELECT count(*) FROM posts WHERE profile = ? AND complete != 1", (scope,)
        ).fetchone()[0]
        print(f"Checkpoint saved. Known Instagram posts still pending: {remaining_posts}.", flush=True)
    return saved, failed


def show_browser_setup(browser):
    if browser == "chrome":
        print(f"In Google Chrome, open {CHROME_SETUP_URL} and enable remote debugging.")
        print("When Chrome asks to allow a connection, review the request.")
        app, url = "Google Chrome", CHROME_SETUP_URL
    else:
        print("In Safari, choose Safari > Settings > Advanced.")
        print("Turn on Show features for web developers.")
        print("Then go to Safari > Settings > Developer > Automation and enable "
              "Allow JavaScript from Apple Events.")
        app, url = "Safari", SAFARI_SETUP_URL

    if sys.platform != "darwin":
        if browser == "safari":
            print("Safari mode requires macOS.", file=sys.stderr)
            return 1
        return 0
    try:
        subprocess.run(["/usr/bin/open", "-a", app, url], check=True)
    except (OSError, subprocess.CalledProcessError):
        print(f"Could not open {app}. Open this address manually: {url}", file=sys.stderr)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="Exact URL of the open Pinterest or Instagram tab")
    parser.add_argument("output_or_max", nargs="?", metavar="OUTPUT_OR_MAX",
                        help="Output directory or file limit")
    parser.add_argument("max", type=positive_int, nargs="?",
                        help="Optional maximum number of new files (default: unlimited)")
    parser.add_argument("--browser", choices=("safari", "chrome"),
                        help="Default: Safari for Instagram, Chrome for Pinterest")
    parser.add_argument("--setup", choices=("safari", "chrome"),
                        help="Show the browser settings steps and open the setup page on macOS")
    parser.add_argument("--recheck", action="store_true",
                        help="Revisit completed Instagram profile posts and check saved files")
    parser.add_argument("--all", action="store_true",
                        help="Keep scanning until the feed stops loading (default)")
    parser.add_argument("--idle-seconds", type=positive_int, default=DEFAULT_IDLE_SECONDS,
                        help="Seconds to wait for a stalled feed (default: %(default)s)")
    args = parser.parse_args()
    if args.setup:
        if args.url:
            parser.error("--setup is a separate command and does not take a media URL")
        return show_browser_setup(args.setup)
    if not args.url:
        parser.error("a media URL is required unless you are using --setup")
    parts = urlsplit(args.url)
    if parts.scheme != "https" or not parts.hostname:
        parser.error("pass the HTTPS address of the open browser tab")
    instagram = parts.netloc in INSTAGRAM_HOSTS
    if args.recheck and (not instagram or instagram_post_key(args.url)):
        parser.error("--recheck requires an Instagram profile URL")
    browser_name = args.browser or ("safari" if instagram else "chrome")
    output = Path("instagram-images" if instagram else "pinterest-images")
    limit = args.max
    if args.output_or_max is not None:
        if args.max is None and re.fullmatch(r"[+-]?[0-9]+", args.output_or_max):
            try:
                limit = positive_int(args.output_or_max)
            except argparse.ArgumentTypeError as error:
                parser.error(str(error))
        else:
            output = Path(args.output_or_max)

    if args.all:
        if args.max is not None or (
            args.output_or_max is not None and re.fullmatch(r"[+-]?[0-9]+", args.output_or_max)
        ):
            parser.error("--all cannot be combined with an file limit")
        limit = None

    if instagram:
        try:
            with person_counter() as count_people:
                return download_instagram(
                    args.url, browser_name, output.expanduser().resolve(), limit,
                    args.idle_seconds, count_people, recheck=args.recheck
                )
        except ImportError:
            print('Chrome mode requires python3 -m pip install "playwright>=1.60".',
                  file=sys.stderr)
            return 1
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"Instagram download stopped: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Stopped. Saved photos are available in the output directory.", file=sys.stderr)
            return 130

    if browser_name != "chrome":
        parser.error("Pinterest mode requires --browser chrome")

    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError:
        print('Install Playwright with python3 -m pip install "playwright>=1.60".',
              file=sys.stderr)
        return 1

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    "chrome", no_defaults=True, timeout=60_000
                )
            except PlaywrightError as error:
                print(
                    "Could not attach to Chrome. In Chrome 144 or later, enable remote "
                    "debugging at chrome://inspect/#remote-debugging and allow the "
                    f"connection when Chrome prompts.\n{error}",
                    file=sys.stderr,
                )
                return 1

            target = tab_key(args.url)
            matching_pages = [
                page for context in browser.contexts for page in context.pages
                if tab_key(page.url) == target
            ]
            if not matching_pages:
                print(
                    "Open the Pinterest URL in your signed-in Chrome profile and pass "
                    "the URL shown in the address bar.",
                    file=sys.stderr,
                )
                return 1
            if len(matching_pages) > 1:
                print("Leave only one tab open with this URL so the correct profile is used.",
                      file=sys.stderr)
                return 1

            output = output.expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
            page = matching_pages[0]
            session = page.context.new_cdp_session(page)
            return download_media(page, session, output, limit, args.idle_seconds)
    except (OSError, PlaywrightError, ValueError) as error:
        print(f"Download stopped: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped. Files already saved are available in the output directory.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
