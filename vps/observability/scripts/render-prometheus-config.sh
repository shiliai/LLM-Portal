#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEMPLATE="$ROOT_DIR/prometheus/prometheus.yml.tmpl"
OUTPUT="$ROOT_DIR/prometheus/prometheus.yml"
ENV_FILE=""
INSTANCE=""
TARGETS=""

usage() {
    echo "usage: $0 [--env-file FILE] [--instance NAME] [--targets SITE=URL[,SITE=URL...]] [--output FILE]" >&2
    exit 2
}

fail() {
    echo "render-prometheus-config: $*" >&2
    exit 1
}

read_env_value() {
    key=$1
    file=$2
    awk -v key="$key" '
        /^[[:space:]]*(#|$)/ { next }
        index($0, key "=") == 1 {
            if (seen++) {
                exit 2
            }
            value = substr($0, length(key) + 2)
            sub(/\r$/, "", value)
            print value
        }
        END {
            if (seen != 1) {
                exit 1
            }
        }
    ' "$file"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --env-file)
            [ "$#" -ge 2 ] || usage
            ENV_FILE=$2
            shift 2
            ;;
        --instance)
            [ "$#" -ge 2 ] || usage
            INSTANCE=$2
            shift 2
            ;;
        --targets)
            [ "$#" -ge 2 ] || usage
            TARGETS=$2
            shift 2
            ;;
        --output)
            [ "$#" -ge 2 ] || usage
            OUTPUT=$2
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

[ -r "$TEMPLATE" ] || fail "template not readable: $TEMPLATE"

if [ -n "$ENV_FILE" ]; then
    [ -r "$ENV_FILE" ] || fail "env file not readable: $ENV_FILE"
    if [ -z "$INSTANCE" ]; then
        INSTANCE=$(read_env_value OBSERVABILITY_INSTANCE "$ENV_FILE") \
            || fail "OBSERVABILITY_INSTANCE must appear exactly once in $ENV_FILE"
    fi
    if [ -z "$TARGETS" ]; then
        TARGETS=$(read_env_value SITE_TARGETS "$ENV_FILE") \
            || fail "SITE_TARGETS must appear exactly once in $ENV_FILE"
    fi
fi

[ -n "$INSTANCE" ] || fail "--instance or OBSERVABILITY_INSTANCE is required"
[ -n "$TARGETS" ] || fail "--targets or SITE_TARGETS is required"
case "$INSTANCE" in
    *[!A-Za-z0-9_.-]* | '') fail "invalid instance label: $INSTANCE" ;;
esac

target_file=$(mktemp)
output_dir=$(dirname -- "$OUTPUT")
mkdir -p "$output_dir"
output_tmp=$(mktemp "$output_dir/.prometheus.yml.XXXXXX")
trap 'rm -f "$target_file" "$output_tmp"' EXIT HUP INT TERM

old_ifs=$IFS
IFS=,
set -- $TARGETS
IFS=$old_ifs
[ "$#" -gt 0 ] || fail "at least one target is required"
for raw_target in "$@"; do
    entry=$(printf '%s' "$raw_target" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    [ -n "$entry" ] || fail "empty target"
    site=${entry%%=*}
    target=${entry#*=}
    [ "$site" != "$entry" ] && [ -n "$target" ] || fail "target must use SITE=URL: $entry"
    case "$site" in
        *[!A-Za-z0-9_.-]* | '') fail "invalid site label: $site" ;;
    esac
    case "$target" in
        http://?* | https://?*) ;;
        *) fail "target must be an http(s) URL: $target" ;;
    esac
    case "$target" in
        *://*@*) fail "target URL must not contain userinfo: $site" ;;
        *[!A-Za-z0-9.:/?\&=_%+~-]*) fail "target contains unsupported characters: $site" ;;
    esac
    printf '%s\t%s\n' "$site" "$target" >> "$target_file"
done
awk -F '\t' 'seen[$1]++ { exit 1 }' "$target_file" || fail "site labels must be unique"
LC_ALL=C sort -u "$target_file" -o "$target_file"

awk -v instance="$INSTANCE" -v target_file="$target_file" '
    {
        line = $0
        gsub("__OBSERVABILITY_INSTANCE__", instance, line)
        if (line ~ /__BLACKBOX_STATIC_CONFIGS__/) {
            while ((getline entry < target_file) > 0) {
                split(entry, fields, "\t")
                printf "      - targets: [\"%s\"]\n", fields[2]
                printf "        labels:\n"
                printf "          site: \"%s\"\n", fields[1]
            }
            close(target_file)
            next
        }
        print line
    }
' "$TEMPLATE" > "$output_tmp"

chmod 0644 "$output_tmp"
mv "$output_tmp" "$OUTPUT"
