from __future__ import annotations

import re

SQL_END = "AYEOPS_SQL_END"
SQL_START = re.compile(r"^(select|with)\b", re.IGNORECASE)
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
SQL_WRITES = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|copy|call|into)\b",
    re.IGNORECASE,
)


def sql_query(query: str) -> str:
    return query.strip().rstrip(";").rstrip()


def sql_query_problem(query: str) -> str | None:
    text = SQL_COMMENT.sub(" ", sql_query(query)).strip()
    if not SQL_START.match(text):
        return "the query must start with SELECT or WITH"
    if ";" in text:
        return "the query must be one statement, without ; inside"
    if "\\" in query:
        return "the query must not hold a backslash, psql would read it as a command"
    if SQL_END in query:
        return f"the query must not hold {SQL_END}"
    write = SQL_WRITES.search(text)
    if write:
        return f"the query must only read, {write.group(1).upper()} is refused"
    return None


def statement_timeout(timeout: int) -> str:
    return f"{max(1, timeout - 1)}s"


def sql_text(query: str, label: str | None, timeout: int) -> str:
    selected = f'"{label}"::text, ' if label else ""
    return (
        "begin transaction read only;\n"
        f"set local statement_timeout = '{statement_timeout(timeout)}';\n"
        f'select {selected}"value"::numeric from (\n'
        f"{sql_query(query)}\n"
        ") as ayeops_check;\n"
        "rollback;\n"
    )


def sql_condition(check: str, op: str, threshold: float, no_data: str, stale: int) -> str:
    selector = f'{{check="{check}"}}'
    missing = 1 if no_data == "alerting" else 0
    breached = f"max(ayeops_check_sql_value{selector} {op} bool {threshold}) or vector({missing})"
    return f"({breached}) + ({sql_failed(check, stale)})"


def sql_failed(check: str, stale: int) -> str:
    selector = f'{{check="{check}"}}'
    return (
        f"1 - max(ayeops_check_sql_ok{selector} * "
        f"(time() - ayeops_check_sql_last_run_timestamp_seconds{selector} < bool {stale}))"
    )
