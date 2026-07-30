# Building KLayout from source on macOS

This guide walks through building KLayout from source on macOS (Apple
Silicon), producing a native `klayout.app` with Ruby and Python bindings.
Every step below was executed verbatim on the configuration listed; where
a step has a gotcha, it's called out inline.

klayout-tools normally uses the prebuilt `klayout` Python module
(`pip install klayout`), but a source build is what you need when the
prebuilt binaries lag a release, when you need bindings against a
specific Homebrew Python, or when you want to patch the engine itself.

## Tested configuration

| Component        | Version used                        |
| ---------------- | ----------------------------------- |
| macOS            | 26.6 "Tahoe" (Apple M3 Ultra)       |
| Xcode CLT clang  | Apple clang 21.0.0                  |
| Homebrew         | 6.0.13                              |
| Qt               | `qt` 6.11.1 (Homebrew)              |
| Python           | `python@3.13` 3.13.14 (Homebrew)    |
| Ruby             | `ruby@3.4` 3.4.10 (Homebrew)        |
| libgit2          | 1.9.6 (Homebrew)                    |
| KLayout          | v0.30.10 (tag)                      |

Build time: **4 m 40 s** wall-clock with `--jobs=24` on the M3 Ultra
(28 cores). Expect ~15–30 min on a 8–10-core machine. The build tree is
~800 MB; the deployed app bundle is ~150 MB.

KLayout's own macOS build documentation lives in
[`macbuild/ReadMe.md`](https://github.com/KLayout/klayout/blob/master/macbuild/ReadMe.md)
in the source tree. It officially supports macOS Tahoe (26.x), Sequoia
(15.x), and Sonoma (14.x). KLayout ≥ 0.29 requires Qt 6 ≥ 6.7.0.

## 1. Prerequisites

You need the Xcode Command Line Tools and [Homebrew](https://brew.sh):

```bash
xcode-select --install   # no-op if already installed
xcode-select -p          # should print a developer dir
```

Install the build dependencies:

```bash
brew install qt@6 python@3.13 ruby@3.4 libgit2
```

Notes:

- `qt@6` currently resolves to the `qt` formula (Qt 6.x). KLayout's build
  script finds it under the Homebrew prefix automatically.
- Python 3.13 is the newest Homebrew Python the build script's `-p HB313`
  flag targets. A newer `python3` on your PATH (e.g. 3.14) is fine — the
  flag pins which interpreter gets *embedded*, not which runs the script.
- Ruby: you can skip `ruby@3.4` and use the macOS-bundled Ruby 2.6 with
  `-r sys` instead; we build against Homebrew Ruby 3.4 (`-r HB34`).
- `libgit2` enables KLayout's Git-based package manager (Salt). Omit it
  and pass `-g/--nolibgit2` if you don't want that.

## 2. Get the source

Clone the release tag (a shallow clone is fine and much faster):

```bash
git clone --branch v0.30.10 --depth 1 https://github.com/KLayout/klayout.git
cd klayout
```

## 3. Build

KLayout on macOS is built with `build4mac.py` (at the repo root,
implemented in `macbuild/`), not with a bare `qmake`/`cmake` invocation.
It selects the Qt/Ruby/Python combination via flags:

```bash
./build4mac.py -q Qt6Brew -r HB34 -p HB313 -m '--jobs=24'
```

- `-q Qt6Brew` — Qt 6 from Homebrew
- `-r HB34` — Ruby 3.4 from Homebrew (`-r sys` for macOS-bundled Ruby)
- `-p HB313` — Python 3.13 from Homebrew
- `-m '--jobs=N'` — passed to `make`; set N near your core count
  (the default is only `--jobs=4`)

Run `./build4mac.py '-?'` to see all options — and note the quotes: in
zsh a bare `-?` is a glob pattern.

The script generates a build directory, an install directory, and a log
next to the checkout root, named after the flavor and macOS version:

```
qt6Brew.build.macos-Tahoe-release-Rhb34Phb313/       # object files, ~800 MB
qt6Brew.bin.macos-Tahoe-release-Rhb34Phb313/         # installed artefacts
qt6Brew.build.macos-Tahoe-release-Rhb34Phb313.log    # full make log
```

Success looks like this at the end of the console output:

```
Build successfully done.
Artefacts were installed to .../qt6Brew.bin.macos-Tahoe-release-Rhb34Phb313
```

## 4. Deploy the app bundle

A successful compile does not yet give you a usable `klayout.app` — the
binaries still reference build-tree paths. Rerun the *same* command with
a deploy flag added. **The flag you want for a locally-used build is
uppercase `-Y`, not lowercase `-y`:**

```bash
./build4mac.py -q Qt6Brew -r HB34 -p HB313 -Y
```

- `-Y` (uppercase) — lightweight ("LW-") deploy for using KLayout on the
  machine that built it. The app links against your Homebrew
  Qt/Python/Ruby rather than embedding them, so it breaks if you later
  uninstall those packages.
- `-y` (lowercase) — full deploy that *embeds* Qt (and optionally
  Python) into the bundle for redistribution. It only supports specific
  module combinations: OS-bundled Ruby+Python ("ST-") or Homebrew
  Python 3.11 ("HW-"). With our Homebrew Ruby 3.4 + Python 3.13 combo it
  aborts partway through with:

  ```
  Exception: ! unsupported PackagePrefix EX-
  ```

  (The usage text makes the two flags sound interchangeable; they are
  not — the semantics live in `build4mac.py`'s `Get_Build_Options`.)

The deployed, ad-hoc-signed bundle lands in:

```
LW-qt6Brew.pkg.macos-Tahoe-release-Rhb34Phb313/klayout.app
```

Move or symlink it into `/Applications` if you want it available like a
normal app. For a redistributable `.dmg`, use one of the supported `-y`
combos and then `macbuild/makeDMG4mac.py -p <pkg-dir> -m`.

## 5. Verify

Version check and headless (batch-mode) Python:

```bash
APP=LW-qt6Brew.pkg.macos-Tahoe-release-Rhb34Phb313/klayout.app
K="$APP/Contents/MacOS/klayout"

"$K" -v
# KLayout 0.30.10

cat > /tmp/smoke.py <<'EOF'
import pya
ly = pya.Layout()
top = ly.create_cell("TOP")
l1 = ly.layer(1, 0)
top.shapes(l1).insert(pya.Box(0, 0, 1000, 2000))
ly.write("smoke.gds")
ly2 = pya.Layout()
ly2.read("smoke.gds")
print("cells:", ly2.top_cell().name, "bbox:", ly2.top_cell().bbox())
EOF
"$K" -b -r /tmp/smoke.py
# cells: TOP bbox: (0,0;1000,2000)
```

And Ruby:

```bash
cat > /tmp/smoke.rb <<'EOF'
ly = RBA::Layout.new
puts "ruby binding ok, dbu=#{ly.dbu}"
EOF
"$K" -b -r /tmp/smoke.rb
# ruby binding ok, dbu=0.001
```

Note: `-r` scripts must be real files — KLayout cannot read
`/dev/stdin`-style paths.

### Using the bundled Python module from your own interpreter

The app ships a `klayout` Python package at `Contents/MacOS/pymod`, but
its extension modules reference the app's dylibs via
`@executable_path`-relative paths, so a plain
`PYTHONPATH=$APP/Contents/MacOS/pymod python3.13` import fails with
`Library not loaded: @executable_path/../Frameworks/libklayout_tl.0.dylib`.
Point the dynamic loader at the app's Frameworks directory and it works
(interpreter must match the build flag — Homebrew Python 3.13 here):

```bash
PYTHONPATH="$APP/Contents/MacOS/pymod" \
DYLD_FALLBACK_LIBRARY_PATH="$APP/Contents/Frameworks" \
/opt/homebrew/opt/python@3.13/bin/python3.13 \
  -c "import klayout.db as db; print(db.__version__)"
# 0.30.10
```

For regular standalone-Python use, prefer the PyPI wheels
(`pip install klayout`) or build wheels from this source tree with the
`-P/--buildPymod` option.

## Gotchas recap

1. **`-y` vs `-Y` are not interchangeable.** Local use → `-Y`
   (lightweight). Lowercase `-y` embeds Qt and only supports
   system-Ruby/Python or Homebrew-Python-3.11 combos; anything else dies
   with `unsupported PackagePrefix EX-` (see §4).
2. **Deploy is a separate second invocation** of `build4mac.py` with the
   same flags plus `-Y`; the compile step alone leaves an app wired to
   build-tree paths.
3. **Default parallelism is `--jobs=4`.** Forgetting `-m '--jobs=N'`
   makes the build several times slower on a big machine.
4. **Quote `'-?'` in zsh** when asking for usage.
5. **Batch scripts must be real files**; process-substitution paths fail.
6. **The bundled pymod needs `DYLD_FALLBACK_LIBRARY_PATH`** when
   imported from an external interpreter (see §5).
