set -eu
D=$1
U=/etc/systemd/system
N=${D##*/}
P=ayeops-$N-backup-
S=$D/backups/systemd
NL='
'
refuse() {
  echo "refused: $*" >&2
  exit 1
}
if ! command -v systemctl >/dev/null 2>&1; then
  refuse "systemctl is missing; the backup jobs of this host run as systemd timers"
fi
if [ -L "$S" ]; then
  refuse "$S is a symbolic link"
fi
wanted=
timers=
if [ -d "$S" ]; then
  for f in "$S"/*; do
    if [ ! -e "$f" ] && [ ! -L "$f" ]; then
      continue
    fi
    b=${f##*/}
    case $b in
      *[!A-Za-z0-9._-]*) refuse "$b is not a unit name" ;;
      "$P"*.service) ;;
      "$P"*.timer) timers="$timers $b" ;;
      *) refuse "$b is not a backup unit of $N" ;;
    esac
    if [ -L "$f" ] || [ ! -f "$f" ]; then
      refuse "$b is not a regular file"
    fi
    wanted=$wanted$NL$b
  done
fi
wanted=$wanted$NL
changed=$NL
touched=0
for b in $wanted; do
  if [ -L "$U/$b" ]; then
    refuse "$U/$b is a symbolic link"
  fi
  if [ -f "$U/$b" ] && cmp -s "$S/$b" "$U/$b"; then
    continue
  fi
  cp "$S/$b" "$U/.$b.aye-new"
  chmod 0644 "$U/.$b.aye-new"
  mv -f "$U/.$b.aye-new" "$U/$b"
  changed=$changed$b$NL
  touched=1
  echo "changed $b"
done
for f in "$U/$P"*.service "$U/$P"*.timer; do
  if [ ! -e "$f" ] && [ ! -L "$f" ]; then
    continue
  fi
  b=${f##*/}
  case $wanted in
    *"$NL$b$NL"*) continue ;;
  esac
  case $b in
    *.timer) systemctl --quiet disable --now "$b" ;;
  esac
  rm -f "$f"
  touched=1
  echo "removed $b"
done
if [ "$touched" = 1 ]; then
  systemctl daemon-reload
fi
for b in $timers; do
  systemctl --quiet enable "$b"
  case $changed in
    *"$NL$b$NL"*) systemctl restart "$b" ;;
    *) systemctl start "$b" ;;
  esac
done
