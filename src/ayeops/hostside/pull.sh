set -u
D=$1
S=$D/.aye-staging
F=$S/compose.yaml
mkdir -p "$D" || exit 1
rm -rf "$S"
mkdir -p "$S" || exit 1
cat > "$F" || exit 1
echo staged
pull() {
  docker compose --project-directory "$D" -f "$F" pull --quiet --policy missing --ignore-buildable "$@"
}
code=0
if ! pull; then
  code=1
  if names=$(docker compose --project-directory "$D" -f "$F" config --services 2>/dev/null); then
    set -f
    for s in $names; do
      if ! pull "$s"; then
        echo "failed $s"
      fi
    done
  fi
fi
rm -rf "$S"
exit "$code"
