# Friends Leaderboard

A private Anki leaderboard for a small group, backed by a Google Sheet.

- `apps_script/Code.gs` is the whole server. It runs as a Google Apps Script web app attached to the sheet.
- `addon/` is the Anki add-on. It computes each person's stats locally and uploads one small record when there are new reviews.

## Setting up the sheet (once, by the organizer)

1. Create a new Google Sheet, for example "Anki Leaderboard".
2. Open **Extensions → Apps Script**. Delete the starter code and paste in `apps_script/Code.gs`. Save.
3. Pick `setup` in the function dropdown and click **Run**. Approve the permissions prompt. Google warns that the app isn't verified; choose **Advanced → Go to (project name)**. The log shows the **invite code**. Admins can also see it from the add-on later.
4. Click **Deploy → New deployment → Select type: Web app**.
   - Execute as: **Me**
   - Who has access: **Anyone** (not "Anyone with a Google account")
5. Copy the **Web app URL** (it ends in `/exec`).

`Code.gs` is meant to be pasted once and never edited. If it ever does need a change, use **Deploy → Manage deployments → Edit → Version: New version**. That keeps the same URL. Creating a new deployment would change the URL and break everyone's add-on.

## Building the add-on

1. Paste the web app URL into `server_url` in `addon/config.json`.
2. Run `./build.sh`. This creates `dist/friends_leaderboard.ankiaddon`.
3. Send that file and the invite code to your friends.

## Installing (each friend)

1. Download `friends_leaderboard.ankiaddon` from the [latest release](https://github.com/denisfliu/anki-friends-leaderboard/releases/latest).
2. In desktop Anki, open **Tools → Add-ons → Install from file…**, pick that file and restart Anki.
3. Open **Tools → Friends Leaderboard → Join…**, then enter a name and the invite code.

## Your cards are never touched

The add-on only reads from Anki collections. It counts reviews and cards; it never adds, changes or deletes cards, notes, decks or review history.

- All collection access goes through `ReadOnly` in `addon/stats.py`, which allows single `SELECT` queries and refuses everything else.
- `scripts/check_safety.py` scans the code and fails if anything could reach a collection another way, call an Anki method that edits one, or contain write SQL. `release.sh` runs it first, so a release that fails can't be published.
- The only files the add-on writes are its own settings, a saved copy of the leaderboard in `user_files/`, and its own files when it updates itself (only after checking the download is this add-on).

## Updates

The add-on checks this repo's latest GitHub release once a day and installs newer versions by itself; the new version runs after Anki restarts. **Tools → Friends Leaderboard → Check for updates** checks right away.

To publish a version: bump `VERSION` in `addon/__init__.py`, commit, then run `./release.sh "What changed"`.

## Notes

- Stats upload only after a sync, and only if something changed. Joining and **Upload my stats now** also upload. The board downloads only when someone opens the leaderboard window, and the server sends a tiny "unchanged" answer if nothing is new.
- Reviews done on a phone count once they sync and desktop Anki opens.
- New stats need no server change. A new field from the add-on shows up as a new column.
- The first person to join is the admin. **Tools → Friends Leaderboard → Admin** shows the invite code, posts announcements, resets someone's sign-in and removes people. The server also supports changing the invite code, renaming people and promoting other admins; those don't have menu items yet.
- If someone reinstalls and loses their token, an admin resets it (**Admin → Reset someone's sign-in**), and that person joins again with the same name. Their history stays.
- Don't put formulas in the Players, Daily or Posts tabs; the script rewrites those cells. Sorting, moving columns or deleting a tab is fine. Make your own tabs for charts.
- Google Sheets keeps version history (**File → Version history**) if anything ever needs undoing.
