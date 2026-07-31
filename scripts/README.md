# Scripts

Repository automation scripts. These are operator tools run by hand (or from
CI), not part of the `klt` CLI.

## Layout

```
scripts/
  README.md          # this file
  deploy-site.sh     # build site/ (Astro) and deploy site/dist/ to Cloudflare Pages
  fetch-pdks.sh      # pinned fetch of lambdapdk open PDK data into pdks/
```

## `deploy-site.sh`

Builds the Astro project in [`site/`](../site) (`npm --prefix site ci && npm
--prefix site run build`, producing `site/dist/`) and deploys `site/dist/` to
Cloudflare Pages (project `klayout-tools`, custom domain klayout-tools.org).
Run it after changing the site content. Auth uses a scoped API token rather
than wrangler OAuth — source `~/.cloudflare/rjwalters/pages-rjwalters.env`
first; see the script's header comment for details.

```
source ~/.cloudflare/rjwalters/pages-rjwalters.env
scripts/deploy-site.sh
```

Pass `--no-deploy` to build only (`site/dist/`) and skip the Cloudflare
deploy — useful for local verification and does not require Cloudflare
credentials:

```
scripts/deploy-site.sh --no-deploy
```

Out of scope: regenerating `blocks/*/output/layout.json` or renders — that's
the content pipeline (#62); `deploy-site.sh` only builds and deploys whatever
is already checked into `blocks/` and `site/`.

## `fetch-pdks.sh`

Downloads a pinned release of [lambdapdk](https://github.com/siliconcompiler/lambdapdk)
(Apache-2.0) into `pdks/` — gitignored except for `pdks/README.md`. Run it to
populate local open PDK data; see [`pdks/README.md`](../pdks/README.md) for
what lands where.

```
scripts/fetch-pdks.sh
```
