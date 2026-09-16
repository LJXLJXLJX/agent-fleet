#!/bin/sh
# POSIX sh on purpose: this wrapper is mounted into arbitrary task base images
# (e.g. Alpine) that do not ship bash. Keep it POSIX; there are no pipelines,
# so pipefail has no effect here anyway.
set -eu

# Cache-rewrite adapter for prebuild curl/wget invocations. This is a strict
# whitelist / fail-closed classifier: only invocation shapes listed in
# classify_curl / classify_wget are rewritten to the Artifact Cache Gateway.
# Everything else — unknown options, unknown clusters, unfamiliar shapes —
# runs the real binary with the original argv unchanged. Caching is an
# optimization; correctness never depends on recognizing a command.

runtime=${OPENSANDBOX_DOWNLOAD_RUNTIME_DIR:-/run/opensandbox-download}
command_name=${0##*/}
case "$command_name" in
    curl|wget) ;;
    *)
        echo "[opensandbox download] ERROR event=unsupported-command" >&2
        exit 126
        ;;
esac

# Test and emergency layouts may point the wrapper at an alternate directory
# holding the real binaries; by default the rootfs absolute paths are used.
real=
real_bin_dir=${OPENSANDBOX_DOWNLOAD_REAL_BIN_DIR:-}
if [ -n "$real_bin_dir" ]; then
    candidate="$real_bin_dir/$command_name"
    if [ -x "$candidate" ]; then
        real=$candidate
    fi
else
    for candidate in "/usr/bin/$command_name" "/bin/$command_name" "/usr/local/bin/$command_name"; do
        if [ -x "$candidate" ]; then
            real=$candidate
            break
        fi
    done
fi
if [ -z "$real" ]; then
    echo "[opensandbox download] ERROR event=real-binary-unavailable command=$command_name" >&2
    exit 127
fi

run_original() {
    reason=$1
    shift
    echo "[opensandbox download] event=download-bypass command=$command_name reason=$reason" >&2
    exec "$real" "$@"
}

# Implicit client configuration is invisible to the classifier: curl reads
# $CURL_HOME/.curlrc, $XDG_CONFIG_HOME/curlrc (default ~/.config/curlrc), or
# $HOME/.curlrc, and wget reads $WGETRC, $HOME/.wgetrc, or the global
# /etc/wgetrc unless disabled. Settings that can carry request headers or
# credentials must keep the original command unchanged so they never reach
# the Gateway or shape cache identity. Benign system defaults such as
# "passive_ftp" do not change HTTP request semantics, so the global file only
# bypasses when it sets header/auth directives.
if [ "$command_name" = curl ]; then
    xdg_curlrc=
    if [ -n "${XDG_CONFIG_HOME:-}" ]; then
        xdg_curlrc=$XDG_CONFIG_HOME/curlrc
    elif [ -n "${HOME:-}" ]; then
        xdg_curlrc=$HOME/.config/curlrc
    fi
    if { [ -n "${CURL_HOME:-}" ] && [ -f "$CURL_HOME/.curlrc" ]; } \
        || { [ -n "$xdg_curlrc" ] && [ -f "$xdg_curlrc" ]; } \
        || { [ -n "${HOME:-}" ] && [ -f "$HOME/.curlrc" ]; }; then
        run_original ambient-config "$@"
    fi
else
    system_wgetrc=${OPENSANDBOX_DOWNLOAD_SYSTEM_WGETRC:-/etc/wgetrc}
    if { [ -n "${WGETRC:-}" ] && [ -f "$WGETRC" ]; } \
        || { [ -n "${HOME:-}" ] && [ -f "$HOME/.wgetrc" ]; } \
        || { [ -f "$system_wgetrc" ] \
            && grep -q -i -E '^[[:space:]]*(header|http_user|http_password|user|password|ftp_user|ftp_password)' \
                "$system_wgetrc" 2>/dev/null; }; then
        run_original ambient-config "$@"
    fi
fi

# classify_curl / classify_wget return 0 and set cache_url when the invocation
# is fully understood, otherwise they return 1. In the cacheable grammar the
# download URL is always the final argument.

# Cacheable curl shapes (v1):
#   curl URL
#   curl -[fsSL]... URL              e.g. curl -fsSL URL
#   curl -[fsSL]... -o FILE URL      separate or clustered, e.g. curl -sSLo FILE URL
#   curl -[fsSL]... -oFILE URL       joined value, e.g. curl -sSLoFILE URL
# Exactly one HTTP(S) URL, nothing else.
classify_curl() {
    cache_url=
    seen_output=
    while [ "$#" -gt 0 ]; do
        [ -z "$cache_url" ] || return 1
        argument=$1
        case "$argument" in
            [hH][tT][tT][pP]://*|[hH][tT][tT][pP][sS]://*)
                cache_url=$argument
                ;;
            --output)
                [ -n "$seen_output" ] && return 1
                seen_output=1
                shift
                [ "$#" -gt 0 ] || return 1
                ;;
            --output=*)
                [ -n "$seen_output" ] && return 1
                seen_output=1
                ;;
            -[!fsSLo]*|--*|-)
                return 1
                ;;
            -*)
                cluster=${argument#-}
                before_o=${cluster%%o*}
                case "$before_o" in
                    *[!fsSL]*) return 1 ;;
                esac
                if [ "$before_o" != "$cluster" ]; then
                    # Single 'o' takes the output file: joined rest of the
                    # argument, or the next argument when nothing follows.
                    [ -n "$seen_output" ] && return 1
                    seen_output=1
                    if [ "$(( ${#before_o} + 1 ))" -eq "${#cluster}" ]; then
                        shift
                        [ "$#" -gt 0 ] || return 1
                    fi
                fi
                ;;
            *)
                return 1
                ;;
        esac
        shift
    done
    [ -n "$cache_url" ]
}

# Cacheable wget shapes (v1, deliberately narrower than curl):
#   wget -O FILE URL
#   wget -q... -O FILE URL           e.g. wget -q -O FILE URL
#   wget -q... -OFILE URL            e.g. wget -qO FILE URL / wget -qO- URL
# Exactly one HTTP(S) URL, nothing else. The -O option is mandatory: without
# it wget derives the output name from the URL, which a rewritten route would
# break. Anything with default filename semantics, recursion, resume, headers,
# auth, or TLS options bypasses.
classify_wget() {
    cache_url=
    seen_output=
    while [ "$#" -gt 0 ]; do
        [ -z "$cache_url" ] || return 1
        argument=$1
        case "$argument" in
            [hH][tT][tT][pP]://*|[hH][tT][tT][pP][sS]://*)
                cache_url=$argument
                ;;
            -[!qO]*|--*|-)
                return 1
                ;;
            -*)
                cluster=${argument#-}
                before_o=${cluster%%O*}
                case "$before_o" in
                    *[!q]*) return 1 ;;
                esac
                if [ "$before_o" != "$cluster" ]; then
                    [ -n "$seen_output" ] && return 1
                    seen_output=1
                    if [ "$(( ${#before_o} + 1 ))" -eq "${#cluster}" ]; then
                        shift
                        [ "$#" -gt 0 ] || return 1
                    fi
                fi
                ;;
            *)
                return 1
                ;;
        esac
        shift
    done
    [ -n "$seen_output" ] && [ -n "$cache_url" ]
}

if ! classify_$command_name "$@"; then
    run_original unsupported-invocation "$@"
fi

source_root=
if ! IFS= read -r source_root < "$runtime/source"; then
    echo "[opensandbox download] ERROR event=source-read-failed" >&2
    exit 125
fi
source_root=${source_root%/}
if [ -z "$source_root" ]; then
    echo "[opensandbox download] ERROR event=source-empty" >&2
    exit 125
fi

case "$cache_url" in
    "$source_root"|"$source_root"/*)
        rewritten=$cache_url
        ;;
    *)
        if ! rewritten=$(LC_ALL=C awk \
            -v source="$source_root" -v original="$cache_url" \
            -f "$runtime/url-rewriter.awk"); then
            # Credential-bearing, loopback, glob, query-bearing, or otherwise
            # unusual URLs are valid curl/wget inputs, but not safe cache
            # keys. Preserve the original invocation exactly.
            run_original url-not-cache-eligible "$@"
        fi
        ;;
esac

echo "[opensandbox download] event=download-cache-rewrite command=$command_name" >&2

source_authority=${source_root#*://}
source_authority=${source_authority%%/*}
case "$source_authority" in
    \[*\]*) source_host=${source_authority#\[}; source_host=${source_host%%\]*} ;;
    *) source_host=${source_authority%%:*} ;;
esac
if [ -z "$source_host" ]; then
    echo "[opensandbox download] ERROR event=source-host-empty" >&2
    exit 125
fi
no_proxy="${no_proxy:+$no_proxy,}$source_host"
NO_PROXY="${NO_PROXY:+$NO_PROXY,}$source_host"
export no_proxy NO_PROXY

# cache_url is the final argument; rotate it off and append the rewritten
# route, preserving every earlier argument exactly (POSIX, no arrays).
arg_count=$#
rotated=1
while [ "$rotated" -lt "$arg_count" ]; do
    set -- "$@" "$1"
    shift
    rotated=$((rotated + 1))
done
shift
exec "$real" "$@" "$rewritten"
