function escaped(text) {
  gsub(/\\/, "\\\\", text)
  gsub(/"/, "\\\"", text)
  return text
}
BEGIN { labels = "check=\"" check "\",service=\"" service "\"" }
code == 0 && $NF ~ /^-?[0-9]+(\.[0-9]+)?$/ {
  series = labels (NF > 1 ? ",label=\"" escaped($1) "\"" : "")
  if (!(series in seen)) {
    seen[series] = 1
    print "ayeops_check_sql_value{" series "} " $NF
  }
}
END {
  print "ayeops_check_sql_ok{" labels "} " (code == 0 ? 1 : 0)
  printf "ayeops_check_sql_duration_seconds{%s} %.3f\n", labels, end - begin
  print "ayeops_check_sql_last_run_timestamp_seconds{" labels "} " now
}
