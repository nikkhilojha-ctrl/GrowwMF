#!/bin/sh
# Usage: sh scripts/set-railway-domain.sh mf-faq-production.up.railway.app
set -eu
domain="${1:?pass the Railway domain, for example mf-faq-production.up.railway.app}"
domain="${domain#https://}"
domain="${domain%/}"
sed -i "s#https://[^/\"]*/api/:path\*#https://${domain}/api/:path*#" vercel.json
grep -n "destination" vercel.json
