# Browser Media Downloader

A Python script that saves accessible photos and videos from an open Instagram or Pinterest tab. It scrolls through the page, saves the media it finds, and skips filenames already in the output folder. Use it for media you own or have permission to download, and follow the site's rules.

## Prerequisites

| Feature | Requirements |
| --- | --- |
| All modes | Python 3.13 recommended, plus access to the relevant site in your browser |
| Instagram | macOS and Apple Command Line Tools for the built-in Apple Vision photo filter. Safari is the default. Chrome is also supported. |
| Pinterest | Google Chrome 144 or later with remote debugging support, plus the Python Playwright package |
| HLS video streams | FFmpeg on your PATH. Direct video downloads do not require it. |

## Installation

In Terminal, clone the repository:

```bash
git clone https://github.com/mahidhar347/browser-media-downloader.git
cd browser-media-downloader
```

If you prefer to download without Git, choose **Code → Download ZIP** on GitHub, unzip it, and open the extracted folder in Terminal. Once you're in the project folder, run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python file-f7w.py --help
```

Playwright is used in Chrome mode. Safari mode uses the copy of Safari already installed on your Mac. For Instagram, install Apple Command Line Tools if `xcode-select -p` reports that they're missing:

```bash
xcode-select --install
```

For HLS videos on macOS, if you use Homebrew:

```bash
brew install ffmpeg
ffmpeg -version
```

## Instagram in Safari

To open this section in Safari and see the steps in Terminal, run:

```bash
python file-f7w.py --setup safari
```

1. Choose **Safari → Settings → Advanced** and enable **Show features for web developers**.
2. Go to **Safari → Settings → Developer → Automation** and enable **Allow JavaScript from Apple Events**. If macOS asks whether Terminal may control Safari when you run the script, review the prompt and allow it if you intend to use Safari mode.
3. Open a profile, post, or reel you may download and copy the exact HTTPS URL from Safari. For a profile, leave its posts grid open. Keep the browser tab open while the script runs.
4. Replace the example URL below with the one you copied.

```bash
python file-f7w.py "https://www.instagram.com/YOUR_PROFILE/" "./instagram-images" --browser safari
```

For profiles, the script opens a temporary tab to visit individual posts and check carousel slides. Apple Vision filters Instagram **photos** to those where it estimates one person. The detector can make mistakes and does not identify who the person is. Instagram videos skip that filter.

## Pinterest in Chrome

On a Mac, this command opens Chrome directly to its remote debugging page. It also prints the address and instructions in Terminal.

```bash
python file-f7w.py --setup chrome
```

1. Open the Pinterest page you may download in Chrome. Keep just one tab with that exact URL open.
2. In a separate tab, open `chrome://inspect/#remote-debugging` and enable remote debugging. Review Chrome's connection prompt when the script starts. This script uses the local browser connection.
3. Copy the Pinterest page's exact HTTPS URL. Replace the example below with your URL.

```bash
python file-f7w.py "https://www.pinterest.com/YOUR_BOARD/" "./pinterest-images" --browser chrome
```

The Pinterest mode saves eligible photos and videos without the Apple Vision filter. Disable remote debugging when you're done if you only enabled it for this run. For Instagram in Chrome, follow the same Chrome connection setup and pass the Instagram URL with `--browser chrome`. The Instagram photo filter still requires macOS and Apple Command Line Tools.

## Limits and restarting

To save up to **25 new files**:

```bash
python file-f7w.py "https://www.instagram.com/YOUR_PROFILE/" "./instagram-images" 25
```

Photos and videos both count toward the limit. Omit the number to scan until the feed stops loading. Use `--idle-seconds 90` if a feed needs longer to load.

Press **Control+C** to stop. To continue, run the same command with the same URL and output folder.

- **Instagram profiles** save progress in `.instagram-progress.sqlite3` inside the output folder. The script saves discovered post links before scrolling and resumes with the in-progress post, followed by queued posts. If the interruption happened inside a carousel, it reopens that post and advances past slides whose files are already saved and have a valid media header. Leave the profile tab open at the same scroll position to continue discovering links from there. After a browser reload it can rediscover links from the top while skipping completed posts.
- **Pinterest and individual Instagram posts** scan again from the top and skip saved filenames.

A scan can miss media the browser fails to load. Different filenames can still represent the same photo.

### Check an Instagram profile again

Use `--recheck` to revisit known posts and scan the profile links from the top. The script keeps existing files with recognized media headers and tries to fetch missing files when the browser exposes them.

```bash
python file-f7w.py "https://www.instagram.com/YOUR_PROFILE/" "./instagram-images" --browser safari --recheck --idle-seconds 120
```

Use `--recheck` once to start the audit. If interrupted, repeat the same command without `--recheck` to continue the audit checkpoint. The photo filter still applies, and posts that fail stay pending.

### Run in the background on macOS

Keep the matching profile tab open and signed in, and grant any Safari automation prompts on a foreground run first. Then, from the project folder:

```bash
nohup /usr/bin/caffeinate -i .venv/bin/python -u file-f7w.py \
  "https://www.instagram.com/YOUR_PROFILE/" "./instagram-images" --browser safari \
  --idle-seconds 120 >> download.log 2>&1 &
```

Run `tail -f download.log` to watch progress. Safari can stay behind other apps. If Instagram stalls, the script can reload the open profile up to twice per run and retry a stalled post once. It retains the saved queue and files. The browser and your Mac still need to be running and signed in. If the browser closes, the Mac restarts, or Instagram asks you to sign in again, reopen the profile and repeat the command with the same output folder.

If you omit the output folder, the script uses `instagram-images` or `pinterest-images` under your current Terminal directory. In a new Terminal window, go back to the project folder and run `source .venv/bin/activate` before using the examples.

## Contributing

Use the pencil icon on a file on GitHub to propose a small edit, or fork the project and open a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md) for the exact steps and local checks.

### Contributors

[View contributors on GitHub](https://github.com/mahidhar347/browser-media-downloader/graphs/contributors). GitHub generates this list from eligible commits merged into the default branch.
