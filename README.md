# Employee Attendance & Analytics API

FastAPI + MongoDB service for the HROne assignment. Everything lives in `app/main.py`.

## Running it

Needs Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in the environment, or in a local `.env` (real env vars win). Indexes are created on startup, so there are no manual steps. To load the sample data, run `python sample_seed.py`.

## Things worth knowing

- The trend range limit counts days inclusively, so 92 days (e.g. Jul 1 to Sep 30) is allowed and 93 is a 422.
- The 7-day moving average is the mean of the rounded daily rates, the same numbers the response shows.
- A punch-out earlier than the employee's open punch-in is a 422, not a 404.
- MongoDB's `$round` rounds halves to even, so half-up rounding in the pipelines is done with integer arithmetic instead.
- Concurrent corrections to the same record use an optimistic check on the history length; the slower request gets a 409 and can retry.

See `REVIEW.md` for the starter fixes and `DECISIONS.md` for design choices.
