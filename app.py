from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import uuid
import requests
import hashlib
import glob
import shutil
import threading
import json
import logging
import time
import subprocess

# --- EJS FIX: Initialize Deno + yt-dlp challenge solver ---
def init_yt_dlp_solver():
    try:
        # Check if Deno is available
        deno_check = subprocess.run(["deno", "--version"], capture_output=True, text=True)

        if deno_check.returncode == 0:
            print(f"[INIT] Deno detected: {deno_check.stdout.strip()}")
        else:
            print("[INIT] Deno not found. Attempting to install...")

            # Install Deno using the standard installation script
            try:
                install_cmd = "curl -fsSL https://deno.land/x/install/install.sh | sh"
                subprocess.run(install_cmd, shell=True, check=True)

                # Determine Deno bin path (usually ~/.deno/bin)
                home_dir = os.path.expanduser("~")
                deno_bin_path = os.path.join(home_dir, ".deno", "bin")

                if os.path.exists(deno_bin_path):
                    # Add to PATH environment variable for the current process
                    os.environ["PATH"] += os.pathsep + deno_bin_path
                    print(f"[INIT] Deno installed successfully to {deno_bin_path} and added to PATH.")

                    # Verify installation
                    verify_check = subprocess.run(["deno", "--version"], capture_output=True, text=True)
                    if verify_check.returncode == 0:
                        print(f"[INIT] Verified Deno version: {verify_check.stdout.strip()}")
                    else:
                        print("[INIT ERROR] Deno installed but failed to run.")
                else:
                    print("[INIT ERROR] Deno installation script ran, but binary not found.")
            except Exception as e:
                print(f"[INIT ERROR] Failed to install Deno: {e}")

        # Clear old caches only (NO NIGHTLY UPDATES ANYMORE)
        subprocess.run(["yt-dlp", "--rm-cache-dir"], check=False)

        # Preload EJS challenge solver
        subprocess.run([
            "yt-dlp",
            "--remote-components", "ejs:github",
            "--simulate", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ], check=False)

        print("[INIT] yt-dlp EJS challenge solver initialized successfully.")
    except Exception as e:
        print(f"[INIT ERROR] Failed to initialize yt-dlp EJS solver: {e}")

threading.Thread(target=init_yt_dlp_solver, daemon=True).start()

app = Flask(__name__)

# --- Configuration ---
BASE_TEMP_DIR = "/tmp"
os.makedirs(BASE_TEMP_DIR, exist_ok=True)

TEMP_DOWNLOAD_DIR = os.path.join(BASE_TEMP_DIR, "download")
os.makedirs(TEMP_DOWNLOAD_DIR, exist_ok=True)

CACHE_DIR = os.path.join(BASE_TEMP_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_VIDEO_DIR = os.path.join(BASE_TEMP_DIR, "cache_video")
os.makedirs(CACHE_VIDEO_DIR, exist_ok=True)

MAX_CACHE_SIZE = 500 * 1024 * 1024  # 500MB

# --- Cookie pool: read a folder of .txt cookie files and rotate through them ---
COOKIE_DIR = os.getenv("COOKIE_DIR", "cookies")
if COOKIE_DIR:
    COOKIE_DIR = os.path.abspath(COOKIE_DIR)

_cookie_lock = threading.Lock()
_cookie_index = 0

def _load_cookie_files():
    if not COOKIE_DIR or not os.path.isdir(COOKIE_DIR):
        return []
    return sorted(glob.glob(os.path.join(COOKIE_DIR, "*.txt")))

_cookie_files = _load_cookie_files()
if _cookie_files:
    app.logger.info(f"Loaded {len(_cookie_files)} cookie file(s) from: {COOKIE_DIR}")
else:
    app.logger.warning(f"No cookie .txt files found in: {COOKIE_DIR}. Continuing without cookies.")

def get_cookie_file():
    """Round-robin through all cookie files in COOKIE_DIR so no single file gets hammered."""
    global _cookie_index
    files = _load_cookie_files()
    if not files:
        return None
    with _cookie_lock:
        cookie_file = files[_cookie_index % len(files)]
        _cookie_index += 1
    return cookie_file

SEARCH_API_URL = "https://odd-block-a945.tenopno.workers.dev/search"

# --- Utility functions ---
def get_cache_key(video_url: str) -> str:
    return hashlib.md5(video_url.encode('utf-8')).hexdigest()

def get_directory_size(directory: str) -> int:
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(directory):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total_size += os.path.getsize(fp)
    return total_size

def check_cache_size_and_cleanup():
    total_size = get_directory_size(CACHE_DIR) + get_directory_size(CACHE_VIDEO_DIR)
    if total_size > MAX_CACHE_SIZE:
        app.logger.info(f"Cache size {total_size} exceeds {MAX_CACHE_SIZE}, clearing caches.")
        for cache_dir in [CACHE_DIR, CACHE_VIDEO_DIR]:
            for file in os.listdir(cache_dir):
                try:
                    os.remove(os.path.join(cache_dir, file))
                except Exception:
                    pass

def periodic_cache_cleanup():
    while True:
        check_cache_size_and_cleanup()
        time.sleep(60)

threading.Thread(target=periodic_cache_cleanup, daemon=True).start()

def resolve_spotify_link(url: str) -> str:
    if "spotify.com" in url:
        resp = requests.get(SEARCH_API_URL, params={"title": url}, timeout=15)
        if resp.status_code != 200:
            raise Exception("Failed to fetch search results for Spotify")
        result = resp.json()
        if not result or "link" not in result:
            raise Exception("No YouTube result for Spotify")
        return result["link"]
    return url

def make_ydl_opts_audio(output_template: str):
    opts = {
        'format': '249/worstaudio',   # FIX: Try itag 249, fallback to lowest quality audio
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'socket_timeout': 60,
        'concurrent_fragment_downloads': 4,
        'n_threads': 4,
    }
    cookie_file = get_cookie_file()
    if cookie_file:
        opts['cookiefile'] = cookie_file
    return opts

def make_ydl_opts_video(output_template: str):
    opts = {
        'format': 'best[ext=mp4][vcodec^=avc1][acodec^=mp4a][height<=360]/best[ext=mp4]/best',
        'outtmpl': output_template,
        'noplaylist': True,
        'quiet': True,
        'socket_timeout': 60,
        'concurrent_fragment_downloads': 4,
        'n_threads': 4,
    }
    cookie_file = get_cookie_file()
    if cookie_file:
        opts['cookiefile'] = cookie_file
    return opts



def download_audio(video_url: str) -> str:
    cache_key = get_cache_key(video_url)
    cached_files = glob.glob(os.path.join(CACHE_DIR, f"{cache_key}.webm"))
    if cached_files:
        return cached_files[0]

    unique_id = str(uuid.uuid4())
    output_template = os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.%(ext)s")

    with yt_dlp.YoutubeDL(make_ydl_opts_audio(output_template)) as ydl:
        info = ydl.extract_info(video_url, download=True)
        downloaded_file = ydl.prepare_filename(info)

        # Move to cache with .webm extension
        cached_file_path = os.path.join(CACHE_DIR, f"{cache_key}.webm")
        try:
            shutil.move(downloaded_file, cached_file_path)
        except Exception:
            # If prepare_filename didn't point to final file (edge-cases), fallback to glob
            candidates = glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.*"))
            if not candidates:
                raise Exception("Audio download failed: no file produced")
            downloaded_file = candidates[0]
            shutil.move(downloaded_file, cached_file_path)

        check_cache_size_and_cleanup()
        return cached_file_path

def download_video(video_url: str) -> str:
    """
    Downloads the best video + best audio, merges into mp4 when necessary,
    caches the result as {cache_key}.mp4 and returns the cached file path.
    """
    cache_key = hashlib.md5((video_url + "_video").encode()).hexdigest()
    cached_files = glob.glob(os.path.join(CACHE_VIDEO_DIR, f"{cache_key}.mp4"))
    if cached_files:
        return cached_files[0]

    unique_id = str(uuid.uuid4())
    output_template = os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.%(ext)s")

    opts = make_ydl_opts_video(output_template)
    # Force merge to mp4 if merging is required
    opts['merge_output_format'] = 'mp4'
    # Make sure we are not writing to cache dir directly to avoid partial files there
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=True)

    # After download, find produced file(s)
    candidates = glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.*"))
    if not candidates:
        # As a fallback, attempt to use ydl.prepare_filename(info) if available
        try:
            downloaded_file = ydl.prepare_filename(info)
        except Exception:
            raise Exception("Video download failed: no file produced")
    else:
        # Prefer mp4 final merged file if present
        mp4_candidate = next((c for c in candidates if c.lower().endswith('.mp4')), None)
        downloaded_file = mp4_candidate or candidates[0]

    # Ensure final cache file path ends with .mp4
    cached_file_path = os.path.join(CACHE_VIDEO_DIR, f"{cache_key}.mp4")
    try:
        shutil.move(downloaded_file, cached_file_path)
    except Exception:
        # If moving fails, try copying then removing
        shutil.copy2(downloaded_file, cached_file_path)
        try:
            os.remove(downloaded_file)
        except Exception:
            pass

    # Cleanup any remaining temp candidates for this unique_id
    for c in glob.glob(os.path.join(TEMP_DOWNLOAD_DIR, f"{unique_id}.*")):
        try:
            os.remove(c)
        except Exception:
            pass

    check_cache_size_and_cleanup()
    return cached_file_path

# --- Endpoints ---

@app.route('/search', methods=['GET'])
def search_video():
    try:
        query = request.args.get('title')
        if not query:
            return jsonify({"error": "The 'title' parameter is required"}), 400

        resp = requests.get(SEARCH_API_URL, params={"title": query}, timeout=15)
        if resp.status_code != 200:
            return jsonify({"error": "Search API failure"}), 500

        result = resp.json()
        if not result or "link" not in result:
            return jsonify({"error": "No results"}), 404

        video_url = result["link"]
        threading.Thread(target=download_audio, args=(video_url,), daemon=True).start()
        threading.Thread(target=download_video, args=(video_url,), daemon=True).start()

        return jsonify({
            "title": result.get("title"),
            "url": video_url,
            "duration": result.get("duration"),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/vdown', methods=['GET'])
def download_video_endpoint():
    try:
        video_url = request.args.get('url')
        video_title = request.args.get('title')

        if video_title and not video_url:
            resp = requests.get(SEARCH_API_URL, params={"title": video_title}, timeout=15)
            if resp.status_code != 200:
                return jsonify({"error": "Search API error"}), 500
            video_url = resp.json()["link"]

        if "spotify.com" in video_url:
            video_url = resolve_spotify_link(video_url)

        cached_file_path = download_video(video_url)
        return send_file(cached_file_path, as_attachment=True)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/download', methods=['GET'])
def download_audio_endpoint():
    try:
        video_url = request.args.get('url')
        video_title = request.args.get('title')

        if video_title and not video_url:
            resp = requests.get(SEARCH_API_URL, params={"title": video_title}, timeout=15)
            if resp.status_code != 200:
                return jsonify({"error": "Search API error"}), 500
            video_url = resp.json()["link"]

        if "spotify.com" in video_url:
            video_url = resolve_spotify_link(video_url)

        cached_file_path = download_audio(video_url)
        return send_file(cached_file_path, as_attachment=True)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- CDN ONLY ENDPOINT (LOCKED TO ITAG 249 WEBM) ---
@app.route('/down', methods=['GET'])
def get_cdn_link():
    try:
        video_url = request.args.get('url')
        video_title = request.args.get('title')

        if video_title and not video_url:
            resp = requests.get(SEARCH_API_URL, params={"title": video_title}, timeout=15)
            if resp.status_code != 200:
                return jsonify({"error": "Search API error"}), 500
            video_url = resp.json()["link"]

        if "spotify.com" in video_url:
            video_url = resolve_spotify_link(video_url)

        cache_key = get_cache_key(video_url)
        cached = bool(glob.glob(os.path.join(CACHE_DIR, f"{cache_key}.webm")))

        opts = {
            'format': '249',
            'skip_download': True,
            'quiet': True,
        }
        cookie_file = get_cookie_file()
        if cookie_file:
            opts['cookiefile'] = cookie_file

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            formats = info.get("formats", [])

            fmt_249 = next((f for f in formats if str(f.get('format_id')) == "249"), None)
            if not fmt_249 or "url" not in fmt_249:
                return jsonify({"error": "itag 249 not available"}), 404

            return jsonify({
                "audio": fmt_249["url"],
                "cached": cached,
                "title": info.get("title", "Unknown")
            })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/')
def home():
    return """
    <h1>🎶 YouTube Audio/Video Downloader API</h1>
    <p><strong>Low-bitrate locked API (itag 249 with fallback)</strong></p>
    <ul>
        <li>/search?title=</li>
        <li>/download?url=</li>
        <li>/vdown?url=</li>
        <li>/down?url=</li>
    </ul>
    """


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
