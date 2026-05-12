# 111477downloader

designed for batch downloading tv show episodes from 111477. 
uses selenium with undetected-chromedriver to load the site, scrapes page for download links then fetches files with python requests, all to bypass the bot protection on the site.


additionally there exists ./transcode.sh which is:
- a script to transcode from whatever source resolution to tv-optimized ffmpeg 720p settings
- strictly written for macos hardware acceleration
- staggers each instance of a batch in order to take advantage of the fact that only the encode step is hardware accelerated, so each instance will be in a different stage. eg: a batch of 3 will have instance 1 % 0% completion, instance 2 @ 33% and instance 3 % 66%. This takes advantage of the CPU & GPU simultaneously and in my experience has found a 86.7% improvement in performance using this technique.

also avoids both duplicate downloads and transcodes based off of standard SxxExx (eg S01E02) filename formatting

## Usage

```sh
./run.sh "https://example.com/files/" [filename prefix filter] [options]
```

The script creates a `.venv`, installs dependencies, and runs the downloader. Files are saved to `out/`.

**Examples:**

```sh
# Download everything
./run.sh "https://example.com/files/"

# Filter by filename prefix
./run.sh "https://a.111477.xyz/tvs/Breaking%20Bad/Season%201/" "Breaking"

# Specify a Chrome binary manually
./run.sh "https://a.111477.xyz/tvs/Breaking%20Bad/Season%201/" "Breaking" --chrome-binary "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

## Scripts

| Script | What it does |
|---|---|
| `run.sh` | Entry point — sets up venv and runs `download.py` |
| `download.py` | Crawls an open directory and downloads files |
| `transcode.sh` | Batch transcodes `out/` → `out-transcoded/` using ffmpeg (max 3 parallel jobs) |

## Requirements

- Python 3
- Chrome / Chromium / Brave / Edge (for Selenium)
- `ffmpeg` (for `transcode.sh` only)
