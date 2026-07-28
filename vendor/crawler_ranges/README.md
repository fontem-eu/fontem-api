# Verified search-crawler IP ranges

Published ranges used by the EU access gate (`GET /geo/eu-gate`) to
exempt search-engine crawlers from geo-filtering — verification is by
source IP against these lists, never by User-Agent (trivially spoofed).

- `googlebot.json` — https://developers.google.com/search/apis/ipranges/googlebot.json
- `bingbot.json`   — https://www.bing.com/toolbox/bingbot.json

Vendored 2026-07-25. Refresh occasionally (ranges change rarely):

    curl -sL https://developers.google.com/search/apis/ipranges/googlebot.json -o googlebot.json
    curl -sL https://www.bing.com/toolbox/bingbot.json -o bingbot.json
