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


def generate_html_report(live_results, screenshot_dir, report_path):
    """
    Write an HTML gallery page showing each live domain's screenshot,
    domain name, HTTP status, and a link to the live URL.
    """
    import base64
    from datetime import datetime

    def embed_image(path):
        try:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            return f"data:image/png;base64,{data}"
        except Exception:
            return ""

    cards_html = ""
    for res in sorted(live_results, key=lambda r: r["domain"]):
        domain = res["domain"]
        url = res["final_url"] or res["scheme_tried"] or f"http://{domain}"
        status = res["http_status"] or "—"
        shot_path = res.get("screenshot_path")

        if shot_path and os.path.exists(shot_path):
            img_src = embed_image(shot_path)
            img_tag = f'<img src="{img_src}" alt="{domain}" loading="lazy">'
        else:
            img_tag = '<div class="no-shot">No screenshot</div>'

        status_class = "ok" if isinstance(status, int) and status < 400 else "warn"

        cards_html += f"""
        <article class="card">
            <a href="{url}" target="_blank" rel="noopener">{img_tag}</a>
            <div class="card-body">
                <h2><a href="{url}" target="_blank" rel="noopener">{domain}</a></h2>
                <span class="badge {status_class}">HTTP {status}</span>
                <p class="url">{url}</p>
            </div>
        </article>"""

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    count = len(live_results)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Domain Report</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: system-ui, -apple-system, sans-serif;
    background: #0f1117;
    color: #e0e0e0;
    padding: 2rem;
  }}
  header {{
    margin-bottom: 2rem;
    border-bottom: 1px solid #2a2d3a;
    padding-bottom: 1rem;
  }}
  header h1 {{ font-size: 1.6rem; color: #fff; }}
  header p {{ color: #888; margin-top: 0.4rem; font-size: 0.9rem; }}
  .gallery {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 1.5rem;
  }}
  .card {{
    background: #1a1d2e;
    border: 1px solid #2a2d3a;
    border-radius: 10px;
    overflow: hidden;
    transition: transform 0.15s, box-shadow 0.15s;
  }}
  .card:hover {{
    transform: translateY(-3px);
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
  }}
  .card a img {{
    width: 100%;
    height: 200px;
    object-fit: cover;
    object-position: top;
    display: block;
  }}
  .no-shot {{
    width: 100%;
    height: 200px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #252836;
    color: #555;
    font-size: 0.85rem;
  }}
  .card-body {{ padding: 0.9rem 1rem 1rem; }}
  .card-body h2 {{ font-size: 1rem; margin-bottom: 0.4rem; }}
  .card-body h2 a {{ color: #7eb6ff; text-decoration: none; }}
  .card-body h2 a:hover {{ text-decoration: underline; }}
  .badge {{
    display: inline-block;
    padding: 0.2rem 0.55rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
  }}
  .badge.ok {{ background: #1a3a2a; color: #4caf7d; }}
  .badge.warn {{ background: #3a2a1a; color: #f0a050; }}
  .url {{
    font-size: 0.75rem;
    color: #666;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .empty {{
    text-align: center;
    color: #555;
    margin-top: 4rem;
    font-size: 1.1rem;
  }}
</style>
</head>
<body>
<header>
  <h1>Live Domain Report</h1>
  <p>{count} live domain(s) found &mdash; generated {timestamp}</p>
</header>
{"<div class='gallery'>" + cards_html + "</div>" if count else "<p class='empty'>No live domains found.</p>"}
</body>
</html>"""

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML report saved -> {report_path}")


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
    parser.add_argument("--report", type=str, default="report.html",
                        help="Path for the HTML gallery report (default: report.html)")
    parser.add_argument("--no-report", action="store_true",
                        help="Skip generating the HTML report")
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

    if not args.no_report:
        generate_html_report(live_results, args.screenshot_dir, args.report)

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
