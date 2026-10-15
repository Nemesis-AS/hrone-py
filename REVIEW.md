# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Function: compute_late_minutes | The grace boundary is wrong | Reading the code + problem statement | Instead of checking boundary using minutes, I used seconds to check but still return the minutes |
| 2 | Function: compute_overtime | It does not implement 30 minute grace | Reading the code + problem statement | Added a check to return 0 if overtime is less than 30 minutes |
| 3 | Function: health | It does not check the db | Testing the API | Added a MongoDB ping, raising 503 if the db is unavailable, also added timeout to the connection |
| 4 | Function: list_employees | Incorrect pagination | Testing the API | Normalized page number skipping. First page entries were being skipped |
| 5 | App startup | No indexes are created. The spec says the db only has the default `_id` index and creating the rest at startup is our job | Reading Code & Problem Statement | Added `ensure_indexes`, called from FastAPI's `lifespan` (using `asynccontextmanager`) so indexes are created idempotently at startup |
| 6 | Function: create_employee | Not safe if run concurrently. It checks with `find_one` and then inserts, so two simultaneous requests for the same emp_code can both get 201 | Concurrency check | Added a unique index on emp_code so the db rejects the duplicate, and removed the `find_one` pre-check |
| 7 | Function: create_employee | Exposes raw db errors to client | Pass duplicate client | Added graceful error handling |
| 8 | Function: list_employees | Incorrect totals when department filter is applied | API Testing | Added filter when counting docs |
| 9 | Function: list_employees | Returns raw employee docs | API Testing | Serialized the employees to return correct created_at field as per the spec |
| 10 | Function: list_attendance | Incorrect sorting order | API Testing | Added correct sorting order as per the spec |
| 11 | Functions: list_attendance, list_employees | page and page_size are not validated | API Testing | Added vallidation to page and page_size query params |
| 12 | Function: punch_in | Employee not found is not handled | Punch in with an emp_code that does not exist, returns 500 | Return 404 if the employee does not exist |
| 13 | Functions: punch_in, compute_late_minutes, compute_overtime | Overnight shifts are not handled. Date and shift times are taken from the punch's own calendar day, so a punch after midnight gets the wrong date and late/overtime are calculated against the wrong day | Reading the code + problem statement | Added `attendance_date` to assign a punch before shift_end to the previous day's shift, and `shift_window` to get the real shift start/end. |


Also note anything you looked at and decided was **not** a defect, and why.
