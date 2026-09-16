function fail(reason) {
    print "[opensandbox download] ERROR event=invalid-url reason=" reason > "/dev/stderr"
    exit 2
}

# Only public DNS names are cache targets. Everything else keeps its authored
# URL: loopback aliases and IP literals are resolved in the Gateway's own
# network namespace (not the build namespace, where same-RUN servers live),
# single-label names are build-local service names, and internal suffixes are
# not public content. IP literals are bypassed wholesale rather than filtered
# by range: a task that hardcodes an IP is not a cache-reuse shape.
function cache_route_allowed(host, lower) {
    lower = tolower(host)
    if (lower == "localhost" || lower == "0.0.0.0" || \
        lower ~ /^127\./ || lower == "::1")
        return 0
    if (lower ~ /^[0-9.]+$/ && index(lower, ".") != 0)
        return 0
    if (index(lower, ":") != 0)
        return 0
    if (index(lower, ".") == 0)
        return 0
    if (lower ~ /\.(internal|local|corp|intranet|lan)$/)
        return 0
    return 1
}

function encode_component(value,    result, position, character, code) {
    result = ""
    for (position = 1; position <= length(value); position++) {
        character = substr(value, position, 1)
        code = byte_value[character]
        result = result sprintf("%02x", code)
    }
    return result
}

BEGIN {
    for (byte = 1; byte <= 255; byte++)
        byte_value[sprintf("%c", byte)] = byte

    lowered = tolower(original)
    if (substr(lowered, 1, 8) == "https://") {
        scheme = "https"
        remainder = substr(original, 9)
    } else if (substr(lowered, 1, 7) == "http://") {
        scheme = "http"
        remainder = substr(original, 8)
    } else {
        fail("scheme")
    }
    fragment = ""
    marker = index(remainder, "#")
    if (marker != 0) {
        fragment = substr(remainder, marker)
        remainder = substr(remainder, 1, marker - 1)
    }
    query = ""
    marker = index(remainder, "?")
    if (marker != 0) {
        query = substr(remainder, marker)
        remainder = substr(remainder, 1, marker - 1)
    }
    # Signed and tokenized URLs are valid curl/wget inputs but not safe cache
    # keys: the query or fragment would reach the Gateway verbatim, so these
    # URLs stay on the original command exactly like the APT rewriter.
    if (query != "" || fragment != "")
        fail("query-or-fragment")

    slash = index(remainder, "/")
    if (slash == 0) {
        authority = remainder
        path = ""
    } else {
        authority = substr(remainder, 1, slash - 1)
        path = substr(remainder, slash)
    }
    if (authority == "" || authority ~ /@/)
        fail("authority")

    port = ""
    if (substr(authority, 1, 1) == "[") {
        close_bracket = index(authority, "]")
        if (close_bracket == 0)
            fail("ipv6")
        hostname = substr(authority, 2, close_bracket - 2)
        suffix = substr(authority, close_bracket + 1)
        if (suffix != "") {
            if (substr(suffix, 1, 1) != ":")
                fail("port")
            port = substr(suffix, 2)
        }
    } else {
        colon = index(authority, ":")
        if (colon == 0) {
            hostname = authority
        } else {
            hostname = substr(authority, 1, colon - 1)
            port = substr(authority, colon + 1)
            if (index(port, ":") != 0)
                fail("ipv6")
        }
    }
    if (hostname == "")
        fail("hostname")
    if (!cache_route_allowed(hostname))
        fail("not-public-host")
    if (port != "" && (port !~ /^[0-9]+$/ || port + 0 < 1 || port + 0 > 65535))
        fail("port")
    # Normalize to a numeric value so leading zeros match the gateway-side
    # urlsplit().port naming (":007" -> -port-7, ":080" -> default port).
    if (port != "")
        port = port + 0

    canonical_authority = tolower(hostname)
    if (index(canonical_authority, ":") != 0)
        canonical_authority = "[" canonical_authority "]"
    if (port != "" && !((scheme == "https" && port == "443") ||
                         (scheme == "http" && port == "80")))
        canonical_authority = canonical_authority ":" port

    route = source "/download/v1/" scheme "/" encode_component(canonical_authority)
    if (path == "" || path == "/") {
        print route "/root" query fragment
        exit
    }
    if (substr(path, 1, 2) == "//")
        fail("path")
    # curl expands [] and {} URL forms client-side; a rewritten route would
    # collapse that expansion into one literal upstream request.
    if (path ~ /[[{}]/)
        fail("glob")
    print route "/object/" encode_component(path) query fragment
}
