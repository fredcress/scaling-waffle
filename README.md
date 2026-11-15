# Wedding photo triage

A small web app for the NAS: go through the photos and videos the guests sent, rate each 1–5,
super-star the best, and send the winners to Immich.

- The photo folder is mounted **read-only**. The app only writes its own database and thumbnails.
- Two people rate independently (pick your name on first open, no password). Progress is tracked per person.
- The guest name is the first folder level: `wedding-guests/Aunt Marie/IMG_1234.HEIC` → "Aunt Marie".
  Files directly in the root show up as "Unknown".
- HEIC photos and iPhone videos are converted to previews once, so browsing is fast.
- Hidden automatically: exact duplicates, the video half of Live Photos, RAW files.
- Flagged, never hidden: photos that look blurry, very dark, blown out or empty ("Possibly junk" in Browse,
  with a button to rate the whole pile as 1), and bursts of look-alike shots (shown as a strip while sorting;
  `B` keeps the one on screen and rates the rest 1).

## Install (TrueNAS + Dockge)

1. Every push to `main` runs the `build` workflow, which tests the app and publishes
   `ghcr.io/fredcress/scaling-waffle:latest`.
2. If the repository is private, the image is too: either make the package public
   (GitHub → Packages → package settings) or run `docker login ghcr.io` on the NAS with a token
   that has `read:packages`.
3. Create a dataset/folder for the app data, e.g. `/mnt/tank/apps/wedding-triage`.
4. In Dockge, create a stack from [`compose.yaml`](compose.yaml) and put your own values in its `.env`
   (see [`.env.example`](.env.example)). Without a `.env` the defaults in the compose file apply.
5. Open `http://<nas>:8484`. The first start scans the folder and builds previews in the background;
   photos become available as they are ready.

To update: push to `main`, then "Update" the stack in Dockge.

## Using it

| | |
|---|---|
| **Sort** | One photo at a time. `1`–`5` rates and moves on, `S` super-stars (counts as a 5 if unrated), `←` back, `→` skip, `0` clears. On a phone: tap the buttons, swipe to skip or go back. |
| **Browse** | Grid of everything with both ratings. Filter by guest, top picks, super-stars, disagreements. Click to open and re-rate. |
| **Results** | Progress per person, the export, and library status / rescan. |

While sorting you don't see the other person's rating; Browse and Results show both.

## Export to Immich

Set `IMMICH_URL` and `IMMICH_API_KEY` in the stack's `.env`. In Immich, create the key under
*Account settings → API keys* with these permissions: `asset.upload`, `asset.update`, `album.read`,
`album.create`, `albumAsset.create`.

On the Results tab, choose the threshold and press **Send to Immich**. The originals are uploaded
directly from the NAS into one album (`IMMICH_ALBUM`), with the average score as Immich's star rating,
super-stars as favourites and "Sent by <guest>" as the description. Running it again only uploads what
is new and refreshes the ratings.

Alternative: set `EXPORT_DIR` (and mount it) to get a **Copy to export folder** button instead.

## Settings

| Variable | Default | |
|---|---|---|
| `USERS` | `Husband,Wife` | Names on the "who's sorting?" screen |
| `EXCLUDE` | | Comma-separated folders/globs to skip, e.g. `Uncle Bob/*` |
| `WORKERS` | `2` | Parallel thumbnail jobs |
| `BLUR_BELOW` | `45` | Sharpness score under which a photo is flagged blurry. Use Browse → "Blurriest first" to see where the line should sit |
| `DARK_BELOW`, `BRIGHT_ABOVE`, `FLAT_BELOW` | `28`, `235`, `12` | Brightness / contrast limits for the other flags |
| `BURST_SECONDS`, `BURST_DISTANCE` | `10`, `12` | How close in time and how alike shots must be to form a burst |
| `VIDEO_PREVIEWS` | `1` | Make 720p H.264 copies of videos for playback |
| `IMMICH_URL`, `IMMICH_API_KEY`, `IMMICH_ALBUM` | | Immich export |
| `EXPORT_DIR` | | Folder export |
| `TZ` | `Europe/Paris` | Timezone of the wedding. Photos carry their own local time; videos only store UTC and need this |
| `PHOTOS_PATH`, `DATA_PATH`, `PORT` | | Compose-only: host folders and port |

## Development

```
pip install -r requirements.txt
python -m unittest discover -s tests -v
PHOTOS_DIR=./sample DATA_DIR=./data uvicorn app.main:app --reload
```

The ratings live in `DATA_DIR/triage.sqlite3`; back that file up and nothing else matters.
