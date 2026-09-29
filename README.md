# Campus Events + Free Food

Pulls new Instagram posts from ~830 UT Austin student orgs three times a week, uses Claude to turn each post into an event (date, time, place, and whether there's free food), and publishes a website plus subscribable calendars.

- **Setup:** see `SETUP.md`
- **Website:** `docs/index.html` (free food, all events, org directory)
- **Calendars:** `docs/events.ics` and `docs/free-food.ics`
- **Org list you edit:** `data/orgs.csv`
- **Account status (active / quiet / not found):** `data/account_status.csv`

How a run works: `triage.py` picks which accounts to check, Apify pulls their new posts, Claude reads each caption and flyer, and the results go to `data/events.json` and the `docs` folder.
