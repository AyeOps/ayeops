set -eu
D=$1
NL='
'
safe() {
  case /$1/ in
    //* | */./* | */../* | *//* | /.aye-*) return 1 ;;
  esac
}
secret() {
  case /$1 in
    */.env.example) return 1 ;;
    */.env | */.env.*) return 0 ;;
  esac
  return 1
}
env_sum() {
  if [ -n "$key" ]; then
    sum=$(printf '%s' "$value" | sha256sum)
    echo "$key ${sum%% *}"
  fi
}
env_sums() {
  if [ ! -f "$1" ] || [ -L "$1" ] || [ ! -r "$1" ]; then
    return 0
  fi
  key=
  value=
  while IFS= read -r line || [ -n "$line" ]; do
    rest=${line#"${line%%[![:space:]]*}"}
    case $rest in
      export[[:space:]]*)
        rest=${rest#export}
        rest=${rest#"${rest%%[![:space:]]*}"}
        ;;
    esac
    case $rest in
      "" | "#"*) continue ;;
    esac
    name=
    case $rest in
      *=*)
        name=${rest%%=*}
        name=${name%"${name##*[![:space:]]}"}
        ;;
    esac
    case $name in
      "" | [0-9]* | *[!A-Za-z0-9_]*)
        if [ -n "$key" ]; then
          value=$value$NL$line
        fi
        continue
        ;;
    esac
    env_sum
    key=$name
    value=${rest#*=}
  done < "$1"
  env_sum
}
owned=$NL
if [ -f "$D/.aye-files" ]; then
  while IFS= read -r f; do
    owned=$owned$f$NL
  done < "$D/.aye-files"
fi
wanted=$NL
while IFS= read -r line; do
  sum=${line%% *}
  f=${line#* }
  if ! safe "$f" || secret "$f"; then
    echo "refused: unsafe path $f" >&2
    exit 1
  fi
  wanted=$wanted$f$NL
  if [ -L "$D/$f" ]; then
    echo "link $f"
    continue
  fi
  if [ ! -f "$D/$f" ]; then
    echo "added $f"
    continue
  fi
  have=$(sha256sum < "$D/$f")
  if [ "${have%% *}" = "$sum" ]; then
    echo "same $f"
    continue
  fi
  case $owned in
    *"$NL$f$NL"*)
      echo "old $f"
      base64 < "$D/$f"
      echo .
      ;;
    *) echo "foreign $f" ;;
  esac
done
if [ -f "$D/.aye-files" ]; then
  while IFS= read -r f; do
    case $wanted in
      *"$NL$f$NL"*) continue ;;
    esac
    if safe "$f" && [ -f "$D/$f" ]; then
      echo "removed $f"
    fi
  done < "$D/.aye-files"
fi
keys() {
  printf '%s\n' "$1" | while read -r k _; do
    if [ -n "$k" ]; then
      echo "$k"
    fi
  done
}
env_drift() {
  E=$D/.env
  B=$D/.aye-env
  if [ -L "$E" ]; then
    echo "env-file link"
    return 0
  fi
  if [ -e "$E" ] && { [ ! -f "$E" ] || [ ! -r "$E" ]; }; then
    echo "env-file unreadable"
    return 0
  fi
  if [ -e "$E" ]; then
    echo "env-file present"
  else
    echo "env-file absent"
  fi
  current=$(env_sums "$E")
  baseline=
  based=
  if [ -L "$B" ] || [ ! -f "$B" ] || [ ! -r "$B" ]; then
    echo "env-base absent"
  else
    based=1
    while read -r k h extra; do
      case $k in
        "" | [0-9]* | *[!A-Za-z0-9_]*) continue ;;
      esac
      case $h in
        *[!0-9a-f]*) continue ;;
      esac
      if [ "${#h}" -eq 64 ] && [ -z "$extra" ]; then
        baseline="$baseline$k $h$NL"
      fi
    done < "$B"
  fi
  seen=$NL
  for k in $(keys "$current") $(keys "$baseline"); do
    case $seen in
      *"$NL$k$NL"*) continue ;;
    esac
    seen=$seen$k$NL
    now=$(printf '%s\n' "$current" | grep -e "^$k " || true)
    if [ -n "$now" ]; then
      echo "env-key $k"
    fi
    if [ -n "$based" ]; then
      was=$(printf '%s\n' "$baseline" | grep -e "^$k " || true)
      if [ "$now" != "$was" ]; then
        echo "env-changed $k"
      fi
    fi
  done
}
env_drift
