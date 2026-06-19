#!/usr/bin/env python3
"""
find_domain.py

Generates all 4-letter ".net" domains (XXXX.net) where the 4 letters contain
the substring "pg" or "wg" somewhere in them, then checks which of those
domains are live (resolve + respond to an HTTP request) and takes a screenshot
of the homepage of each live domain.

Usage:
    python3 find_domain.py                 # generate, check, screenshot live ones
    python3 find_domain.py --list-only      # just print the candidate list, don't check liveness
    python3 find_domain.py --workers 50     # tune concurrency
    python3 find_domain.py --timeout 5      # tune per-request timeout (seconds)
    python3 find_domain.py --out results.csv # save full results to CSV
    python3 find_domain.py --no-screenshots  # skip screenshots
    python3 find_domain.py --screenshot-dir shots/  # custom screenshot output directory

Notes:
- "Live" here means: the domain resolves AND responds to an HTTP(S) request
  (any status code counts as "live" -- even a 404 or 403 means a server
  answered). DNS-only resolution without an HTTP response is reported
  separately as "resolves_but_no_http".
- This checks both the bare domain (example.net) and the www subdomain
  (www.example.net), since some sites only answer on one or the other.
- Screenshots are saved as PNG files in the --screenshot-dir directory.
- Requires: pip install requests playwright && python3 -m playwright install chromium
"""

import argparse
import csv
import itertools
import os
import socket
import string
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    print("This script requires the 'requests' library.")
    print("Install it with:  pip install requests")
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
except ImportError:
    print("This script requires the 'playwright' library.")
    print("Install it with:  pip install playwright && python3 -m playwright install chromium")
    sys.exit(1)


LETTERS = string.ascii_lowercase
TARGET_SUBSTRINGS = ["pg", "wg"]


def generate_candidates():
    """
    Generate all unique 4-letter strings that contain "pg" or "wg"
    as a contiguous substring somewhere in the 4 letters.
    """
    candidates = set()

    for target in TARGET_SUBSTRINGS:
        for start_pos in range(3):  # 0, 1, 2
            remaining_positions = [i for i in range(4) if i not in (start_pos, start_pos + 1)]

            for fill in itertools.product(LETTERS, repeat=2):
                chars = [""] * 4
                chars[start_pos] = target[0]
                chars[start_pos + 1] = target[1]
                chars[remaining_positions[0]] = fill[0]
                chars[remaining_positions[1]] = fill[1]
                candidates.add("".join(chars))

    return sorted(candidates)


def resolves(domain):
    """Return True if the domain has a DNS A/AAAA record."""
    try:
        socket.getaddrinfo(domain, None)
        return True
    except socket.gaierror:
        return False


def check_http(domain, timeout):
    """
    Try HTTPS then HTTP, with and without 'www.' prefix.
    Returns a dict describing the result.
    """
    result = {
        "domain": domain,
        "dns_resolves": False,
        "http_status": None,
        "final_url": None,
        "scheme_tried": None,
        "live": False,
        "screenshot_path": None,
    }

    hosts_to_try = [domain, f"www.{domain}"]
    schemes = ["https://", "http://"]

    if resolves(domain) or resolves(f"www.{domain}"):
        result["dns_resolves"] = True

    if not result["dns_resolves"]:
        return result

    for host in hosts_to_try:
        for scheme in schemes:
            url = scheme + host
            try:
                resp = requests.get(
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; domain-checker/1.0)"},
                )
                result["http_status"] = resp.status_code
                result["final_url"] = resp.url
                result["scheme_tried"] = url
                result["live"] = True
                return result
            except requests.RequestException:
                continue

    return result


def take_screenshot(url, output_path, timeout_ms=15000):
    """
    Use Playwright (headless Chromium) to load the given URL and save a
    full-page screenshot to output_path.  Returns True on success.
    """
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.screenshot(path=output_path, full_page=False)
                return True
            except PWTimeoutError:
                print(f"  [screenshot] Timed out loading {url}", file=sys.stderr)
                return False
            except Exception as exc:
                print(f"  [screenshot] Error loading {url}: {exc}", file=sys.stderr)
                return False
            finally:
                browser.close()
    except Exception as exc:
        print(f"  [screenshot] Playwright error for {url}: {exc}", file=sys.stderr)
        return False


def screenshot_live_results(live_results, screenshot_dir, timeout_ms=15000):
    """Take screenshots for all live domains, saving to screenshot_dir."""
    os.makedirs(screenshot_dir, exist_ok=True)
    print(f"\nTaking screenshots of {len(live_results)} live domain(s) -> '{screenshot_dir}/'")

    for res in live_results:
        domain = res["domain"]
        url = res["final_url"] or res["scheme_tried"]
        safe_name = domain.replace(".", "_")
        out_path = os.path.join(screenshot_dir, f"{safe_name}.png")

        print(f"  Screenshotting {url} ...", end=" ", flush=True)
        success = take_screenshot(url, out_path, timeout_ms=timeout_ms)
        if success:
            res["screenshot_path"] = out_path
            print(f"saved -> {out_path}")
        else:
            print("failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-only", action="store_true",
                        help="Only print candidates, skip liveness check")
    parser.add_argument("--workers", type=int, default=30,
                        help="Number of concurrent workers (default: 30)")
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="Per-request timeout in seconds (default: 5)")
    parser.add_argument("--out", type=str, default=None,
                        help="Optional path to save full CSV results")
    parser.add_argument("--no-screenshots", action="store_true",
                        help="Skip taking screenshots of live domains")
    parser.add_argument("--screenshot-dir", type=str, default="screenshots",
                        help="Directory to save screenshots (default: screenshots/)")
    args = parser.parse_args()

    candidates = generate_candidates()
    domains = [f"{c}.net" for c in candidates]

    print(f"Generated {len(domains)} candidate domains (4 letters, containing 'pg' or 'wg').\n")

    if args.list_only:
        for d in domains:
            print(d)
        return

    print(f"Checking liveness with {args.workers} workers, {args.timeout}s timeout each...\n")
    results = []
    live_results = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(check_http, d, args.timeout): d for d in domains}
        completed = 0
        total = len(futures)
        for future in as_completed(futures):
            completed += 1
            res = future.result()
            results.append(res)
            if res["live"]:
                live_results.append(res)
                print(f"[LIVE] {res['scheme_tried']} -> status {res['http_status']} (final: {res['final_url']})")
            if completed % 100 == 0 or completed == total:
                print(f"  ...checked {completed}/{total}", file=sys.stderr)

    print("\n" + "=" * 60)
    print(f"Done. {len(live_results)} live domain(s) found out of {len(domains)} checked.")
    print("=" * 60)
    for res in sorted(live_results, key=lambda r: r["domain"]):
        print(f"  {res['domain']:12s} -> {res['scheme_tried']} (HTTP {res['http_status']})")

    if not args.no_screenshots and live_results:
        screenshot_live_results(live_results, args.screenshot_dir)
    elif not live_results:
        print("\nNo live domains found — nothing to screenshot.")

    if args.out:
        fieldnames = ["domain", "dns_resolves", "http_status", "final_url",
                      "scheme_tried", "live", "screenshot_path"]
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for res in results:
                writer.writerow(res)
        print(f"\nFull results written to {args.out}")


if __name__ == "__main__":
    main()
