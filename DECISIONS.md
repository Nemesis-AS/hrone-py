# DECISIONS.md

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

   On `employees`: unique `emp_code` (blocks duplicate employees, even concurrent ones), `(department, emp_code)` for the filtered list, and `(department, joined_on)` plus `joined_on` for headcount in the summary and trend. On `attendance_logs`: unique `(emp_code, date)` (one record per day, the punch-in race guard, monthly and `$lookup`), `(date desc, emp_code)` for the list sort and month ranges, `(emp_code, punch_in desc)` for punch-out, and `(status, date desc, emp_code)` for status filters. I rejected a single-field `status` index: five values is too low cardinality to be selective, and it can't serve the sort.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

   Both requests load the employee, truncate the time and compute the same attendance date. Neither one checks for an existing record first. Both call `insert_one`. The unique `(emp_code, date)` index lets exactly one insert succeed. The other gets a `DuplicateKeyError`, which I return as 409. The database is the referee, so no timing gap exists.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

   Ranking uses `$setWindowFields` with `$rank` on total late minutes, so tied employees share a rank and the next rank is skipped (1, 2, 2, 4). I filter `rank <= limit` after ranking. If two people tie at the cutoff, both are returned, even if that gives more than `limit` rows. Ties are ordered by `emp_code`. Cutting at the row position would drop someone with exactly the same score, depending on which row happened to come first.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?

   The summary starts from `employees`, not from the logs. I match everyone in the department who joined on or before the last day of the month, then `$lookup` their logs for that month. Grouping by department uses `headcount: {$sum: 1}`, which counts each employee document whether or not they have logs. An employee without logs just adds 0 to every total. Starting from the logs would make those people invisible.

5. **One thing you would change** if this had to serve 100x the data.

   I'd keep pre-computed daily totals per employee in a separate collection, updated on every punch and correction, or rebuilt nightly with `$merge`. Analytics currently re-scan raw logs on each request. At 10M records the summary and trend would get slow, while a rollup collection stays about 30x smaller and is cheap to query.
