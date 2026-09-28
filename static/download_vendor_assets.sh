#!/bin/sh
# One-time setup: downloads Leaflet and Chart.js into static/vendor/ so the
# app no longer depends on unpkg.com / jsdelivr.net at runtime (this is what
# the ZAP VAPT scan flagged as "Sub Resource Integrity Attribute Missing"
# and "Cross-Domain JavaScript Source File Inclusion" - self-hosting removes
# both, and lets the CSP's script-src/style-src drop those two hosts
# entirely). Run this once from a machine with normal internet access; the
# CSP only blocks the *browser* from loading these at runtime, not you
# downloading them here.
#
# Run from anywhere; it writes relative to this script's own location.

set -e
root="$(cd "$(dirname "$0")" && pwd)"
vendor_dir="$root/vendor"
images_dir="$vendor_dir/images"

mkdir -p "$vendor_dir" "$images_dir"

echo "Downloading Leaflet 1.9.4 and Chart.js 4.4.4 into $vendor_dir ..."

curl -sL -o "$vendor_dir/leaflet.css" "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
curl -sL -o "$vendor_dir/leaflet.js" "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
curl -sL -o "$vendor_dir/chart.umd.min.js" "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"

# leaflet.css references these via relative url(images/...) - the zoom
# control and (unused here, but referenced) layers control need them.
for f in layers.png layers-2x.png marker-icon.png marker-icon-2x.png marker-shadow.png; do
    curl -sL -o "$images_dir/$f" "https://unpkg.com/leaflet@1.9.4/dist/images/$f"
done

echo ""
echo "Done. Files written under: $vendor_dir"
echo "  leaflet.css, leaflet.js, chart.umd.min.js"
echo "  images/layers.png (+ -2x), marker-icon.png (+ -2x), marker-shadow.png"
