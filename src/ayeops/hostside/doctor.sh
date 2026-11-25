set -u
D=$1
M=$2
H=$3
B=$4
I=$5
shift 5
echo "user $(id -un 2>/dev/null)"
echo "uname $(uname -m 2>/dev/null)"
echo "clock $(date +%s 2>/dev/null)"
T=
if command -v timeout >/dev/null 2>&1; then
  T="timeout 10"
fi
R=
A=
if command -v docker >/dev/null 2>&1; then
  echo "docker $(docker --version 2>/dev/null)"
  echo "compose $(docker compose version --short 2>/dev/null)"
  R=$($T docker info --format '{{.DockerRootDir}}' 2>/dev/null)
  S=$?
  if [ -n "$R" ]; then
    A=yes
    echo "daemon ok"
  elif [ "$S" -eq 124 ] || [ "$S" -eq 143 ]; then
    echo "daemon timeout"
  else
    echo "daemon fail"
  fi
fi
if [ -z "$R" ]; then
  R=/var/lib/docker
  echo "dockerroot $R assumed"
else
  echo "dockerroot $R"
fi
P=$D
while [ ! -e "$P" ] && [ "$P" != / ] && [ "$P" != . ]; do
  P=$(dirname "$P")
done
if [ -L "$D" ]; then
  echo "dir link"
elif [ -d "$D" ]; then
  echo "dir present"
elif [ -e "$D" ]; then
  echo "dir other"
else
  echo "dir absent $P"
fi
if [ -w "$P" ]; then
  echo "writable yes"
else
  echo "writable no"
fi
O=
if [ "$M" = sudo ]; then
  O=$(sudo -n -l 2>&1 >/dev/null)
fi
if [ "$M" = helper ]; then
  if [ -f "$H" ] && [ -x "$H" ]; then
    echo "helper present"
  else
    echo "helper absent"
  fi
  if O=$(sudo -n -l "$H" "$D" pull 2>&1 >/dev/null); then
    echo "sudo yes"
  else
    echo "sudo no"
  fi
fi
if [ "$M" != plain ]; then
  printf '%s\n' "$(sudo -V 2>/dev/null)" | {
    IFS= read -r V
    echo "sudoversion $V"
  }
  printf '%s\n' "$O" | while IFS= read -r L; do
    case $L in
      /*:[0-9]*:[0-9]*:\ *)
        echo "sudoerror $L"
        break
        ;;
    esac
  done
fi
if [ "$B" = timers ]; then
  if command -v systemctl >/dev/null 2>&1; then
    echo "systemd yes"
  else
    echo "systemd no"
  fi
  if [ "$M" = helper ]; then
    if sudo -n -l "$H" "$D" timers >/dev/null 2>&1; then
      echo "timers yes"
    else
      echo "timers no"
    fi
  fi
fi
echo "dfdir $(df -P -k "$P" 2>/dev/null | tail -n 1)"
if [ -e "$R" ]; then
  echo "dfdocker $(df -P -k "$R" 2>/dev/null | tail -n 1)"
fi
if [ -r /proc/meminfo ]; then
  while read -r k v u; do
    if [ "$k" = MemTotal: ]; then
      echo "memkb $v"
    fi
  done < /proc/meminfo
fi
E=$D/.env
if [ $# -eq 0 ]; then
  echo "env none"
elif [ -d "$D" ] && [ ! -x "$D" ]; then
  echo "env unreadable"
elif [ ! -e "$E" ]; then
  echo "env absent"
elif [ ! -r "$E" ]; then
  echo "env unreadable"
else
  echo "env read"
  for n in "$@"; do
    case $n in
      "" | *[!A-Z0-9_]*) continue ;;
    esac
    K="^[[:space:]]*(export[[:space:]]+)?$n[[:space:]]*="
    if ! grep -Eq "$K" "$E" 2>/dev/null; then
      echo "missing $n"
    elif ! grep -Eq "$K([^[:space:]\"']|[[:space:]]+[^[:space:]#\"']|[[:space:]]*\"[^\"]|[[:space:]]*'[^'])" "$E" 2>/dev/null; then
      echo "empty $n"
    fi
  done
fi
if command -v docker >/dev/null 2>&1; then
  set -f
  for i in $I; do
    case $i in
      *[!A-Za-z0-9._/:@-]*) continue ;;
    esac
    L=
    if [ -n "$A" ]; then
      L=$($T docker image inspect --format '{{.Os}}/{{.Architecture}}' "$i" 2>/dev/null)
    fi
    if [ -n "$L" ]; then
      echo "image $i $L"
      continue
    fi
    O=$($T docker manifest inspect "$i" 2>&1)
    C=$?
    if [ "$C" -eq 0 ]; then
      case $O in
        *'"manifests"'*) ;;
        *)
          O=$($T docker manifest inspect -v "$i" 2>&1)
          C=$?
          ;;
      esac
    fi
    echo "manifest $i $C $(printf '%s' "$O" | tr -d '\r\n')"
  done
fi
echo end
