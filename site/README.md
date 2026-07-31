# Site

The klayout-tools.org landing page — a single static HTML file
([`index.html`](index.html)), no build step or framework. It deploys to
Cloudflare Pages (project `klayout-tools`, custom domain klayout-tools.org) via
[`../scripts/deploy-site.sh`](../scripts/deploy-site.sh).

To publish a change, edit `index.html`, then run the deploy script (see
[`scripts/README.md`](../scripts/README.md) for the auth flow):

```
source ~/.cloudflare/rjwalters/pages-rjwalters.env
scripts/deploy-site.sh
```

## Layout

```
site/
  README.md      # this file
  index.html     # the static landing page
```
