# Employee Attendance & Analytics API

FastAPI and MongoDB attendance API. Requires Python 3.11+ and MongoDB 6.0+.

Set `MONGO_URI` and `MONGO_DB`, install dependencies with `pip install -r requirements.txt`, then run:

```bash
uvicorn app.main:app --port 8000
```

The application creates its indexes at startup. `sample_seed.py` can load the supplied shape examples into a development database. Review `REVIEW.md` for starter findings and `DECISIONS.md` for design notes.
