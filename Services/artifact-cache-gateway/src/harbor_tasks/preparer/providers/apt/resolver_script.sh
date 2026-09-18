
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources
: > /etc/apt/sources.list
if [ "$APT_DISTRO" = ubuntu ]; then
  components='main restricted universe multiverse'
  if [ "$APT_SOURCE_MODE" = domestic ]; then
    # Minimal distribution images may not contain ca-certificates yet.  APT's
    # signed InRelease/Release verification remains mandatory on this HTTP hop.
    root='http://mirrors.tuna.tsinghua.edu.cn/ubuntu'
    printf 'deb %s %s %s\n' "$root" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb %s %s-updates %s\n' "$root" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb %s %s-security %s\n' "$root" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb %s %s-backports %s\n' "$root" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
  else
    printf 'deb %s/ubuntu-upstream %s %s\n' "$APT_GATEWAY" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb %s/ubuntu-upstream %s-updates %s\n' "$APT_GATEWAY" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb %s/ubuntu-security-upstream %s-security %s\n' "$APT_GATEWAY" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
  fi
else
  if [ "$APT_CODENAME" = bookworm ]; then
    components='main contrib non-free non-free-firmware'
  else
    components='main contrib non-free'
  fi
  if [ "$APT_SOURCE_MODE" = domestic ]; then
    printf 'deb http://mirrors.tuna.tsinghua.edu.cn/debian %s %s\n' "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb http://mirrors.tuna.tsinghua.edu.cn/debian %s-updates %s\n' "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb http://mirrors.tuna.tsinghua.edu.cn/debian-security %s-security %s\n' "$APT_CODENAME" "$components" >> /etc/apt/sources.list
  else
    printf 'deb %s/debian-upstream %s %s\n' "$APT_GATEWAY" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb %s/debian-upstream %s-updates %s\n' "$APT_GATEWAY" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
    printf 'deb %s/debian-security-upstream %s-security %s\n' "$APT_GATEWAY" "$APT_CODENAME" "$components" >> /etc/apt/sources.list
  fi
fi
apt-get \
  -o Acquire::Retries=1 \
  -o Acquire::http::Proxy=false \
  -o Acquire::https::Proxy=false \
  update >&2
find /var/lib/apt/lists -type f -name '*_Packages*' -print -quit | grep -q .
# apt-cache accepts multiple package names. One process avoids hundreds of
# subprocess startups in large datasets.
packages="$(tr '\n' ' ' < /probe/packages.txt)"
: > /tmp/artifact-cache-resolved-packages
{ apt-cache show --no-all-versions $packages 2>/dev/null || true; } | awk \
  -v distro="$APT_DISTRO" \
  -v mode="$APT_SOURCE_MODE" \
  -v gateway="$APT_GATEWAY" '
  function clear_record() {
    package=""; version=""; filename=""; sha256=""; size=""
  }
  function emit_record(    site) {
    if (package == "" || filename == "" || seen[package]++) return
    if (distro == "ubuntu") {
      site = mode == "domestic" \
        ? "http://mirrors.tuna.tsinghua.edu.cn/ubuntu" \
        : gateway "/ubuntu-upstream"
    } else if (filename ~ /^pool\/updates\//) {
      site = mode == "domestic" \
        ? "http://mirrors.tuna.tsinghua.edu.cn/debian-security" \
        : gateway "/debian-security-upstream"
    } else {
      site = mode == "domestic" \
        ? "http://mirrors.tuna.tsinghua.edu.cn/debian" \
        : gateway "/debian-upstream"
    }
    printf "ACG\t%s\tresolved\t%s\t%s\t%s\t%s\t%s\n", \
      package, version, filename, sha256, size, site
    print package >> "/tmp/artifact-cache-resolved-packages"
  }
  /^Package: / { emit_record(); clear_record(); package=$2; next }
  /^Version: / { version=$2; next }
  /^Filename: / { filename=$2; next }
  /^SHA256: / { sha256=$2; next }
  /^Size: / { size=$2; next }
  /^$/ { emit_record(); clear_record(); next }
  END { emit_record() }
'
# Resolve virtual package names through APT's declared Reverse Provides.  This
# loop is intentionally limited to provider lookup; the bulk of the inventory
# remains handled by the single apt-cache invocation above.
while IFS= read -r requested; do
  [ -n "$requested" ] || continue
  grep -F -x -q "$requested" /tmp/artifact-cache-resolved-packages && continue
  provider="$(apt-cache showpkg "$requested" 2>/dev/null | awk '
    /^Reverse Provides:[[:space:]]*$/ { providers=1; next }
    providers && NF { print $1; exit }
  ')"
  [ -n "$provider" ] || continue
  record="$(apt-cache show --no-all-versions "$provider" 2>/dev/null || true)"
  [ -n "$record" ] || continue
  version="$(printf '%s\n' "$record" | awk -F ': ' '/^Version:/ {print $2; exit}')"
  filename="$(printf '%s\n' "$record" | awk -F ': ' '/^Filename:/ {print $2; exit}')"
  sha256="$(printf '%s\n' "$record" | awk -F ': ' '/^SHA256:/ {print $2; exit}')"
  size="$(printf '%s\n' "$record" | awk -F ': ' '/^Size:/ {print $2; exit}')"
  [ -n "$filename" ] || continue
  if [ "$APT_DISTRO" = ubuntu ]; then
    if [ "$APT_SOURCE_MODE" = domestic ]; then
      site='http://mirrors.tuna.tsinghua.edu.cn/ubuntu'
    else
      site="${APT_GATEWAY%/}/ubuntu-upstream"
    fi
  elif printf '%s' "$filename" | grep -q '^pool/updates/'; then
    if [ "$APT_SOURCE_MODE" = domestic ]; then
      site='http://mirrors.tuna.tsinghua.edu.cn/debian-security'
    else
      site="${APT_GATEWAY%/}/debian-security-upstream"
    fi
  elif [ "$APT_SOURCE_MODE" = domestic ]; then
    site='http://mirrors.tuna.tsinghua.edu.cn/debian'
  else
    site="${APT_GATEWAY%/}/debian-upstream"
  fi
  printf 'ACG\t%s\tresolved\t%s\t%s\t%s\t%s\t%s\n' \
    "$requested" "$version" "$filename" "$sha256" "$size" "$site"
done < /probe/packages.txt
