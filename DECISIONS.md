1. **Indexes.** I use a unique index on employee code and a unique compound index on attendance employee/date; these enforce the natural keys and make duplicate writes safe. Department/code and department/join-date indexes support employee lists, headcount and trend lookups. Attendance indexes cover employee/date/status filters, date/code list ordering, and month/date plus late-minute scans. I considered a separate status-first index and left it out because the main queries constrain date or employee first.

2. **Punch-in race.** Both requests may read the employee and build the same attendance date. The unique employee/date index allows one insert to win. MongoDB rejects the other with duplicate-key error, which the handler turns into 409. The winner returns 201.

3. **Ties.** I calculate competition rank from total late minutes. If rank `limit` is tied, all tied employees are returned; employee code makes their order stable.

4. **Headcount.** The department summary starts from employees and looks up logs. Preserving empty lookup results keeps employees with no logs in the group; join date filters out future hires.

5. **100x data.** I would replace offset pagination with a cursor, review explain plans against production-like distributions, and consider materialized daily aggregates for frequently requested analytics.
