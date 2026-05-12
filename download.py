#!/usr/bin/env python3
import argparse
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlparse

import certifi
import requests
from selenium.common.exceptions import SessionNotCreatedException, TimeoutException  # pyright: ignore[reportMissingImports]
from selenium.webdriver.common.by import By  # pyright: ignore[reportMissingImports]
from selenium.webdriver.support import expected_conditions as EC  # pyright: ignore[reportMissingImports]
from selenium.webdriver.support.ui import WebDriverWait  # pyright: ignore[reportMissingImports]


DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_DELAY_SECONDS = 10
DEFAULT_MAX_RETRIES = 5
DEFAULT_RATE_LIMIT_DELAY_SECONDS = 120
SKIP_HREFS = {"", "../", "./", "#"}
CHROME_BINARY_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
CHROME_COMMAND_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
)


def configure_tls_certificates() -> None:
    ca_bundle = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", ca_bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_bundle)

    def create_certifi_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        kwargs.setdefault("cafile", ca_bundle)
        return ssl.create_default_context(*args, **kwargs)

    ssl._create_default_https_context = create_certifi_context


def load_undetected_chromedriver() -> Any:
    configure_tls_certificates()

    try:
        import distutils.version  # pyright: ignore[reportMissingImports]  # noqa: F401
    except ModuleNotFoundError:
        from setuptools._distutils.version import LooseVersion  # pyright: ignore[reportMissingImports]

        distutils_module = types.ModuleType("distutils")
        version_module = types.ModuleType("distutils.version")
        version_module.LooseVersion = LooseVersion
        distutils_module.version = version_module
        sys.modules["distutils"] = distutils_module
        sys.modules["distutils.version"] = version_module

    import undetected_chromedriver as uc  # pyright: ignore[reportMissingImports]

    return uc


def safe_filename(url: str) -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return name or "download"


def episode_key(filename: str) -> str | None:
    match = re.search(
        r"(?i)(?<![a-z0-9])s0*(\d{1,2})[\s._-]*e0*(\d{1,3})(?![a-z0-9])",
        filename,
    )
    if not match:
        return None

    season = int(match.group(1))
    episode = int(match.group(2))
    return f"S{season:02d}E{episode:02d}"


def find_chrome_binary(chrome_binary: str | None) -> str:
    if chrome_binary:
        path = Path(chrome_binary).expanduser()
        if path.exists():
            return str(path)
        raise RuntimeError(f"Chrome binary does not exist: {path}")

    for candidate in CHROME_BINARY_CANDIDATES:
        if Path(candidate).exists():
            return candidate

    for command in CHROME_COMMAND_CANDIDATES:
        found = shutil.which(command)
        if found:
            return found

    raise RuntimeError(
        "Could not find Chrome, Chromium, Brave, or Edge. Install Google Chrome, "
        "or pass the browser path with --chrome-binary."
    )


def get_browser_major_version(browser_path: str) -> int | None:
    try:
        result = subprocess.run(
            [browser_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    version_output = f"{result.stdout} {result.stderr}"
    match = re.search(r"\b(\d+)\.\d+\.\d+\.\d+\b", version_output)
    if not match:
        return None

    return int(match.group(1))


def build_driver(headless: bool, chrome_binary: str | None) -> Any:
    uc = load_undetected_chromedriver()
    browser_path = find_chrome_binary(chrome_binary)
    browser_major = get_browser_major_version(browser_path)
    options = uc.ChromeOptions()
    options.binary_location = browser_path
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-popup-blocking")

    chrome_kwargs = {
        "options": options,
        "headless": headless,
        "browser_executable_path": browser_path,
        "use_subprocess": True,
    }
    if browser_major is not None:
        chrome_kwargs["version_main"] = browser_major

    return uc.Chrome(**chrome_kwargs)


def wait_for_listing(driver: Any, timeout: int) -> None:
    print("Waiting for directory listing. Complete any browser challenge if prompted.")
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href]"))
    )


def collect_file_links(driver: Any, base_url: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()

    for anchor in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
        href = anchor.get_attribute("href") or ""
        raw_href = anchor.get_dom_attribute("href") or ""

        if raw_href in SKIP_HREFS or href in SKIP_HREFS:
            continue
        if raw_href.endswith("/") or href.endswith("/"):
            continue

        absolute = urljoin(base_url, href)
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)

    return links


def filter_links_by_filename_prefix(urls: Iterable[str], prefix: str | None) -> list[str]:
    queued_urls = list(urls)
    if not prefix:
        return queued_urls

    return [url for url in queued_urls if safe_filename(url).startswith(prefix)]


def copy_browser_session(driver: Any) -> requests.Session:
    session = requests.Session()
    user_agent = driver.execute_script("return navigator.userAgent")
    session.headers.update({"User-Agent": user_agent})

    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )

    return session


def retry_after_seconds(response: requests.Response, fallback_seconds: int) -> int:
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return fallback_seconds

    if retry_after.isdigit():
        return max(1, int(retry_after))

    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError):
        return fallback_seconds

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    wait_seconds = int((retry_at - datetime.now(timezone.utc)).total_seconds())
    return max(1, wait_seconds)


def format_bytes(size: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"


def content_length(response: requests.Response) -> int | None:
    value = response.headers.get("Content-Length")
    if not value:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def content_range_total(response: requests.Response) -> int | None:
    value = response.headers.get("Content-Range")
    if not value:
        return None

    match = re.match(r"bytes\s+\d+-\d+/(\d+|\*)$", value)
    if not match or match.group(1) == "*":
        return None

    return int(match.group(1))


def truncate_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]

    return f"{text[: max_length - 3]}..."


def print_download_progress(
    position: int,
    total: int,
    filename: str,
    progress: str,
    speed: float,
) -> None:
    terminal_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    prefix = f"[{position}/{total}] "
    status = f"{progress} at {format_bytes(speed)}/s"
    separator = ": "
    filename_width = terminal_width - len(prefix) - len(separator) - len(status) - 1

    if filename_width >= 12:
        line = f"{prefix}{truncate_text(filename, filename_width)}{separator}{status}"
    else:
        line = f"{prefix}{status}"

    print(f"\r\033[K{line[: terminal_width - 1]}", end="", flush=True)


def download_skip_message(
    filename: str,
    destination: Path,
    key: str | None,
    overwrite: bool,
    existing_filenames: set[str],
    existing_episode_keys: set[str],
) -> str | None:
    if overwrite:
        return None
    if filename in existing_filenames:
        return f"Skipping existing file: {destination}"
    if key and key in existing_episode_keys:
        return f"Skipping existing episode {key}: {filename}"

    return None


def download_file(
    session: requests.Session,
    url: str,
    out_dir: Path,
    overwrite: bool,
    position: int,
    total: int,
    max_retries: int,
    rate_limit_delay: int,
    existing_filenames: set[str],
    existing_episode_keys: set[str],
) -> bool:
    filename = safe_filename(url)
    destination = out_dir / filename
    key = episode_key(filename)

    skip_message = download_skip_message(
        filename,
        destination,
        key,
        overwrite,
        existing_filenames,
        existing_episode_keys,
    )
    if skip_message:
        print(f"[{position}/{total}] {skip_message}")
        return False

    temp_destination = destination.with_suffix(destination.suffix + ".part")

    for attempt in range(1, max_retries + 2):
        resume_from = (
            temp_destination.stat().st_size
            if not overwrite and temp_destination.exists()
            else 0
        )
        headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else None
        if resume_from > 0:
            print(
                f"[{position}/{total}] Resuming {filename} from "
                f"{format_bytes(resume_from)}."
            )
        else:
            print(f"[{position}/{total}] Downloading one file: {url}")

        with session.get(url, headers=headers, stream=True, timeout=60) as response:
            if response.status_code == 429 and attempt <= max_retries:
                wait_seconds = retry_after_seconds(response, rate_limit_delay)
                print(
                    f"[{position}/{total}] Rate limited. Waiting {wait_seconds}s "
                    f"before retry {attempt}/{max_retries}."
                )
                time.sleep(wait_seconds)
                continue

            if response.status_code == 416 and resume_from > 0:
                print(
                    f"[{position}/{total}] Could not resume {filename}; "
                    "restarting from the beginning."
                )
                temp_destination.unlink(missing_ok=True)
                if attempt <= max_retries:
                    continue

            response.raise_for_status()

            can_resume = resume_from > 0 and response.status_code == 206
            if resume_from > 0 and not can_resume:
                print(
                    f"[{position}/{total}] Server did not accept resume for "
                    f"{filename}; restarting from the beginning."
                )
                resume_from = 0

            expected_size = (
                content_range_total(response)
                if can_resume
                else content_length(response)
            )
            downloaded_size = resume_from if can_resume else 0
            transferred_size = 0
            started_at = time.monotonic()
            open_mode = "ab" if can_resume else "wb"
            with temp_destination.open(open_mode) as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)
                        downloaded_size += len(chunk)
                        transferred_size += len(chunk)
                        elapsed = max(time.monotonic() - started_at, 0.001)
                        speed = transferred_size / elapsed
                        if expected_size:
                            percent = downloaded_size / expected_size * 100
                            progress = (
                                f"{percent:5.1f}% "
                                f"({format_bytes(downloaded_size)}/"
                                f"{format_bytes(expected_size)})"
                            )
                        else:
                            progress = format_bytes(downloaded_size)

                        print_download_progress(
                            position,
                            total,
                            filename,
                            progress,
                            speed,
                        )
            print()
        break

    os.replace(temp_destination, destination)
    existing_filenames.add(filename)
    if key:
        existing_episode_keys.add(key)
    print(f"[{position}/{total}] Saved: {destination}")
    return True


def download_all(
    urls: Iterable[str],
    session: requests.Session,
    out_dir: Path,
    overwrite: bool,
    delay: int,
    max_retries: int,
    rate_limit_delay: int,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    queued_urls = list(urls)
    total = len(queued_urls)
    existing_filenames = {path.name for path in out_dir.iterdir() if path.is_file()}
    existing_episode_keys = {
        key
        for filename in existing_filenames
        if not filename.endswith(".part")
        for key in [episode_key(filename)]
        if key
    }

    print(f"Downloading {total} file(s), one at a time.")
    downloaded_count = 0
    for position, url in enumerate(queued_urls, start=1):
        filename = safe_filename(url)
        destination = out_dir / filename
        key = episode_key(filename)
        skip_message = download_skip_message(
            filename,
            destination,
            key,
            overwrite,
            existing_filenames,
            existing_episode_keys,
        )
        if skip_message:
            print(f"[{position}/{total}] {skip_message}")
            continue

        if downloaded_count > 0 and delay > 0:
            print(f"Waiting {delay}s before the next file.")
            time.sleep(delay)

        try:
            downloaded = download_file(
                session,
                url,
                out_dir,
                overwrite,
                position,
                total,
                max_retries,
                rate_limit_delay,
                existing_filenames,
                existing_episode_keys,
            )
            if downloaded:
                downloaded_count += 1
        except requests.HTTPError as error:
            print(f"Failed HTTP download for {url}: {error}", file=sys.stderr)
        except requests.RequestException as error:
            print(f"Failed network download for {url}: {error}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download files from a browser-accessible directory listing into ./out."
    )
    parser.add_argument("url", help="Directory listing URL to download from.")
    parser.add_argument(
        "filename_prefix",
        nargs="?",
        help="Only download files whose filename starts with this prefix.",
    )
    parser.add_argument(
        "--out",
        default="out",
        help="Output directory. Defaults to ./out.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Seconds to wait for the browser listing to become available.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless. Do not use this if you need to complete an interactive challenge.",
    )
    parser.add_argument(
        "--chrome-binary",
        help="Path to Chrome or another Chromium-based browser binary.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite files that already exist in the output directory.",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds to wait between files. Defaults to {DEFAULT_DELAY_SECONDS}.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Retries per file when rate limited. Defaults to {DEFAULT_MAX_RETRIES}.",
    )
    parser.add_argument(
        "--rate-limit-delay",
        type=int,
        default=DEFAULT_RATE_LIMIT_DELAY_SECONDS,
        help=(
            "Seconds to wait after HTTP 429 when Retry-After is missing. "
            f"Defaults to {DEFAULT_RATE_LIMIT_DELAY_SECONDS}."
        ),
    )
    parser.add_argument(
        "--keep-browser-open",
        action="store_true",
        help="Leave Chrome open after collecting links.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        driver = build_driver(headless=args.headless, chrome_binary=args.chrome_binary)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    except SessionNotCreatedException as error:
        print(
            "ChromeDriver could not start a session with this browser version. "
            "Try installing the stable Google Chrome release, or pass a matching "
            "Chromium browser with --chrome-binary.",
            file=sys.stderr,
        )
        print(error, file=sys.stderr)
        return 1

    try:
        driver.get(args.url)
        wait_for_listing(driver, args.timeout)
        links = collect_file_links(driver, args.url)

        if not links:
            print("No file links found.")
            return 1

        print(f"Found {len(links)} file(s).")
        links = filter_links_by_filename_prefix(links, args.filename_prefix)
        if args.filename_prefix:
            print(
                f"{len(links)} file(s) match filename prefix: {args.filename_prefix}"
            )
            if not links:
                return 1

        session = copy_browser_session(driver)
        download_all(
            links,
            session,
            Path(args.out),
            args.overwrite,
            args.delay,
            args.retries,
            args.rate_limit_delay,
        )
    except TimeoutException:
        print("Timed out waiting for the directory listing.", file=sys.stderr)
        return 1
    finally:
        if args.keep_browser_open:
            print("Leaving browser open. Close it when finished.")
        else:
            driver.quit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
