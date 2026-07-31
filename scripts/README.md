# Scripts

Repository automation scripts. These are operator tools run by hand (or from
CI), not part of the `klt` CLI.

## Layout

```
scripts/
  README.md          # this file
  deploy-site.sh     # deploy site/ to Cloudflare Pages (klayout-tools.org)
```

## `deploy-site.sh`

Deploys the static site in [`site/`](../site) to Cloudflare Pages (project
`klayout-tools`, custom domain klayout-tools.org). Run it after changing the
site content. Auth uses a scoped API token rather than wrangler OAuth — source
`~/.cloudflare/rjwalters/pages-rjwalters.env` first; see the script's header
comment for details.

```
source ~/.cloudflare/rjwalters/pages-rjwalters.env
scripts/deploy-site.sh
```
