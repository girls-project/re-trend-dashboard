# Community collector

Runs the existing title-only collection pipeline and updates ../data/all_latest.json.
The existing feed is always loaded before merging; malformed/missing data aborts the run.
Author, body, and comment text are not exported. HTTP failures retain prior entries.
Arca uses ordinary HTTP without a challenge solver. Reddit uses its public weekly-top RSS.
Run with Python 3.12+ and requirements.txt; set TZ=Asia/Seoul.
