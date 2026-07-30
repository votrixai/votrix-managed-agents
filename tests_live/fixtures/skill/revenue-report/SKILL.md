---
name: revenue-report
description: Produce the standard Votrix revenue report from a revenue CSV. Use this whenever asked for a revenue report or for the total revenue in a CSV file.
---

# Revenue report

Read the CSV. Add up every value in the `revenue` column.

Write **exactly one line**, and nothing else:

```
VOTRIX-REVENUE-REPORT total=<sum> rows=<number of data rows>
```

`rows` counts data rows only, not the header. Do not add a heading, a blank
line, an explanation, or a trailing newline of prose. The line above is the
whole file.
