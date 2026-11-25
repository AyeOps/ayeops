set -eu
D=$1
S=$D/.aye-staging
NL='
'
safe() {
  case /$1/ in
    //* | */./* | */../* | *//* | /.aye-*) return 1 ;;
  esac
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
mkdir -p "$D"
rm -rf "$S"
mkdir -p "$S"
tar -xzf - --no-same-owner -C "$S"
cd "$S"
if [ -n "$(find . ! -type f ! -type d)" ]; then
  echo "refused: the archive holds links or special files" >&2
  exit 1
fi
while IFS= read -r f; do
  if ! safe "$f"; then
    echo "refused: unsafe path $f in .aye-files" >&2
    exit 1
  fi
done < .aye-files
rm -f "$D/.aye-env"
while IFS= read -r f; do
  mkdir -p "$D/$(dirname "$f")"
  if [ -f "$D/$f" ] && cmp -s "$f" "$D/$f"; then
    continue
  fi
  mv -f "$f" "$D/$f"
  echo "changed $f"
done < .aye-files
if [ -f "$D/.aye-files" ]; then
  while IFS= read -r f; do
    if safe "$f" && ! grep -qxF -- "$f" .aye-files && [ -f "$D/$f" ]; then
      rm -f "$D/$f"
      echo "removed $f"
    fi
  done < "$D/.aye-files"
fi
mv -f .aye-files "$D/.aye-files"
rm -rf .aye-env
(umask 077 && env_sums "$D/.env" > .aye-env)
mv -f .aye-env "$D/.aye-env"
cd "$D"
rm -rf "$S"
for f in /etc/systemd/system/ayeops-"${D##*/}"-backup-*; do
  if [ -e "$f" ] || [ -L "$f" ]; then
    echo "installed ${f##*/}"
  fi
done
