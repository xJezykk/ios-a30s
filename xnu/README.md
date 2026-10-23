# XNU for the Galaxy A30s (SM-A307FN, Exynos 7904)

Built on GitHub's macOS arm64 runners by `.github/workflows/xnu.yml`, using
[blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build) (MIT) at a pinned commit.

- `MACOS_VERSION=13.5` -> xnu-8796 (same generation as iOS 16.6/16.7, the last iOS for iPhone 8).
- `patches/` in this repo carries **only the Exynos patches of this
  project** (`0082` onward). The upstream ios-sweet series (0001-0081,
  MIT) is fetched by the workflow at build time from a **pinned commit**
  and merged in glob order (`0001..0081` then ours `0082+`) - so no
  Apple-authored source text is mirrored in this repository.
- Output: `kernel.<config>` built with `MACHINE_CONFIG=SWEET` as a workflow artifact.

Fetch the latest build: `gh run download -R <owner>/ios-a30s -n xnu-kernel -D xnu/out`
