#!/usr/bin/env bash
# Build script for Render — installs Chrome + ChromeDriver + Python deps

set -e

STORAGE_DIR=/opt/render/project/.render

# ── Install Chrome ──
if [[ ! -d $STORAGE_DIR/chrome ]]; then
    echo ">>> Downloading Chrome..."
    mkdir -p $STORAGE_DIR/chrome
    cd $STORAGE_DIR/chrome
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    ar x google-chrome-stable_current_amd64.deb
    tar -xf data.tar.xz -C $STORAGE_DIR/chrome
    rm -f google-chrome-stable_current_amd64.deb data.tar.xz control.tar.xz debian-binary
    echo ">>> Chrome installed."
else
    echo ">>> Using cached Chrome."
fi

CHROME_PATH="$STORAGE_DIR/chrome/opt/google/chrome/google-chrome"
CHROME_VERSION=$($CHROME_PATH --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1)
echo ">>> Chrome version: $CHROME_VERSION"

# ── Install ChromeDriver (matching version) ──
if [[ ! -f $STORAGE_DIR/chromedriver/chromedriver ]]; then
    echo ">>> Downloading ChromeDriver..."
    mkdir -p $STORAGE_DIR/chromedriver

    # Get the matching ChromeDriver URL from the Chrome for Testing API
    MAJOR_VERSION=$(echo $CHROME_VERSION | cut -d. -f1)
    DRIVER_URL=$(wget -qO- "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json" \
        | python3 -c "
import sys, json
data = json.load(sys.stdin)
versions = data['versions']
# Find the latest version matching our major version
best = None
for v in versions:
    if v['version'].startswith('$MAJOR_VERSION.'):
        downloads = v.get('downloads', {}).get('chromedriver', [])
        for d in downloads:
            if d['platform'] == 'linux64':
                best = d['url']
if best:
    print(best)
")

    if [[ -n "$DRIVER_URL" ]]; then
        cd $STORAGE_DIR/chromedriver
        wget -q "$DRIVER_URL" -O chromedriver.zip
        unzip -q chromedriver.zip
        mv chromedriver-linux64/chromedriver .
        chmod +x chromedriver
        rm -rf chromedriver.zip chromedriver-linux64
        echo ">>> ChromeDriver installed."
    else
        echo ">>> WARNING: Could not find matching ChromeDriver."
    fi
else
    echo ">>> Using cached ChromeDriver."
fi

# ── Install Python dependencies ──
cd /opt/render/project/src
pip install -r requirements.txt

echo ">>> Build complete."
